"""
DecisionEngine — the core intelligence layer of JARVIS.

Pipeline per event:
  perception → state snapshot → decision → action

Three-stage decision flow (fastest first, LLM only as last resort):

  Stage 1 — Reflex     : FastRouter regex match → immediate tool
  Stage 2 — Rule-based : System events, mood signals → scripted response
  Stage 3 — LLM        : Full reasoning when nothing else suffices

  Memory is injected upstream by ContextManager.build_messages() before
  decide() is called — no separate memory-aware stage needed.

Decision priority levels:
  CRITICAL  (100) — interrupts TTS immediately (system alerts, emergencies)
  HIGH       (75) — queued next, skips idle content
  NORMAL     (50) — standard response flow
  LOW        (25) — proactive suggestions, can be dropped if busy
  BACKGROUND (10) — internal thoughts, never interrupt

Each Decision tells the Assistant exactly what to do — no ambiguity.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from action.fast_router import FastRouter, RouteResult
from cognition.agent_state import AgentState, StateSnapshot

log = logging.getLogger("jarvis.decision")


# ── Priority constants ────────────────────────────────────────────────────────

PRIORITY_CRITICAL   = 100
PRIORITY_HIGH       = 75
PRIORITY_NORMAL     = 50
PRIORITY_LOW        = 25
PRIORITY_BACKGROUND = 10


# ── Decision types ────────────────────────────────────────────────────────────

ActionType = Literal["tool", "speak", "llm", "ignore", "plan"]


@dataclass
class Decision:
    """
    The output of the DecisionEngine — a fully resolved action.

    action_type:
      "tool"   → execute tool_call immediately via AgentExecutor
      "speak"  → speak content directly (no LLM)
      "llm"    → pass messages to LLM streaming pipeline
      "ignore" → do nothing
      "plan"   → multi-step plan (list of tool steps in content)

    priority:
      Drives interrupt behaviour in Assistant.
      >= PRIORITY_CRITICAL → interrupt current TTS
      >= PRIORITY_HIGH     → enqueue next
      < PRIORITY_HIGH      → drop if TTS is active
    """
    action_type:  ActionType
    priority:     int         = PRIORITY_NORMAL
    content:      Any         = None           # str | RouteResult | list[dict]
    should_act:   bool        = True
    reason:       str         = ""             # for logging/debug
    interrupt_tts: bool       = False          # derived from priority


# ── Proactive thought ─────────────────────────────────────────────────────────

@dataclass
class InternalThought:
    should_act: bool
    content:    str
    priority:   int = PRIORITY_LOW


# ── Rule tables ───────────────────────────────────────────────────────────────

# Scripted responses by user_state — no LLM needed
_MOOD_RESPONSES: dict[str, list[str]] = {
    "tired": [
        "You sound tired, Sir. Might be worth stepping away for a bit.",
        "Noted. I'll keep things brief while you're running low.",
        "Rest is a legitimate productivity strategy, Sir.",
    ],
    "stressed": [
        "Let's take this one thing at a time.",
        "Understood. I'll focus on what's most useful right now.",
        "I've got this end. You focus on the thinking.",
    ],
    "frustrated": [
        "Noted. Let's figure out what went wrong.",
        "Understood. Walk me through it and we'll sort it.",
        "I hear you. Let's fix it.",
    ],
    "happy": [
        "Good to hear, Sir.",
        "Excellent.",
    ],
}

# Scripted responses by intent — used for gratitude / chitchat
_INTENT_RESPONSES: dict[str, list[str]] = {
    "gratitude": [
        "Of course, Sir.",
        "That's what I'm here for.",
        "Always.",
    ],
}

# Idle proactive prompts — only fire if idle > threshold and low probability
_IDLE_PROMPTS = [
    "Still here if you need anything, Sir.",
    "Quiet out there. Everything alright?",
    "Systems nominal. Awaiting your next move.",
]

_IDLE_THRESHOLD_SECONDS = 300   # 5 minutes before proactive idle prompt
_IDLE_SPEAK_PROBABILITY = 0.15  # 15% chance per internal loop tick

# Goal creation/completion is owned entirely by GoalManager.handle_input(),
# which the Brain calls synchronously BEFORE the decision pipeline. So goal
# utterances never reach decide(); anything GoalManager declines correctly falls
# through here to the LLM. (Previously this module suppressed goal phrases to
# "ignore", which could silently swallow edge cases GoalManager didn't handle.)


class DecisionEngine:
    """
    The reasoning core. Given an event (user text), current state, and
    recalled memories, returns a Decision that tells the Assistant
    exactly what to do — and at what priority.

    All four stages are tried in order. The first that produces a
    confident decision short-circuits the rest.
    """

    def __init__(self, fast_router: FastRouter):
        self._router = fast_router
        self._last_mood_response_ts: float = 0.0
        self._mood_response_cooldown = 120.0  # seconds between mood comments

    # ── Main entry point ──────────────────────────────────────────────────────

    async def decide(
        self,
        text: str,
        state: StateSnapshot,
        memories: list[str],
        messages: list[dict],
    ) -> Decision:
        """
        Given the user's input and current world-state, return the best Decision.

        Parameters
        ----------
        text     : raw user utterance
        state    : snapshot from AgentState.snapshot()
        memories : recalled strings from MemoryStore (may be empty)
        messages : built message list from ContextManager (for LLM stage)
        """

        # ── Stage 1: Reflex — FastRouter ────────────────────────────────────
        route = self._router.route(text)
        if isinstance(route, list):
            log.info("Decision: REFLEX compound=%d steps", len(route))
            return Decision(
                action_type = "plan",
                priority    = PRIORITY_HIGH,
                content     = route,
                should_act  = True,
                reason      = f"fast_router:compound({len(route)} steps)",
            )
        if route:
            log.info("Decision: REFLEX route=%s args=%s", route.tool, route.args)
            return Decision(
                action_type = "tool",
                priority    = PRIORITY_HIGH,
                content     = route,
                should_act  = True,
                reason      = f"fast_router:{route.tool}",
            )

        # ── Stage 2: Rule-based ──────────────────────────────────────────────
        rule_decision = self._rule_based(text, state)
        if rule_decision is not None:
            return rule_decision

        # ── Ignore — low-signal input (no alphabetic content, or ≤3 chars) ──
        stripped = text.strip()
        if len(stripped) <= 3 or not any(c.isalpha() for c in stripped):
            log.info("Decision: IGNORE low-signal=%r", stripped)
            return Decision(
                action_type = "ignore",
                priority    = PRIORITY_BACKGROUND,
                should_act  = False,
                reason      = "ignore:low_signal",
            )

        # ── Stage 3: LLM ─────────────────────────────────────────────────────
        # Memory is already injected into `messages` by ContextManager.build_messages().
        log.info("Decision: LLM fallback")
        return Decision(
            action_type = "llm",
            priority    = PRIORITY_NORMAL,
            content     = messages,
            should_act  = True,
            reason      = "llm:full_reasoning",
        )

    # ── Stage 2: Rule-based intelligence ────────────────────────────────────

    def _rule_based(self, text: str, state: StateSnapshot) -> Optional[Decision]:
        """
        Handle events that don't need the LLM.
        System events, mood signals, simple intents.
        (Goal creation/completion is handled upstream by GoalManager.handle_input.)
        """

        # System critical — always interrupt
        if state.system_state == "critical":
            return Decision(
                action_type  = "speak",
                priority     = PRIORITY_CRITICAL,
                content      = (
                    "Sir, system resources are critical. "
                    "You may want to close some applications."
                ),
                should_act   = True,
                interrupt_tts = True,
                reason       = "rule:system_critical",
            )

        # Gratitude — no LLM needed
        if state.conversation_intent == "gratitude":
            return Decision(
                action_type = "speak",
                priority    = PRIORITY_NORMAL,
                content     = random.choice(_INTENT_RESPONSES["gratitude"]),
                should_act  = True,
                reason      = "rule:gratitude",
            )

        # Mood-aware comment — with cooldown so it doesn't repeat every message
        mood = state.user_state
        if (
            mood in _MOOD_RESPONSES
            and (time.time() - self._last_mood_response_ts) > self._mood_response_cooldown
        ):
            # Only comment on mood if input is short / not a complex command
            word_count = len(text.split())
            if word_count < 12 and state.conversation_intent in ("chitchat", "unknown"):
                self._last_mood_response_ts = time.time()
                return Decision(
                    action_type = "speak",
                    priority    = PRIORITY_NORMAL,
                    content     = random.choice(_MOOD_RESPONSES[mood]),
                    should_act  = True,
                    reason      = f"rule:mood:{mood}",
                )

        return None

    # ── Proactive internal loop ───────────────────────────────────────────────

    async def generate_internal_thought(
        self,
        state: StateSnapshot,
        memories: list[str],
    ) -> InternalThought:
        """
        Called by the background internal_loop in assistant.py every N seconds.
        Returns a thought — may or may not trigger an action.

        Rules:
          - Don't speak if user was recently active (< idle threshold)
          - Don't speak if system is under load
          - Randomise to avoid feeling mechanical
        """

        # User is active — nothing to do
        if state.idle_seconds < _IDLE_THRESHOLD_SECONDS:
            return InternalThought(should_act=False, content="")

        # System is stressed — don't add noise
        if state.system_state != "normal":
            return InternalThought(should_act=False, content="")

        # Low probability random idle prompt
        if random.random() < _IDLE_SPEAK_PROBABILITY:
            prompt = random.choice(_IDLE_PROMPTS)
            return InternalThought(
                should_act = True,
                content    = prompt,
                priority   = PRIORITY_BACKGROUND,
            )

        return InternalThought(should_act=False, content="")

    # ── Priority → interrupt logic ────────────────────────────────────────────

    @staticmethod
    def should_interrupt_tts(decision: Decision) -> bool:
        """True if this decision is urgent enough to cut off current speech."""
        return decision.priority >= PRIORITY_CRITICAL or decision.interrupt_tts