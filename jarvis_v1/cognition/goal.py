"""
cognition/goal.py — Goal dataclass and related types.

AgentState has been unified into cognition/agent_state.py.
This module is now a focused data model for persistent objectives.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

# ── Types ─────────────────────────────────────────────────────────────────────

UserState   = Literal["neutral", "stressed", "focused", "tired", "happy"]
SystemState = Literal["normal", "high_load", "critical"]
GoalStatus  = Literal["active", "completed", "dropped"]


# ── Goal ──────────────────────────────────────────────────────────────────────

@dataclass
class Goal:
    """
    A persistent objective JARVIS is tracking.

    metadata stores goal-specific context, e.g.:
      {"exam_date": "2025-04-10", "topic": "physics"}
      {"interval_minutes": 30}   # for hydration reminders
    """
    name:       str
    priority:   int                    # 1 (low) – 10 (critical)
    created_at: float = field(default_factory=time.time)
    status:     GoalStatus = "active"
    metadata:   dict = field(default_factory=dict)

    # Runtime — not persisted
    # 0.0 is the "never reminded" sentinel: seconds_since_last_action will be
    # huge, so the first reminder fires as soon as the idle threshold is met.
    # After the first touch(), last_acted_at = now and normal cooldown applies.
    last_acted_at: float = 0.0
    act_count:     int   = 0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def complete(self) -> None:
        self.status = "completed"

    def drop(self) -> None:
        self.status = "dropped"

    def touch(self) -> None:
        """Record that JARVIS acted on this goal."""
        self.last_acted_at = time.time()
        self.act_count += 1

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def seconds_since_last_action(self) -> float:
        return time.time() - self.last_acted_at

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def __repr__(self) -> str:
        return f"Goal({self.name!r}, p={self.priority}, {self.status})"
