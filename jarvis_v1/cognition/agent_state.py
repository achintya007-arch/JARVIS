"""
AgentState — the persistent internal mind of JARVIS.

Tracks what the assistant knows about the user, the system, and
the current moment — without calling the LLM.

Updated on every event via update_from_input() and update_from_system().
Provides snapshot() for the DecisionEngine to read at any point.

Design:
  - All updates are synchronous and O(1) — never a bottleneck
  - State is in-memory; survives the session, not across reboots
    (ChromaDB handles cross-session memory)
  - No LLM involvement — pure signal processing on text + metrics
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

# Import Goal for the unified goal-management methods added below.
# goal.py is standalone (no back-import), so no circular dependency.
from cognition.goal import Goal

# ── Type aliases ──────────────────────────────────────────────────────────────

UserState   = Literal["neutral", "stressed", "focused", "tired", "happy", "frustrated"]
SystemState = Literal["normal", "high_load", "critical"]
Intent      = Literal["query", "command", "chitchat", "complaint", "gratitude", "unknown"]


# ── Signal tables — keyword → state (no LLM needed) ──────────────────────────

_STRESS_SIGNALS   = re.compile(
    r"\b(stress|overwhelm|too much|can't handle|anxious|anxiet|panic|urgent|emergency|deadline)\b",
    re.IGNORECASE,
)
_TIRED_SIGNALS    = re.compile(
    r"\b(tired|exhausted|sleepy|drowsy|fatigue|can'?t sleep|up all night|no sleep)\b",
    re.IGNORECASE,
)
_HAPPY_SIGNALS    = re.compile(
    r"\b(great|awesome|amazing|perfect|love it|excellent|fantastic|brilliant|nailed it)\b",
    re.IGNORECASE,
)
_FRUSTRATED_SIGNALS = re.compile(
    r"\b(stupid|broken|doesn'?t work|useless|wrong|again|still not|why won'?t|ugh|argh)\b",
    re.IGNORECASE,
)
_FOCUSED_SIGNALS  = re.compile(
    r"\b(focus|concentrate|do not disturb|working on|building|coding|writing|deep work)\b",
    re.IGNORECASE,
)

_COMMAND_SIGNALS  = re.compile(
    r"^(open|close|run|set|start|stop|play|pause|find|search|show|get|take|type|click|launch)\b",
    re.IGNORECASE,
)
_QUESTION_SIGNALS = re.compile(
    r"^(what|who|where|when|why|how|is|are|can|could|would|should|do|does|did)\b",
    re.IGNORECASE,
)
_GRATITUDE_SIGNALS = re.compile(
    r"\b(thank|thanks|cheers|appreciate|good job|well done)\b",
    re.IGNORECASE,
)
_COMPLAINT_SIGNALS = re.compile(
    r"\b(wrong|incorrect|that'?s not|you said|but you|why did you|mistake)\b",
    re.IGNORECASE,
)

# Topic extraction — pull the most meaningful noun phrase from a query
_TOPIC_RE = re.compile(
    r"\b(the\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*|[a-z]{4,}(?:\s+[a-z]{4,})?)\b"
)


# ── StateSnapshot — what DecisionEngine reads ────────────────────────────────

@dataclass(frozen=True)
class StateSnapshot:
    user_state:           UserState
    system_state:         SystemState
    current_focus:        str | None
    last_interaction_age: float          # seconds since last user input
    recent_topics:        tuple[str, ...]
    conversation_intent:  Intent
    interaction_count:    int            # total this session
    idle_seconds:         float          # seconds since last interaction


# ── AgentState ────────────────────────────────────────────────────────────────

class AgentState:
    """
    The internal mind. Single source of truth for what JARVIS knows right now.

    Usage:
        state = AgentState()
        state.update_from_input("I'm really tired, can you set a timer?")
        state.update_from_system(cpu_pct=45.0, ram_pct=62.0)
        snap = state.snapshot()
    """

    def __init__(self, topic_history_len: int = 8):
        self._user_state:          UserState   = "neutral"
        self._system_state:        SystemState = "normal"
        self._current_focus:       str | None = None
        self._last_interaction_ts: float = time.time()
        self._recent_topics:       deque[str] = deque(maxlen=topic_history_len)
        self._intent:              Intent = "unknown"
        self._interaction_count:   int = 0

        # Mood smoothing — don't flip state on one word
        self._mood_streak: dict[str, int] = {}

        # Hysteresis for system state — require 2 consecutive ticks above threshold
        # before flipping, preventing TTS thrash at boundary CPU/RAM levels.
        self._system_state_streak: int = 0
        self._system_state_candidate: SystemState = "normal"

        # Goal tracking (unified from cognition/goal.py AgentState)
        self.active_goals: list[Goal] = []

    # ── Public update API ─────────────────────────────────────────────────────

    def update_from_input(self, text: str) -> None:
        """
        Called on every user utterance. Pure signal processing — no LLM.
        Updates user_state, intent, recent_topics, current_focus.
        """
        self._last_interaction_ts = time.time()
        self._interaction_count  += 1

        self._intent        = self._infer_intent(text)
        self._user_state    = self._infer_user_state(text)
        self._current_focus = self._infer_focus(text) or self._current_focus

        topic = self._extract_topic(text)
        if topic:
            self._recent_topics.append(topic)

    def update_from_system(
        self,
        cpu_pct: float = 0.0,
        ram_pct: float = 0.0,
        vram_pct: float = 0.0,
    ) -> None:
        """
        Called by ResourceMonitor on each poll. Updates system_state with
        hysteresis: state only flips after 2 consecutive ticks above threshold,
        preventing TTS thrash when metrics hover at a boundary.
        """
        if cpu_pct > 90 or ram_pct > 90 or vram_pct > 92:
            candidate: SystemState = "critical"
        elif cpu_pct > 70 or ram_pct > 75 or vram_pct > 80:
            candidate = "high_load"
        else:
            candidate = "normal"

        if candidate == self._system_state_candidate:
            self._system_state_streak += 1
        else:
            self._system_state_candidate = candidate
            self._system_state_streak = 1

        # Flip immediately into a lower-pressure state; require 2 ticks for escalation.
        if candidate == "normal" or self._system_state_streak >= 2:
            self._system_state = candidate

    def clear_focus(self) -> None:
        """Call when user switches topic."""
        self._current_focus = None

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self) -> StateSnapshot:
        now = time.time()
        return StateSnapshot(
            user_state           = self._user_state,
            system_state         = self._system_state,
            current_focus        = self._current_focus,
            last_interaction_age = now - self._last_interaction_ts,
            recent_topics        = tuple(self._recent_topics),
            conversation_intent  = self._intent,
            interaction_count    = self._interaction_count,
            idle_seconds         = now - self._last_interaction_ts,
        )

    # ── Internal signal processing ────────────────────────────────────────────

    def _infer_user_state(self, text: str) -> UserState:
        """
        Smooth mood detection — requires 2 consecutive signals before switching.
        Prevents "I'm not tired" from immediately flipping state to tired.
        """
        candidates: dict[UserState, re.Pattern] = {
            "stressed":   _STRESS_SIGNALS,
            "tired":      _TIRED_SIGNALS,
            "happy":      _HAPPY_SIGNALS,
            "frustrated": _FRUSTRATED_SIGNALS,
            "focused":    _FOCUSED_SIGNALS,
        }

        detected: UserState | None = None
        for state, pattern in candidates.items():
            if pattern.search(text):
                detected = state
                break

        if detected is None:
            # Decay streak — one neutral input softens but doesn't reset
            for k in list(self._mood_streak.keys()):
                self._mood_streak[k] = max(0, self._mood_streak[k] - 1)
            # Only return neutral if no streak is active
            active = [k for k, v in self._mood_streak.items() if v >= 1]
            return active[0] if active else "neutral"  # type: ignore[return-value]

        # Increment streak
        self._mood_streak[detected] = self._mood_streak.get(detected, 0) + 1

        # Fix #3: require 2 consecutive hits before flipping mood (streak smoothing).
        # >= 1 fired on the very first signal, defeating the purpose of the streak.
        if self._mood_streak[detected] >= 2:
            return detected

        return self._user_state  # keep current until streak confirmed

    def _infer_intent(self, text: str) -> Intent:
        if _COMMAND_SIGNALS.search(text):
            return "command"
        if _QUESTION_SIGNALS.search(text):
            return "query"
        if _GRATITUDE_SIGNALS.search(text):
            return "gratitude"
        if _COMPLAINT_SIGNALS.search(text):
            return "complaint"
        if len(text.split()) < 6:
            return "chitchat"
        return "unknown"

    def _infer_focus(self, text: str) -> str | None:
        """
        Detect if user is explicitly entering a focus mode.
        Returns the focus topic, or None.
        """
        m = re.search(
            r"\b(?:working on|focused on|building|coding|writing|studying)\s+(.+?)(?:\.|$)",
            text, re.IGNORECASE
        )
        if m:
            return m.group(1).strip()[:60]
        if _FOCUSED_SIGNALS.search(text):
            return "deep work"
        return None

    def _extract_topic(self, text: str) -> str | None:
        """
        Pull a short topic string from the input for recent_topics.
        Prefers noun phrases of 2-4 words. Falls back to longest single word.
        """
        # Strip question/command words from front
        cleaned = re.sub(
            r"^(what|who|where|when|why|how|can you|please|could you|open|run|set|play)\s+",
            "", text.strip(), flags=re.IGNORECASE
        ).strip()

        words = cleaned.split()
        if not words:
            return None

        # Short meaningful phrase
        phrase = " ".join(words[:3]).lower()
        phrase = re.sub(r"[^a-z0-9 ]", "", phrase).strip()
        return phrase if len(phrase) > 3 else None

    # ── Goal management ───────────────────────────────────────────────────────

    def add_goal(self, goal: Goal) -> None:
        """
        Add a goal, or refresh an existing active goal with the same name.
        On a duplicate we merge metadata and keep the higher priority rather
        than dropping the new goal — a re-stated goal may carry new context
        (e.g. an exam that gains a date).
        """
        for g in self.active_goals:
            if g.name == goal.name and g.is_active:
                g.metadata.update(goal.metadata)
                g.priority = max(g.priority, goal.priority)
                return
        self.active_goals.append(goal)

    def get_goal(self, name: str) -> Goal | None:
        """Return the first active goal whose name matches (case-insensitive)."""
        name_lower = name.lower()
        for g in self.active_goals:
            if g.is_active and g.name.lower() == name_lower:
                return g
        return None

    def prune_goals(self) -> None:
        """Remove completed / dropped goals from the in-memory list."""
        self.active_goals = [g for g in self.active_goals if g.is_active]

    @property
    def has_active_goals(self) -> bool:
        return any(g.is_active for g in self.active_goals)

    @property
    def top_goal(self) -> Goal | None:
        """Highest-priority active goal, or None."""
        candidates = [g for g in self.active_goals if g.is_active]
        return max(candidates, key=lambda g: g.priority) if candidates else None

    @property
    def idle_seconds(self) -> float:
        return time.time() - self._last_interaction_ts

    @property
    def is_idle(self) -> bool:
        return self.idle_seconds > 60

    # ── Repr ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        s = self.snapshot()
        return (
            f"AgentState(user={s.user_state}, sys={s.system_state}, "
            f"intent={s.conversation_intent}, focus={s.current_focus}, "
            f"topics={list(s.recent_topics)[-3:]})"
        )
