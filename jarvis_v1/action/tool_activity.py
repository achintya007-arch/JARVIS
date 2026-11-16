"""
What a running tool looks like from outside: one short line ("Opening
Calculator", "Pressing 'Seven' in Calculator") for the HUD's activity list
(BBP tool.started / tool.finished, amendment 23).

The line comes from a fixed template per tool and never carries what the tool
works on: no typed text, shell commands, file contents, search queries or
clipboard text. App names, allowlisted display names, UI element names and a
file's base name are allowed. Whatever is filled in is cleaned (control,
bidirectional and zero-width characters removed, whitespace collapsed) and the
line is cut to SUMMARY_MAX characters.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from pathlib import PurePath
from typing import Any

from action import app_launcher

SUMMARY_MAX = 80
_NAME_MAX = 40

# A tool's outcome as the HUD shows it: done (✓), you declined or it wasn't
# allowed (grey), or it failed (✗).
STATUS_OK = "ok"
STATUS_DENIED = "denied"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"     # the turn was stopped while it ran

# (call_id, tool, summary) / (call_id, tool, summary, status)
StartedCallback = Callable[[str, str, str], None]
FinishedCallback = Callable[[str, str, str, str], None]


def clean(text: Any, limit: int) -> str:
    """Printable, one line, at most `limit` characters (… when cut)."""
    out = []
    for ch in str(text or ""):
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp"):        # controls, bidi/zero-width, separators
            out.append(" ")
        else:
            out.append(ch)
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _app(name: Any) -> str:
    """An allowlisted app's display name, else "" (a window title or an
    unknown name may say more than the app: a document's name, say)."""
    try:
        return app_launcher.ALLOWED_APPS[app_launcher.resolve_key(str(name))].display
    except Exception:
        return ""


def _quoted(name: Any) -> str:
    n = clean(name, _NAME_MAX)
    return f"'{n}'" if n else "an element"


def _file(path: Any) -> str:
    try:
        base = PurePath(str(path or "").replace("\\", "/")).name
    except Exception:
        base = ""
    return clean(base, _NAME_MAX) or "a file"


def _in_app(args: dict[str, Any]) -> str:
    app = args.get("app")
    if not app:
        return ""
    # The Body's allowlist id ("calculator") when the Brain doesn't know it.
    return f" in {_app(app) or clean(app, _NAME_MAX)}"


_FIXED: dict[str, str] = {
    "get_time": "Checking the time",
    "system_info": "Checking the system",
    "run_shell": "Running a shell command",
    "search": "Searching the web",
    "screenshot": "Taking a screenshot",
    "click": "Clicking on the screen",
    "double_click": "Clicking on the screen",
    "right_click": "Clicking on the screen",
    "move_mouse": "Moving the mouse",
    "scroll": "Scrolling",
    "drag": "Dragging on the screen",
    "type_text": "Typing",
    "hotkey": "Pressing keys",
    "press_key": "Pressing a key",
    "key_down": "Pressing a key",
    "key_up": "Releasing a key",
    "list_windows": "Listing windows",
    "get_screen_size": "Checking the screen",
    "get_mouse_position": "Checking the mouse",
    "copy_selection": "Copying the selection",
    "paste_text": "Pasting",
    "get_clipboard": "Reading the clipboard",
    "set_clipboard": "Setting the clipboard",
    "get_volume": "Checking the volume",
    "mute_volume": "Muting the sound",
    "unmute_volume": "Unmuting the sound",
    "create_note": "Writing a note",
    "append_to_daily_note": "Adding to today's note",
    "search_vault": "Searching your notes",
    "link_notes": "Linking notes",
    "complete_task": "Ticking off a task",
    "list_today": "Reading today's note",
}


def _volume(args: dict[str, Any]) -> str:
    level = args.get("level")
    if level is not None:
        try:
            return f"Setting the volume to {max(0, min(100, int(level)))}%"
        except (TypeError, ValueError):
            pass
    return "Changing the volume"


def _timer(args: dict[str, Any]) -> str:
    try:
        seconds = int(args.get("duration_seconds") or 60)
    except (TypeError, ValueError):
        return "Setting a timer"
    if seconds % 3600 == 0 and seconds >= 3600:
        span = f"{seconds // 3600} h"
    elif seconds % 60 == 0 and seconds >= 60:
        span = f"{seconds // 60} min"
    else:
        span = f"{seconds} s"
    return f"Setting a {span} timer"


_TEMPLATES: dict[str, Callable[[dict[str, Any]], str]] = {
    "open_app": lambda a: f"Opening {_app(a.get('app') or a.get('name') or a.get('command')) or 'an app'}",
    "close_app": lambda a: f"Closing {_app(a.get('name')) or 'an app'}",
    "focus_window": lambda a: f"Switching to {_app(a.get('title')) or 'a window'}",
    "minimize_window": lambda a: f"Minimizing {_app(a.get('title')) or 'a window'}",
    "maximize_window": lambda a: f"Maximizing {_app(a.get('title')) or 'a window'}",
    "close_window": lambda a: f"Closing {_app(a.get('title')) or 'a window'}",
    "read_screen_ui": lambda a: f"Reading {_app(a.get('app')) or 'the screen'}",
    "click_element": lambda a: f"Pressing {_quoted(a.get('name') or a.get('automation_id'))}{_in_app(a)}",
    "set_element_text": lambda a: f"Typing into {_quoted(a.get('name') or a.get('automation_id'))}{_in_app(a)}",
    "read_file": lambda a: f"Reading {_file(a.get('path'))}",
    "write_file": lambda a: f"Writing {_file(a.get('path'))}",
    "list_directory": lambda a: "Listing a folder",
    "weather": lambda a: f"Checking the weather in {clean(a.get('city'), _NAME_MAX)}"
                         if a.get("city") else "Checking the weather",
    "open_url": lambda a: f"Opening {clean(a.get('name'), _NAME_MAX)} in the browser"
                          if a.get("name") else "Opening a web page",
    "set_volume": _volume,
    "set_timer": _timer,
}


def summarize(tool: str, args: dict[str, Any] | None) -> str:
    """The activity line for this call (never empty, at most SUMMARY_MAX)."""
    args = args if isinstance(args, dict) else {}
    try:
        if tool in _TEMPLATES:
            text = _TEMPLATES[tool](args)
        elif tool in _FIXED:
            text = _FIXED[tool]
        else:
            text = f"Using {clean(tool, _NAME_MAX) or 'a tool'}"
    except Exception:
        text = f"Using {clean(tool, _NAME_MAX) or 'a tool'}"
    return clean(text, SUMMARY_MAX) or "Working"
