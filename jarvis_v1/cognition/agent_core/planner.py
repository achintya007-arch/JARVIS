"""
AgentCore — the agentic planning and execution loop.

Sprint A: TOOL_SCHEMAS extended with get_time, set_timer, system_info.
Phase 2 : Collects PermissionDenial objects from executor results.
Phase 3 : Real token counts from Ollama accumulated into UsageSummary.
Sprint D : SCREEN_TOOL_SCHEMAS added — screenshot, mouse, keyboard, windows, apps.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .parser import extract_tool_calls
from .schema import AgentResult, TurnState


def _clean_for_tts(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"[*_#~>|]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\n{2,}", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


if TYPE_CHECKING:
    from action.agent_executor import AgentExecutor
    from cognition.llm_client import LLMClient

log = logging.getLogger("jarvis.agent_core")


# ── Core tool schemas ─────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    # ── Time & timers ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current date and time",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Set a countdown timer that fires an alert when complete",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Timer duration in seconds",
                    },
                    "label": {
                        "type": "string",
                        "description": "Short name for the timer, e.g. 'pasta', 'meeting'",
                    },
                },
                "required": ["duration_seconds"],
            },
        },
    },
    # ── System awareness ───────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Get live CPU, RAM, and GPU usage plus system uptime",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Shell & files ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command and return stdout/stderr",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file (requires user confirmation)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["w", "a"]},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files in a directory",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    # ── Clipboard ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Read the current clipboard text content",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": "Copy text to the clipboard",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to copy to clipboard"},
                },
                "required": ["text"],
            },
        },
    },
    # ── Volume ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_volume",
            "description": "Get the current system volume level (0–100) and mute state",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "Set system volume. Use 'level' for absolute (0–100) or 'delta' for relative change (e.g. +10 / -10)",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "description": "Absolute volume level 0–100"},
                    "delta": {"type": "integer", "description": "Relative change, e.g. 10 or -10"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mute_volume",
            "description": "Mute system audio",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unmute_volume",
            "description": "Unmute system audio",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Web ────────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the web via DuckDuckGo",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    # ── Vault / Obsidian memory (V3) ───────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a note in the user's Obsidian vault. Use for 'make a note about X'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short note title"},
                    "body":  {"type": "string", "description": "Note content"},
                    "tags":  {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_daily_note",
            "description": "Append a line to today's daily note. Use for 'log this' / journaling.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": "Search the user's notes for relevant context. Use for 'what do my notes say about X'.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_today",
            "description": "Summarize what's been recorded in today's daily note.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_notes",
            "description": "Add a wikilink from one note to another.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Source note name"},
                    "dst": {"type": "string", "description": "Target note to link to"},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a task/goal as complete in the vault's goals list.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]


# ── Screen tool schemas ───────────────────────────────────────────────────────

SCREEN_TOOL_SCHEMAS = [
    # ── Screen capture ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Capture the screen or a region. Returns the saved file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [left, top, width, height] to capture a region. Omit for full screen.",
                    },
                },
                "required": [],
            },
        },
    },
    # ── Mouse ──────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the mouse at screen coordinates (x, y). Omit x/y to click at current position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Screen x coordinate"},
                    "y": {"type": "integer", "description": "Screen y coordinate"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button"},
                    "clicks": {"type": "integer", "description": "Number of clicks (default 1)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Double-click at screen coordinates",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "right_click",
            "description": "Right-click at screen coordinates to open context menus",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_mouse",
            "description": "Move the mouse cursor to coordinates without clicking",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "duration": {"type": "number", "description": "Move duration in seconds (default 0.2)"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the mouse wheel. Positive clicks = up, negative = down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "clicks": {"type": "integer", "description": "Scroll amount. Positive=up, negative=down."},
                    "x": {"type": "integer", "description": "Optional x position to scroll at"},
                    "y": {"type": "integer", "description": "Optional y position to scroll at"},
                },
                "required": ["clicks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag",
            "description": "Click and drag from one screen position to another",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer", "description": "Start x"},
                    "y1": {"type": "integer", "description": "Start y"},
                    "x2": {"type": "integer", "description": "End x"},
                    "y2": {"type": "integer", "description": "End y"},
                    "duration": {"type": "number", "description": "Drag duration in seconds"},
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        },
    },
    # ── Keyboard ───────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text at the current cursor position",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                    "interval": {"type": "number", "description": "Delay between keystrokes in seconds (default 0.04)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Press a keyboard shortcut. e.g. ctrl+c, alt+f4, win+d",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys to press together e.g. ['ctrl', 'c'] or ['alt', 'f4']",
                    },
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a single key one or more times. e.g. enter, esc, tab, space, backspace, delete, up, down, left, right, f5",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key name"},
                    "presses": {"type": "integer", "description": "Number of times to press (default 1)"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_down",
            "description": "Hold a key down (use key_up to release)",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to hold"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_up",
            "description": "Release a held key",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key to release"},
                },
                "required": ["key"],
            },
        },
    },
    # ── Screen info ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_screen_size",
            "description": "Get the screen resolution width and height",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mouse_position",
            "description": "Get the current mouse cursor coordinates",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # ── Window management ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": "List all currently open window titles",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "Bring a window to the foreground by partial title match",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Partial window title to match"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "minimize_window",
            "description": "Minimize a window by partial title match",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "maximize_window",
            "description": "Maximize a window by partial title match",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_window",
            "description": "Close a window by partial title match. Requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    # ── App control ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": (
                "Launch an application by name. "
                "Supports: chrome, firefox, notepad, explorer, calculator, cmd, "
                "powershell, vscode, spotify, discord, word, excel, outlook, paint, "
                "terminal, or any executable name/path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Application name or path"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "Force-close an application by process name. Requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Process name e.g. 'notepad' or 'chrome.exe'"},
                },
                "required": ["name"],
            },
        },
    },
    # ── Clipboard ──────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "copy_selection",
            "description": "Copy the currently selected text/content to clipboard and return it",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "paste_text",
            "description": "Paste text at the current cursor position",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to paste"},
                },
                "required": ["text"],
            },
        },
    },
]


# ── Schema builder ────────────────────────────────────────────────────────────

def build_tool_schemas(screen_enabled: bool = True) -> list:
    """Return the full tool schema list, with screen tools appended if enabled."""
    schemas = list(TOOL_SCHEMAS)
    if screen_enabled:
        schemas.extend(SCREEN_TOOL_SCHEMAS)
    return schemas


# ── AgentCore ─────────────────────────────────────────────────────────────────

class AgentCore:
    def __init__(
        self,
        llm: LLMClient,
        executor: AgentExecutor,
        max_turns: int = 5,
        stream_to_stdout: bool = True,
        tools: list | None = None,
    ):
        self.llm = llm
        self.executor = executor
        self.max_turns = max_turns
        self.stream_to_stdout = stream_to_stdout
        # Full tool set (core + screen/keyboard) so the agent can type, focus
        # windows, launch apps, etc. — not just the 21 core tools.
        self.tools = tools if tools is not None else build_tool_schemas()

    async def run(self, messages: list[dict]) -> AgentResult:
        state = TurnState(messages=list(messages))

        for turn in range(self.max_turns):
            state.turn = turn
            log.debug("Agent turn %d/%d", turn + 1, self.max_turns)

            # Step 1: call LLM
            response_text, raw_message, in_tok, out_tok = await self._call_llm(state.messages)
            state.last_response = response_text
            state.usage = state.usage.add(in_tok, out_tok)

            # Step 2: parse tool calls
            tool_calls = extract_tool_calls(raw_message, response_text)

            if not tool_calls:
                stripped = response_text.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    log.warning("Unparsed tool call in response — suppressing")
                    response_text = "I attempted a tool call but something went wrong. Please try again, Sir."

                log.info(
                    "Run complete | turns=%d | %s | denials=%d",
                    turn + 1, state.usage, len(state.permission_denials),
                )
                return AgentResult(
                    response=response_text,
                    used_tools=tuple(state.tool_calls_made),
                    turns=turn + 1,
                    stop_reason="done",
                    usage=state.usage,
                    permission_denials=tuple(state.permission_denials),
                )

            # Step 3: execute tools
            tool_names = [c.tool for c in tool_calls]
            log.info("Executing tools: %s", tool_names)
            state.tool_calls_made.extend(tool_names)

            plan = [c.to_executor_step() for c in tool_calls]
            exec_results = await self.executor.execute(plan)

            # Collect permission denials
            for r in exec_results:
                if r.get("status") == "denied" and "denial" in r:
                    state.permission_denials.append(r["denial"])

            # Step 4: feed results back
            state.messages = self._append_tool_results(
                state.messages, response_text, exec_results
            )

        # Max turns reached
        log.warning("Max turns (%d) reached, summarising", self.max_turns)
        summary, _, in_tok, out_tok = await self._summarise(state)
        state.usage = state.usage.add(in_tok, out_tok)

        log.info(
            "Run complete (max_turns) | turns=%d | %s | denials=%d",
            self.max_turns, state.usage, len(state.permission_denials),
        )
        return AgentResult(
            response=summary,
            used_tools=tuple(state.tool_calls_made),
            turns=self.max_turns,
            stop_reason="max_turns",
            usage=state.usage,
            permission_denials=tuple(state.permission_denials),
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _call_llm(
        self, messages: list[dict]
    ) -> tuple[str, dict, int, int]:
        chunks: list[str] = []
        raw_message: dict = {}

        async for chunk, message in self.llm.stream_with_message(messages, tools=self.tools):
            chunks.append(chunk)
            raw_message = message
            if self.stream_to_stdout and chunk:
                print(chunk, end="", flush=True)

        if self.stream_to_stdout and chunks:
            print()

        input_tokens  = raw_message.get("prompt_eval_count", 0)
        output_tokens = raw_message.get("eval_count", 0)
        log.debug("LLM turn: in=%d out=%d", input_tokens, output_tokens)

        text = _clean_for_tts("".join(chunks))
        return text, raw_message, input_tokens, output_tokens

    def _append_tool_results(
        self,
        messages: list[dict],
        assistant_text: str,
        results: list[dict],
    ) -> list[dict]:
        updated = list(messages)

        if assistant_text.strip():
            updated.append({"role": "assistant", "content": assistant_text})

        result_lines = []
        for r in results:
            if r["status"] == "ok":
                result_lines.append(f"[{r['tool']}] → {r['result']}")
            elif r["status"] == "denied":
                result_lines.append(f"[{r['tool']}] → DENIED: {r.get('reason', '')}")
            elif r["status"] == "skipped":
                result_lines.append(f"[{r['tool']}] → skipped: {r.get('reason', '')}")
            else:
                result_lines.append(f"[{r['tool']}] → ERROR: {r.get('error', 'unknown')}")

        updated.append({
            "role": "user",
            "content": (
                "Tool results:\n" + "\n".join(result_lines) + "\n\n"
                "Now give a short spoken response (1–3 sentences) summarising the result. "
                "No markdown. Stay in character as JARVIS."
            ),
        })
        return updated

    async def _summarise(self, state: TurnState) -> tuple[str, dict, int, int]:
        summary_messages = list(state.messages) + [{
            "role": "user",
            "content": (
                f"You have completed {state.turn + 1} turns using tools: "
                f"{', '.join(state.tool_calls_made) or 'none'}. "
                "Summarise what was accomplished in 1–2 sentences. Stay in character."
            ),
        }]
        return await self._call_llm(summary_messages)
