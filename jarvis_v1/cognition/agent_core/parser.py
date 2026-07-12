"""
Tool call parser — extracts ToolCall objects from LLM output.

Supports two strategies (tried in order):
  1. Native Ollama tool_calls field (Qwen2.5 / Phi-4 with tools param)
  2. JSON block fallback for models that inline JSON in text

Extracted from claw-code's tool routing pattern:
  PortRuntime.route_prompt() did keyword-token matching.
  We replace that with LLM-native structured output — more robust,
  zero maintenance, works with any model that supports tool calling.
"""

from __future__ import annotations

import json
import logging
import re

from .schema import ToolCall

log = logging.getLogger("jarvis.agent_core.parser")

# Regex to find a JSON block the LLM may emit inline
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_RAW_JSON_RE = re.compile(r"\{[^{}]*\"tool\"\s*:[^{}]*\}", re.DOTALL)


def parse_tool_calls_from_ollama(message: dict) -> list[ToolCall]:
    """
    Parse native Ollama tool_calls from the /api/chat response message dict.

    Ollama returns:
      message = {
        "role": "assistant",
        "tool_calls": [{"function": {"name": "...", "arguments": {...}}}]
      }
    """
    raw_calls = message.get("tool_calls") or []
    result = []
    for call in raw_calls:
        fn = call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if name:
            result.append(ToolCall(tool=name, args=args))
    return result


def parse_tool_calls_from_text(text: str) -> list[ToolCall]:
    """
    Fallback: extract a JSON tool call the model may have written inline.

    Expected shape (either fenced or bare):
      {"tool": "run_shell", "args": {"command": "ls -la"}}
    """
    candidates = _JSON_BLOCK_RE.findall(text) + _RAW_JSON_RE.findall(text)
    result = []
    for raw in candidates:
        try:
            obj = json.loads(raw)
            if "tool" in obj and isinstance(obj.get("args"), dict):
                result.append(ToolCall(tool=obj["tool"], args=obj["args"]))
        except (json.JSONDecodeError, TypeError):
            continue
    return result


def extract_tool_calls(message: dict, text: str) -> list[ToolCall]:
    """
    Try native parsing first, fall back to text extraction.
    Returns empty list if LLM intends a plain-text response.
    """
    native = parse_tool_calls_from_ollama(message)
    if native:
        log.debug("Native tool calls parsed: %s", [c.tool for c in native])
        return native

    fallback = parse_tool_calls_from_text(text)
    if fallback:
        log.debug("Fallback text tool calls parsed: %s", [c.tool for c in fallback])
    return fallback
