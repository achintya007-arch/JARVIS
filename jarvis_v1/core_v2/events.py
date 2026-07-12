"""
core_v2/events.py — All V2 event types.

Events are immutable value objects. Treat them as facts, not commands.
Do not mutate fields after creation.

Priority field (on every event, used by ActionAdapter for TTS dispatch):
    PRIORITY_NORMAL    = 0  — standard conversational flow
    PRIORITY_IMPORTANT = 5  — proactive thoughts, goal reminders
    PRIORITY_CRITICAL  = 10 — system alerts, timers — can interrupt TTS
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ── Priority constants ────────────────────────────────────────────────────────

PRIORITY_NORMAL    = 0
PRIORITY_IMPORTANT = 5
PRIORITY_CRITICAL  = 10


# ── Event definitions ─────────────────────────────────────────────────────────

@dataclass
class UserInput:
    """Text received from any input source."""
    text:      str
    source:    str           # "stt" | "keyboard" | "hotword"
    timestamp: float = field(default_factory=time.time)
    priority:  int   = PRIORITY_NORMAL


@dataclass
class StateUpdated:
    """Published after AgentState is updated from user input. Observability only."""
    snapshot: Any            # cognition.agent_state.StateSnapshot
    priority: int = PRIORITY_NORMAL


@dataclass
class DecisionReady:
    """Published after DecisionEngine returns. Observability / future extensions."""
    decision:      Any       # cognition.decision_engine.Decision
    original_text: str
    messages:      list      # list[dict] — pre-built LLM message list
    priority:      int = PRIORITY_NORMAL


@dataclass
class SpeakRequest:
    """
    Request to speak text via TTS.

    ActionAdapter dispatches based on priority:
      >= PRIORITY_CRITICAL  → interrupt current speech, then speak
      >= PRIORITY_IMPORTANT → enqueue, drain after current sentence
      == PRIORITY_NORMAL    → fire-and-forget enqueue
    """
    text:     str
    priority: int = PRIORITY_NORMAL


@dataclass
class ToolRequest:
    """Published when a tool is about to be dispatched. Observability only."""
    tool:       str
    args:       dict
    request_id: str
    priority:   int = PRIORITY_NORMAL


@dataclass
class ToolResult:
    """Published after a tool completes. Observability only."""
    tool:       str
    request_id: str
    status:     str
    result:     Any
    priority:   int = PRIORITY_NORMAL


@dataclass
class StreamComplete:
    """Published when a full response (LLM or direct) is ready. Triggers memory persistence."""
    response:      str
    original_text: str
    priority:      int = PRIORITY_NORMAL


@dataclass
class ThoughtGenerated:
    """Published when ThinkingEngine / internal loop produces a proactive thought."""
    thought:  Any        # cognition.decision_engine.InternalThought
    priority: int = PRIORITY_IMPORTANT


@dataclass
class ResourceAlert:
    """
    Published on a pressure state TRANSITION (edge-triggered by ResourceMonitor,
    not on every poll tick while a metric stays elevated — see
    infra/resource_monitor.py). entering=True means pressure just started;
    entering=False means it just cleared.
    """
    metric:   str        # "vram_mb" | "ram_pct" | "cpu_pct"
    value:    float
    snapshot: dict
    entering: bool = True
    priority: int = PRIORITY_CRITICAL


@dataclass
class ActionSuggestion:
    """
    Proactive suggestion published by GoalManager when an actionable goal is due.
    Brain stores the action_text in its pending slot and speaks speak_text.
    On user confirmation ("yes"), Brain re-publishes action_text as a synthetic
    UserInput so it runs through the normal FastRouter → DecisionEngine pipeline.
    """
    goal_id:    str
    speak_text: str      # what Jarvis asks, e.g. "Sir, shall I open spotify?"
    action_text: str     # what to route on confirmation, e.g. "open spotify"
    created_at: float = field(default_factory=time.time)
    priority:   int = PRIORITY_IMPORTANT


@dataclass
class SystemEvent:
    """Lifecycle events (startup / shutdown). PerceptionAdapter subscribes to start loops."""
    kind:     str        # "startup" | "shutdown" | "error"
    detail:   str = ""
    priority: int = PRIORITY_NORMAL
