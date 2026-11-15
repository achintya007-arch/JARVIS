"""
What a gated action asks you before it runs (BBP confirm.request).

AgentExecutor gates four tools behind a confirmation: run_shell, write_file,
close_app and close_window. describe() turns a gated call into a ConfirmRequest:

  prompt  what JARVIS says and the dialog shows ("About to run the command
          git status. Shall I proceed, Sir?")
  detail  exactly what would run (the command, the file and its size, the app,
          the window), shown on its own line in the dialog (BBP amendment 17)
  risk    "high" for run_shell and close_app, "low" for write_file and
          close_window (BBP spec)

The arguments come from the LLM, so they're treated as untrusted text: control
characters and bidirectional overrides (which can make a dialog show something
other than what will run) are removed, whitespace is collapsed, and the length
is capped. No V3 dependencies, so V2 can use it too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HIGH_RISK = frozenset({"run_shell", "close_app"})
DETAIL_MAX = 200          # characters of detail shown in the dialog
SPOKEN_MAX = 60           # characters of detail read out in the prompt

# C0/C1 controls, zero-width characters and every bidi embedding/override/isolate.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")


@dataclass(frozen=True)
class ConfirmRequest:
    tool: str
    prompt: str
    detail: str = ""
    risk: str = "low"


def clean(text: Any, limit: int = DETAIL_MAX) -> str:
    """Untrusted text made safe to show: no control or bidi characters, single
    spaces, at most `limit` characters (cut with an ellipsis)."""
    text = " ".join(_UNSAFE.sub(" ", str(text)).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe(tool: str, args: dict[str, Any]) -> ConfirmRequest:
    args = args or {}
    if tool == "run_shell":
        what = "run the command"
        target = detail = clean(args.get("command", ""))
    elif tool == "write_file":
        content = str(args.get("content") or "")
        what = "append to the file" if str(args.get("mode") or "w").startswith("a") else "write to the file"
        path = clean(args.get("path", ""), DETAIL_MAX - 30)
        detail = f"{path} ({len(content)} characters)" if path else ""
        # Say the file's name, not a long absolute path cut off mid-name; the
        # full path is in the detail.
        target = re.split(r"[\\/]", path)[-1] or path
    elif tool == "close_app":
        what = "close"
        target = detail = clean(args.get("name", ""))
    elif tool == "close_window":
        what = "close the window"
        target = detail = clean(args.get("title", ""))
    else:
        what, target, detail = f"run {clean(tool, 40)}", "", ""

    spoken = clean(target, SPOKEN_MAX)
    target = f" {spoken}" if spoken else ""
    prompt = f"About to {what}{target}. Shall I proceed, Sir?"
    return ConfirmRequest(tool=tool, prompt=prompt, detail=detail,
                          risk="high" if tool in HIGH_RISK else "low")
