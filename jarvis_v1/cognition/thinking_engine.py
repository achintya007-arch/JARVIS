"""
thinking_engine.py — JARVIS internal cognition layer.

Pipeline:
  state + memory → generate_thought() → Thought
  Thought(importance > 0.6, requires_action=True) → push to internal queue
  Assistant drains internal queue between user turns

Thought generation is always:
  Step 1: state-based rules  (< 1ms, always run)
  Step 2: goal-based rules   (< 1ms, always run)
  Step 3: memory signals     (async, ~10ms)
  Step 4: LLM fallback       (only if steps 1–3 produce nothing)

Design rules:
  - Steps 1–3 are deterministic.  No randomness, no probability gates.
  - LLM fallback is rare (estimated < 5% of loop iterations).
  - The loop never blocks the main event loop.
  - All goal mutations go through AgentState — ThinkingEngine never
    writes to goals directly, only reads and calls state methods.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from cognition.goal import Goal
from cognition.agent_state import AgentState

if TYPE_CHECKING:
    from cognition.context_manager import ContextManager
    from cognition.llm_client import LLMClient

log = logging.getLogger("jarvis.thinking")


# ── Thought ───────────────────────────────────────────────────────────────────

ThoughtType = str   # "reflection" | "reminder" | "suggestion" | "check" | "goal_progress" | "system_warning"


@dataclass
class Thought:
    type:            ThoughtType
    content:         str
    importance:      float          # 0.0 – 1.0
    requires_action: bool
    spoken_text:     str = ""       # what JARVIS should say if acted on
    goal_name:       str = ""       # which goal triggered this, if any
    source:          str = "rules"  # "rules" | "memory" | "llm"

    def __repr__(self) -> str:
        return (
            f"Thought({self.type!r}, "
            f"importance={self.importance:.2f}, "
            f"act={self.requires_action}, "
            f"src={self.source!r})"
        )


# Sentinel: nothing worth acting on this cycle
_NULL_THOUGHT = Thought(
    type="reflection",
    content="nothing notable",
    importance=0.0,
    requires_action=False,
    source="rules",
)


# ── Goal detection from user input ────────────────────────────────────────────

# Each entry: (regex, goal_name, priority, metadata_extractor)
# metadata_extractor receives the re.Match and returns a dict.

def _no_meta(_m: re.Match) -> dict:
    return {}


_GOAL_PATTERNS: list[tuple[re.Pattern, str, int, callable]] = [
    (
        re.compile(r"\b(exam|test|quiz|assignment)\b.{0,40}(tomorrow|today|tonight|\d+ days?)", re.I),
        "exam_preparation",
        8,
        lambda m: {"trigger_phrase": m.group(0)[:80]},
    ),
    (
        re.compile(r"\b(deadline|due|submit|submission)\b.{0,30}(tomorrow|today|\d+ days?)", re.I),
        "deadline_tracking",
        8,
        lambda m: {"trigger_phrase": m.group(0)[:80]},
    ),
    (
        re.compile(r"\b(remind me|reminder).{0,30}(water|drink|hydrat)", re.I),
        "hydration_reminder",
        5,
        lambda _: {"interval_minutes": 30},
    ),
    (
        re.compile(r"\b(remind me|reminder)\b", re.I),
        "custom_reminder",
        6,
        lambda m: {"trigger_phrase": m.group(0)[:80]},
    ),
    (
        re.compile(r"\b(i'?m tired|feeling tired|so tired|exhausted)\b", re.I),
        "improve_user_state",
        4,
        lambda _: {"reason": "tired"},
    ),
    (
        re.compile(r"\b(i'?m stressed|feeling stressed|anxious|overwhelmed)\b", re.I),
        "improve_user_state",
        5,
        lambda _: {"reason": "stressed"},
    ),
    (
        re.compile(r"\b(learn|study|practice|get better at)\s+(?P<topic>[\w\s]{2,30})", re.I),
        "learning_goal",
        5,
        lambda m: {"topic": m.group("topic").strip()},
    ),
    (
        re.compile(r"\b(lose weight|exercise|work ?out|get fit|diet)\b", re.I),
        "health_goal",
        4,
        _no_meta,
    ),
    (
        re.compile(r"\b(build|create|make|finish|complete)\s+(?P<project>[\w\s]{2,30})", re.I),
        "project_goal",
        6,
        lambda m: {"project": m.group("project").strip()},
    ),
]


def detect_goals_from_input(text: str, state: AgentState) -> list[Goal]:
    """
    Parse user input for goal signals.
    Returns new Goal objects — caller decides whether to add them to state.
    Called from DecisionEngine after every user input.
    """
    goals: list[Goal] = []
    for pattern, name, priority, extractor in _GOAL_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                meta = extractor(m)
            except Exception:
                meta = {}
            goals.append(Goal(name=name, priority=priority, metadata=meta))
    return goals


# ── Memory signal extraction ──────────────────────────────────────────────────

# Patterns that indicate time-sensitive commitments in memory strings
_DEADLINE_RE = re.compile(
    r"\b(tomorrow|today|tonight|in \d+ (hours?|days?|minutes?)|at \d+[:\d]*\s*(am|pm)?)\b",
    re.I,
)
_COMMITMENT_RE = re.compile(
    r"\b(exam|test|deadline|meeting|appointment|submit|due|remind)\b",
    re.I,
)


def _has_time_signal(memory_fragment: str) -> bool:
    return bool(_DEADLINE_RE.search(memory_fragment) and _COMMITMENT_RE.search(memory_fragment))


# ── ThinkingEngine ────────────────────────────────────────────────────────────

class ThinkingEngine:
    """
    Runs a background loop that generates internal thoughts.
    High-importance actionable thoughts are pushed to internal_queue
    so the assistant can speak them between user turns.

    Wiring (in Assistant.__init__):
        self.thinking = ThinkingEngine(
            state=self.state,
            context=self.context,
            llm=self.llm,
            internal_queue=self._internal_queue,
        )

    Wiring (in Assistant.start()):
        asyncio.create_task(self.thinking.run())
    """

    # How long between loop iterations (seconds)
    LOOP_INTERVAL = 5

    # Idle threshold before generating a check-in thought
    IDLE_CHECKIN_AFTER = 180   # 3 minutes

    # Goal action cooldowns — don't repeat the same goal thought too soon
    GOAL_COOLDOWN = {
        "exam_preparation":  600,   # 10 min
        "hydration_reminder": 1800,  # 30 min
        "deadline_tracking":  900,   # 15 min
        "improve_user_state": 300,   # 5 min
        "custom_reminder":    600,
        "learning_goal":     1200,
        "health_goal":       3600,
        "project_goal":       900,
    }
    DEFAULT_COOLDOWN = 600

    def __init__(
        self,
        state: AgentState,
        context: "ContextManager",
        llm: "LLMClient",
        internal_queue: asyncio.Queue,
    ) -> None:
        self._state   = state
        self._context = context
        self._llm     = llm
        self._queue   = internal_queue
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        log.info("ThinkingEngine started (interval=%ds)", self.LOOP_INTERVAL)
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("ThinkingEngine tick error: %s", e)
            await asyncio.sleep(self.LOOP_INTERVAL)

    def stop(self) -> None:
        self._running = False

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        self._state.prune_goals()

        # Recall memory for this cycle (fast — ChromaDB already embedded)
        query = self._state.current_focus or "recent activity"
        try:
            memories: list[str] = await self._context.recall_for_thinking(query)
        except Exception:
            memories = []

        thought = await self.generate_thought(self._state, memories)

        log.debug("Thought: %s", thought)

        if thought.importance >= 0.6 and thought.requires_action and thought.spoken_text:
            await self._queue.put(thought)
            # Mark the goal as acted on so cooldown applies
            if thought.goal_name:
                g = self._state.get_goal(thought.goal_name)
                if g:
                    g.touch()

    # ── Thought generation ────────────────────────────────────────────────────

    async def generate_thought(
        self,
        state: AgentState,
        memories: list[str],
    ) -> Thought:
        """
        Four-step thought generation.
        Returns the highest-importance thought found, or _NULL_THOUGHT.
        """
        candidates: list[Thought] = []

        # Step 1 — State-based rules
        t = self._state_based_thought(state)
        if t:
            candidates.append(t)

        # Step 2 — Goal-based rules
        t = self._goal_based_thought(state)
        if t:
            candidates.append(t)

        # Step 3 — Memory signals
        t = self._memory_based_thought(memories, state)
        if t:
            candidates.append(t)

        if candidates:
            return max(candidates, key=lambda x: x.importance)

        # Step 4 — LLM fallback (only when truly idle and memory is silent)
        if state.idle_seconds > self.IDLE_CHECKIN_AFTER:
            t = await self._llm_fallback_thought(state, memories)
            if t:
                return t

        return _NULL_THOUGHT

    # ── Step 1: State-based ───────────────────────────────────────────────────

    def _state_based_thought(self, state: AgentState) -> Optional[Thought]:
        if state.system_state == "critical":
            return Thought(
                type="system_warning",
                content="system under critical load",
                importance=0.95,
                requires_action=True,
                spoken_text="Sir, system resources are critically low. You may want to close some applications.",
                source="rules",
            )

        if state.system_state == "high_load":
            return Thought(
                type="system_warning",
                content="system under high load",
                importance=0.7,
                requires_action=True,
                spoken_text="Heads up, Sir — CPU and RAM usage are elevated.",
                source="rules",
            )

        if state.user_state == "stressed":
            return Thought(
                type="suggestion",
                content="user appears stressed",
                importance=0.65,
                requires_action=True,
                spoken_text="You seem stressed, Sir. A short break might help. I can put on something ambient if you'd like.",
                source="rules",
            )

        if state.user_state == "tired":
            return Thought(
                type="suggestion",
                content="user is tired",
                importance=0.6,
                requires_action=True,
                spoken_text="You mentioned being tired, Sir. Consider a rest. I'll keep watch.",
                source="rules",
            )

        return None

    # ── Step 2: Goal-based ────────────────────────────────────────────────────

    def _goal_based_thought(self, state: AgentState) -> Optional[Thought]:
        if not state.has_active_goals:
            return None

        best: Optional[Thought] = None

        for goal in sorted(state.active_goals, key=lambda g: -g.priority):
            if not goal.is_active:
                continue

            cooldown = self.GOAL_COOLDOWN.get(goal.name, self.DEFAULT_COOLDOWN)
            if goal.seconds_since_last_action < cooldown:
                continue

            t = self._thought_for_goal(goal, state)
            if t and (best is None or t.importance > best.importance):
                best = t

        return best

    def _thought_for_goal(self, goal: Goal, state: AgentState) -> Optional[Thought]:
        name = goal.name

        if name == "hydration_reminder":
            interval = goal.metadata.get("interval_minutes", 30) * 60
            if goal.seconds_since_last_action >= interval:
                return Thought(
                    type="reminder",
                    content="hydration reminder due",
                    importance=0.7,
                    requires_action=True,
                    spoken_text="Sir, it's time to drink some water.",
                    goal_name=name,
                    source="rules",
                )

        elif name == "exam_preparation":
            phrase = goal.metadata.get("trigger_phrase", "your upcoming exam")
            return Thought(
                type="goal_progress",
                content="exam preparation check-in",
                importance=0.75,
                requires_action=True,
                spoken_text=f"Sir, a reminder about {phrase}. Would you like me to help you prepare?",
                goal_name=name,
                source="rules",
            )

        elif name == "deadline_tracking":
            phrase = goal.metadata.get("trigger_phrase", "your deadline")
            return Thought(
                type="reminder",
                content="deadline approaching",
                importance=0.8,
                requires_action=True,
                spoken_text=f"Sir, {phrase} is coming up. Are you on track?",
                goal_name=name,
                source="rules",
            )

        elif name == "improve_user_state":
            reason = goal.metadata.get("reason", "stress")
            # Drop this goal after 30 minutes — temporary
            if goal.age_seconds > 1800:
                goal.complete()
                return None
            if reason == "tired":
                text = "Sir, you mentioned being tired. Have you had a chance to rest?"
            else:
                text = "Sir, are you feeling any better? Let me know if there's anything I can help with."
            return Thought(
                type="check",
                content="user state check-in",
                importance=0.62,
                requires_action=True,
                spoken_text=text,
                goal_name=name,
                source="rules",
            )

        elif name == "custom_reminder":
            phrase = goal.metadata.get("trigger_phrase", "something you asked me to remember")
            return Thought(
                type="reminder",
                content="custom reminder",
                importance=0.72,
                requires_action=True,
                spoken_text=f"Sir, you asked me to remind you — {phrase}.",
                goal_name=name,
                source="rules",
            )

        elif name in ("learning_goal", "project_goal"):
            topic = goal.metadata.get("topic") or goal.metadata.get("project", "your current project")
            return Thought(
                type="goal_progress",
                content=f"check in on {topic}",
                importance=0.6,
                requires_action=True,
                spoken_text=f"Sir, how's {topic} coming along? Shall I help?",
                goal_name=name,
                source="rules",
            )

        return None

    # ── Step 3: Memory-based ──────────────────────────────────────────────────

    def _memory_based_thought(
        self,
        memories: list[str],
        state: AgentState,
    ) -> Optional[Thought]:
        for fragment in memories:
            if _has_time_signal(fragment):
                # Extract short label from the fragment
                label = fragment[:120].replace("\n", " ").strip()
                return Thought(
                    type="reminder",
                    content=f"memory signal: {label[:60]}",
                    importance=0.78,
                    requires_action=True,
                    spoken_text=f"Sir, I found something relevant in memory: {label[:100]}",
                    source="memory",
                )
        return None

    # ── Step 4: LLM fallback ──────────────────────────────────────────────────

    async def _llm_fallback_thought(
        self,
        state: AgentState,
        memories: list[str],
    ) -> Optional[Thought]:
        """
        Call LLM only when rules found nothing and the user has been idle.
        Prompt is tiny and structured — expects a single short sentence or "none".
        """
        if not memories and not state.has_active_goals:
            return None     # nothing to reason about

        memory_block = "\n".join(f"- {m[:100]}" for m in memories[:3])
        goals_block  = "\n".join(f"- {g.name}" for g in state.active_goals if g.is_active)

        prompt = (
            "You are JARVIS. The user has been idle for a while.\n"
            f"User state: {state.user_state}\n"
            f"Recent topics: {', '.join(list(state.recent_topics)[-3:]) or 'none'}\n"
            f"Active goals:\n{goals_block or 'none'}\n"
            f"Memory context:\n{memory_block or 'none'}\n\n"
            "Should JARVIS say something proactively right now?\n"
            "If yes, reply with one short natural spoken sentence (no markdown).\n"
            "If no, reply with exactly: none"
        )

        try:
            reply = await self._llm.complete([{"role": "user", "content": prompt}])
            reply = reply.strip()
            if reply.lower() in ("none", "", "no", "no.", "not now."):
                return None
            return Thought(
                type="suggestion",
                content="llm proactive thought",
                importance=0.62,
                requires_action=True,
                spoken_text=reply[:200],
                source="llm",
            )
        except Exception as e:
            log.debug("LLM fallback thought failed: %s", e)
            return None