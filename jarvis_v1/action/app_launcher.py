"""
App launcher — the ONE allowlist for every way JARVIS starts an application
(FastRouter commands, the LLM's open_app tool, V2 and V3, and the Python
fallback for the Brain–Body Protocol's desktop.app.launch).

Security model:
  - Only apps in ALLOWED_APPS can be launched. A name is matched against fixed
    aliases; anything else raises AppNotAllowed (E_NOT_ALLOWED). No caller-
    supplied string is ever executed, opened, or passed to a shell.
  - Each app resolves to an absolute executable path (PATH / System32, the
    Windows "App Paths" registry, or known install locations) and starts with
    subprocess.Popen(argv) — shell=False, so &, |, ;, quotes and backticks in a
    name are just characters in a lookup key that matches nothing.
  - Store/UWP apps without an .exe launch through a fixed protocol URI from this
    table (os.startfile on a constant, never on input).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("jarvis.launcher")


class AppNotAllowed(Exception):
    """The requested app isn't on the allowlist (BBP error code E_NOT_ALLOWED)."""

    code = "E_NOT_ALLOWED"

    def __init__(self, requested: str) -> None:
        super().__init__(f"'{requested}' is not on the list of apps I'm allowed to open")
        self.requested = requested


class AppNotInstalled(Exception):
    """Allowed, but no executable was found on this machine (E_NOT_FOUND)."""

    code = "E_NOT_FOUND"


@dataclass(frozen=True)
class AppSpec:
    key: str
    display: str
    exe: str = ""                                   # executable file name to resolve
    args: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()                # known install paths (env vars allowed)
    uri: str = ""                                   # protocol fallback for Store apps
    aliases: tuple[str, ...] = field(default=())


ALLOWED_APPS: dict[str, AppSpec] = {a.key: a for a in (
    AppSpec("notepad", "Notepad", "notepad.exe"),
    AppSpec("calculator", "Calculator", "calc.exe", aliases=("calc",)),
    AppSpec("paint", "Paint", "mspaint.exe", aliases=("mspaint",)),
    AppSpec("task manager", "Task Manager", "taskmgr.exe", aliases=("taskmgr",)),
    AppSpec("explorer", "File Explorer", "explorer.exe", aliases=("file explorer", "files")),
    AppSpec("chrome", "Chrome", "chrome.exe", aliases=("google chrome",)),
    AppSpec("firefox", "Firefox", "firefox.exe"),
    AppSpec("vscode", "VS Code", "Code.exe",
            candidates=(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
                        r"%PROGRAMFILES%\Microsoft VS Code\Code.exe"),
            aliases=("code", "vs code", "visual studio code", "vs studio")),
    AppSpec("spotify", "Spotify", "Spotify.exe",
            candidates=(r"%APPDATA%\Spotify\Spotify.exe",), uri="spotify:"),
    AppSpec("discord", "Discord", "Update.exe", args=("--processStart", "Discord.exe"),
            candidates=(r"%LOCALAPPDATA%\Discord\Update.exe",)),
    AppSpec("todoist", "Todoist", "Todoist.exe",
            candidates=(r"%LOCALAPPDATA%\Todoist\Todoist.exe",
                        r"%LOCALAPPDATA%\Programs\todoist\Todoist.exe")),
    AppSpec("word", "Word", "winword.exe", aliases=("microsoft word",)),
    AppSpec("excel", "Excel", "excel.exe", aliases=("microsoft excel",)),
    AppSpec("outlook", "Outlook", "outlook.exe", aliases=("microsoft outlook",)),
)}

_ALIASES: dict[str, str] = {}
for _spec in ALLOWED_APPS.values():
    _ALIASES[_spec.key] = _spec.key
    _ALIASES[_spec.display.lower()] = _spec.key
    for _alias in _spec.aliases:
        _ALIASES[_alias] = _spec.key


def resolve_key(name: str) -> str:
    """Map a spoken/typed name to an allowlist key, or raise AppNotAllowed.
    Exact alias match only — no substring or fuzzy matching on untrusted input."""
    key = " ".join(str(name).lower().split())
    key = key.removesuffix(".exe")
    if key in _ALIASES:
        return _ALIASES[key]
    raise AppNotAllowed(str(name)[:80])


def _app_paths_registry(exe: str) -> str | None:
    """HKCU/HKLM ...\\App Paths\\<exe> — where installers register full paths."""
    if sys.platform != "win32":
        return None
    import winreg
    sub = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub) as k:
                value, _ = winreg.QueryValueEx(k, "")
                path = os.path.expandvars(str(value).strip('"'))
                if Path(path).is_file():
                    return path
        except OSError:
            continue
    return None


def resolve_executable(spec: AppSpec) -> str | None:
    """Absolute path of the app's executable, or None if not installed."""
    for candidate in spec.candidates:
        path = os.path.expandvars(candidate)
        if Path(path).is_file():
            return path
    if spec.exe:
        found = shutil.which(spec.exe) or _app_paths_registry(spec.exe)
        if found and Path(found).is_file():
            return str(Path(found).resolve())
    return None


def launch(name: str) -> str:
    """Start an allowlisted app. Returns its display name. Raises AppNotAllowed
    or AppNotInstalled. Never uses a shell."""
    spec = ALLOWED_APPS[resolve_key(name)]
    exe = resolve_executable(spec)
    if exe is not None:
        argv = [exe, *spec.args]
        log.info("launch %s: %s", spec.key, argv)
        subprocess.Popen(argv, shell=False, close_fds=True)
        return spec.display
    if spec.uri:
        log.info("launch %s via %s", spec.key, spec.uri)
        os.startfile(spec.uri)                       # constant from the table above
        return spec.display
    raise AppNotInstalled(f"{spec.display} doesn't seem to be installed")
