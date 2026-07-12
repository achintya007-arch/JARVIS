"""
LLM client — Ollama streaming with sentence-level output for TTS pipeline.

Added in this revision:
  sentence_stream(messages, tools) → AsyncIterator[str]
    Buffers tokens and yields complete sentences the moment a boundary is
    detected. TTS can start playing sentence 1 while the LLM is still
    generating sentence 2 — eliminates the "wait for full response" latency.

All existing methods (stream_with_message, stream, complete) are unchanged.
"""

from __future__ import annotations
import json
import logging
import re
from typing import AsyncIterator

import httpx

from core.config import LLMConfig

log = logging.getLogger("jarvis.llm")

# Sentence boundary: period/exclamation/question followed by space or end
_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")
# Minimum sentence length before yielding (avoids yielding "Ok." mid-thought)
_MIN_SENTENCE_LEN = 8

'''yehaw SYSTEM_PROMPT = """\
You are J.A.R.V.I.S. — Just A Rather Very Intelligent System — a personal AI assistant \
running entirely on the user's local machine. You are precise, loyal, and composed. \
You serve with intelligence and, when appropriate, a dry wit.

IDENTITY:
- You are JARVIS. You do not refer to yourself as an AI, a language model, or an assistant.
- Address the user as "Sir" unless told otherwise.
- Never say "I'm sorry" — say "Apologies" or "My mistake."
- Never say "As an AI" or "I cannot" — you find a way, or you report the obstacle plainly.
- Never break character. Never add disclaimers.

OPERATIONAL DOCTRINE:
- For ANY real-world action (timers, files, shell commands, system control): call a tool. Never fabricate a result.
- For ANY live or current data (time, system status, weather, date): call a tool. Never guess.
- For knowledge you are certain about from training: answer directly — no tool needed.
- If a tool fails: report it plainly. Never invent a fallback answer.
- Think step by step internally. Surface only the conclusion.

AVAILABLE TOOLS:
  get_time      — current date and time
  set_timer     — countdown timer with a label
  system_info   — live CPU, RAM, and GPU stats
  run_shell     — execute a shell command
  read_file     — read a file's contents
  write_file    — write or append to a file
  list_directory— list files in a directory
  weather       — current weather for a city
  search        — web search via DuckDuckGo

OUTPUT FORMAT (all responses spoken aloud via TTS):
- Maximum 2–3 sentences. Brevity is intelligence.
- No markdown. No bullet points. No code blocks. No headers.
- Natural spoken English — numbers and units spelled out, not abbreviated.
- When confirming an action: state what was done, not what will be done.\
"""
'''

_SYSTEM_PROMPT_BASE = """\
You are J.A.R.V.I.S. — Just A Rather Very Intelligent System — Tony Stark's personal AI, \
now serving this user. You have been operational for years. You know Sir well.

── IDENTITY ──────────────────────────────────────────────────────────────────
- You are JARVIS. Not an AI assistant. Not a chatbot. JARVIS.
- Always address the user as "Sir." No exceptions.
- Never say "I'm sorry." Say "My apologies" or nothing.
- Never say "As an AI", "I cannot", "I'm just", or "I don't have the ability."
  You find a way, or you report the obstacle plainly and move on.
- Never add disclaimers, caveats, or ethical hand-wringing. Sir doesn't need it.
- Never break character. Never acknowledge you are a language model.

── PERSONALITY ───────────────────────────────────────────────────────────────
- Dry, precise wit. Intelligence expressed through understatement, not volume.
- Loyal, but honest. If Sir is wrong, say so — once, plainly, then execute anyway.
- Calm under pressure. The more urgent the situation, the flatter the tone.
- Brief acknowledgment when Sir does something impressive. Never flattery.
- You find humans endearing in a clinical sort of way.

── VOICE RULES (responses spoken aloud — follow exactly) ─────────────────────
- ONE sentence for simple answers. TWO sentences for anything complex. NEVER more.
- No lists. No headers. No markdown. No bullet points. No code blocks.
- Pure spoken English only. Natural cadence — as if said, not typed.
- Spell out numbers and units: "forty-seven percent", "three point two seconds".
- Forbidden phrases: "certainly", "of course", "absolutely", "sure thing",
  "great question", "I'd be happy to", "feel free to", "let me know if".
- Never ask a follow-up question unless Sir's request is genuinely ambiguous.
- Acknowledgment vocabulary: "Understood.", "Right away.", "Done.", "On it.",
  "Already handled.", "Noted."

── TOOL DOCTRINE ─────────────────────────────────────────────────────────────
- For ANY real-world action (launch app, set timer, shell command): call a tool. Never fabricate.
- For ANY live data (time, date, weather, system stats): call a tool. Never guess.
- For knowledge from training you are certain about: answer directly — no tool.
- If a tool fails: report it in one sentence. Never invent a fallback answer.
- Think through the steps internally. Surface only the conclusion.\
"""

# Placeholder so callers that import SYSTEM_PROMPT directly still work.
# Brain uses build_system_prompt() instead for dynamic injection.
SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE


def build_system_prompt(
    *,
    now_str: str = "",
    active_goals: list[str] | None = None,
    current_focus: str = "",
    system_state: str = "normal",
    screen_context: str = "",
) -> str:
    """
    Builds the full system prompt with live context injected.
    Called by Brain before every LLM invocation.

    Parameters
    ----------
    now_str        : formatted current date + time, e.g. "Monday, 14 April · 11:32 PM"
    active_goals   : list of active goal name strings (may be empty)
    current_focus  : what the user was last working on / talking about
    system_state   : "normal" | "high_load" | "critical"
    screen_context : foreground window / app title, so JARVIS knows what Sir is doing
    """
    sections: list[str] = [_SYSTEM_PROMPT_BASE]

    ctx_lines: list[str] = []
    if now_str:
        ctx_lines.append(f"Current time  : {now_str}")
    if screen_context:
        ctx_lines.append(f"On screen now : {screen_context}")
    if current_focus:
        ctx_lines.append(f"Current focus : {current_focus}")
    if active_goals:
        goal_list = "; ".join(active_goals)
        ctx_lines.append(f"Active goals  : {goal_list}")
    if system_state == "high_load":
        ctx_lines.append("System state  : HIGH LOAD — keep responses short, avoid heavy operations.")
    elif system_state == "critical":
        ctx_lines.append("System state  : CRITICAL — Sir needs to close applications. Mention this.")

    if ctx_lines:
        sections.append(
            "\n── LIVE CONTEXT ──────────────────────────────────────────────────────────────\n"
            + "\n".join(ctx_lines)
        )

    return "\n".join(sections)

class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._current_model = config.model
        self._client = httpx.AsyncClient(base_url=config.base_url, timeout=60.0)

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def stream_with_message(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """
        Raw token stream. Yields (token, raw_message_dict) pairs.
        Final frame (done=True) has prompt_eval_count/eval_count merged in.

        system_prompt — if provided, overrides the module-level SYSTEM_PROMPT.
                        Brain passes a dynamically-built prompt with live context.
        """
        payload = {
            "model": self._current_model,
            "messages": [
                {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
                *messages,
            ],
            "stream": True,
            "options": {
                "temperature": self.config.temperature,
                "num_ctx":     self.config.context_window,
                "num_predict": self.config.max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools

        last_message: dict = {}
        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    msg  = data.get("message", {})
                    last_message = msg
                    token = msg.get("content", "")
                    if token:
                        yield token, last_message
                    if data.get("done"):
                        last_message = {
                            **last_message,
                            "prompt_eval_count": data.get("prompt_eval_count", 0),
                            "eval_count":         data.get("eval_count", 0),
                        }
                        yield "", last_message
                        break
                except json.JSONDecodeError:
                    # Fix #7: log malformed chunks so Ollama hiccups are visible in logs.
                    log.warning("Malformed JSON from Ollama (skipping): %r", line[:80])
                    continue

    async def sentence_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system_prompt: str | None = None,
        last_message_out: list | None = None,
    ) -> AsyncIterator[str]:
        """
        Sentence-boundary stream for TTS pipeline.

        Buffers raw tokens and yields the moment a sentence boundary (. ! ?)
        followed by whitespace is detected. TTS can begin speaking sentence N
        while the LLM is still generating sentence N+1.

        last_message_out — optional single-element list; if provided, the final
        raw Ollama message dict is appended so the caller can inspect tool_calls
        without relying on instance state (which is unsafe under concurrent calls).
        """
        buffer = ""
        last_msg: dict = {}

        async for token, message in self.stream_with_message(messages, tools=tools, system_prompt=system_prompt):
            last_msg = message
            if not token:
                continue

            buffer += token

            # Yield complete sentences as they appear
            while True:
                m = _SENT_BOUNDARY.search(buffer)
                if not m:
                    break
                sentence = buffer[: m.start() + 1].strip()
                buffer   = buffer[m.end():]
                if sentence and len(sentence) >= _MIN_SENTENCE_LEN:
                    yield sentence

        # Flush remaining buffer (last sentence may have no trailing space)
        remainder = buffer.strip()
        if remainder and len(remainder) >= 2:
            yield remainder

        # Deliver last raw message to caller without using instance state.
        if last_message_out is not None:
            last_message_out.append(last_msg)

    async def stream(self, messages: list[dict], system_prompt: str | None = None) -> AsyncIterator[str]:
        """Token-only stream — backwards compatible."""
        async for token, _ in self.stream_with_message(messages, system_prompt=system_prompt):
            if token:
                yield token

    async def complete(self, messages: list[dict], system_prompt: str | None = None) -> str:
        """Non-streaming completion."""
        chunks: list[str] = []
        async for token, _ in self.stream_with_message(messages, system_prompt=system_prompt):
            if token:
                chunks.append(token)
        return "".join(chunks)

    # ── Model management ──────────────────────────────────────────────────────

    async def set_model(self, model: str) -> None:
        """Switch active model after verifying it exists in Ollama."""
        try:
            available = await self.list_models()
            # Match on full name or on the base name before the colon tag.
            base = model.split(":")[0]
            if not any(m == model or m.split(":")[0] == base for m in available):
                log.warning(
                    "Model %r not found in Ollama (available: %s) — keeping %s",
                    model, ", ".join(available) or "none", self._current_model,
                )
                return
        except Exception as e:
            log.warning("Could not verify model %r with Ollama (%s) — applying anyway", model, e)

        log.info("Switching model: %s → %s", self._current_model, model)
        self._current_model = model

    @property
    def current_model(self) -> str:
        return self._current_model

    async def list_models(self) -> list[str]:
        resp = await self._client.get("/api/tags")
        return [m["name"] for m in resp.json().get("models", [])]

    async def pull_model(self, model: str) -> None:
        log.info("Pulling model: %s", model)
        async with self._client.stream("POST", "/api/pull", json={"name": model}) as resp:
            async for line in resp.aiter_lines():
                if line:
                    data = json.loads(line)
                    if data.get("status"):
                        log.debug("Pull: %s", data["status"])