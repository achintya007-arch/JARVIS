"""
What a gated action asks you before it runs (BBP confirm.request).

AgentExecutor gates these tools behind a confirmation: run_shell, write_file,
close_app, close_window, and (BBP amendment 20) set_element_text always and
click_element when the element looks destructive or outbound. describe() turns
a gated call into a ConfirmRequest:

  prompt  what JARVIS says and the dialog shows ("About to run the command
          git status. Shall I proceed, Sir?")
  detail  exactly what would run (the command, the file and its size, the app,
          the window), shown on its own line in the dialog (BBP amendment 17)
  risk    "high" for run_shell, close_app and click_element, "low" for
          write_file, close_window and set_element_text (BBP spec)
  action  for click_element / set_element_text: the exact desktop request
          this approval is for; the Body accepts that request only if it
          matches (amendment 20)

The arguments come from the LLM, so they're treated as untrusted text: control
characters and bidirectional overrides (which can make a dialog show something
other than what will run) are removed, whitespace is collapsed, and the length
is capped. No V3 dependencies, so V2 can use it too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

HIGH_RISK = frozenset({"run_shell", "close_app", "click_element"})
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
    action: dict[str, Any] | None = field(default=None, compare=False)


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
    elif tool in ("click_element", "set_element_text"):
        element = args.get("element") or {}
        label = clean(element.get("name") or element.get("automation_id") or "", 80)
        kind = clean(element.get("control_type") or "", 40)
        app = clean(args.get("app") or "the app in front", 60)
        where = f"{app} › {kind + ' ' if kind else ''}'{label}'"
        if element.get("name") and element.get("automation_id"):
            where += f" [{clean(element['automation_id'], 60)}]"
        if tool == "click_element":
            what = "press"
            target = f"'{label}' in {app}"
            detail = clean(where + (f" ({clean(args['why'], 80)})" if args.get("why") else ""), DETAIL_MAX)
        else:
            text = str(args.get("text") or "")
            what = "type into"
            target = f"'{label}' in {app}"
            detail = clean(f"{where} ← {len(text)} characters", DETAIL_MAX)
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
