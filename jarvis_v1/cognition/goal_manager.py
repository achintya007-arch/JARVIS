"""
cognition/goal_manager.py — Persistent autonomous goal tracking.

Responsibilities:
  - Detect goal creation from user utterances (regex, no LLM)
  - Persist goals to SQLite across sessions
  - Proactively remind the user when idle and goals are overdue
  - Detect goal completion from user utterances
  - Input handling is driven by the Brain via handle_input() (sequential, no
    parallel bus race); reminders are published to the EventBus from run()

Design:
  - Owns its own aiosqlite connection (same DB file as ContextManager, WAL mode)
  - Mirrors brain._internal_loop() pattern for the reminder loop
  - Uses cognition/goal.py AgentState (V1) — the one with active_goals
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

import aiosqlite

from action.fast_router import FastRouter
from cognition.agent_state import AgentState
from cognition.goal import Goal
from core_common.event_bus import EventBus
from core_common.events import (
    PRIORITY_IMPORTANT,
    ActionSuggestion,
    SpeakRequest,
)

log = logging.getLogger("jarvis.goals")

# ── Goal creation patterns ────────────────────────────────────────────────────
# Each entry: (compiled_regex, priority, name_group_index)
_CREATION_PATTERNS: list[tuple[re.Pattern, int, int]] = [
    (re.compile(r"\bI have (?:an? )?exam (?:for|on|about) (.+?)(?:\s+(?:next|this|on)\b.*)?\s*$",
                re.IGNORECASE), 8, 1),
    (re.compile(r"\bdeadline (?:for|on) (.+?)\s*$", re.IGNORECASE), 8, 1),
    (re.compile(r"\bset (?:a )?goal to (.+?)\s*$", re.IGNORECASE), 6, 1),
    (re.compile(r"\bmy goal is (?:to )?(.+?)\s*$", re.IGNORECASE), 6, 1),
    (re.compile(r"\bremind me (?:to|about) (.+?)\s*$", re.IGNORECASE), 5, 1),
    (re.compile(r"\bdon'?t (?:let me )?forget (?:to )?(.+?)\s*$", re.IGNORECASE), 5, 1),
]
# Intentionally excluded — too many false positives from casual speech:
#   "I need/want/have to ..." → matches "I want to know the time", "I have to go"
#   "I'm going/trying/planning to ..." → matches "I'm going to grab lunch"

# Guard: these prefixes mean it's a FastRouter command, not a goal
_NOT_GOAL_RE = re.compile(
    r"^(?:open|close|start|launch|play|set a timer|search|find|show|get|take)\b",
    re.IGNORECASE,
)

# ── Goal completion patterns ──────────────────────────────────────────────────

# Phrases where the goal name comes AFTER the verb (group 1 = goal name)
_COMPLETION_SUFFIX_RE = re.compile(
    r"(?:"
    r"I(?:'ve| have)?\s+(?:finished|completed|done)\b\s+(?:the\s+)?"   # I finished/I've finished/I have done
    r"|I(?:'m| am)\s+done\s+with\s+(?:the\s+)?"                        # I'm done with
    r"|done\s+with\s+(?:the\s+)?"                                       # done with
    r"|finished\s+(?:the\s+)?"                                          # finished
    r"|completed\s+(?:the\s+)?"                                         # completed
    r"|cancel\s+(?:the\s+)?(?:my\s+)?(?:goal\s+|reminder\s+(?:for\s+)?)?"  # cancel (the) (goal|reminder)
    r"|drop\s+(?:the\s+)?(?:my\s+)?(?:goal\s+|reminder\s+(?:for\s+)?)?"    # drop (the) (goal|reminder)
    r"|remove\s+(?:the\s+)?(?:my\s+)?(?:goal\s+|reminder\s+(?:for\s+)?)?"  # remove (the) (goal|reminder)
    r"|forget\s+about\s+(?:the\s+)?"                                    # forget about
    r")(.+?)(?:\s+(?:goal|reminder|task))?$",
    re.IGNORECASE,
)

# Phrases where goal name comes IN THE MIDDLE: "mark X as done/complete"
_COMPLETION_MARK_RE = re.compile(
    r"\bmark\s+(?:the\s+)?(.+?)\s+(?:goal\s+|reminder\s+)?(?:as\s+)?(?:done|complete|completed|finished)\b",
    re.IGNORECASE,
)


class GoalManager:
    REMINDER_INTERVAL = 120    # seconds between loop ticks (2 min)
    IDLE_THRESHOLD    = 60     # user idle before reminders fire (1 min)
    REMINDER_COOLDOWN = 300    # min seconds before re-reminding same goal (5 min)

    def __init__(
        self,
        db_path: str | None,
        agent_state: AgentState,
        bus: EventBus,
        vault=None,
    ) -> None:
        # Two persistence backends, chosen by construction:
        #   vault given (V3)  → goals live in the Obsidian vault's goals.md
        #   vault None  (V2)  → goals live in SQLite at db_path
        self._db_path     = db_path
        self._state       = agent_state
        self._bus         = bus
        self._vault       = vault
        self._router      = FastRouter()        # stateless — used to test goal actionability
        self._db: aiosqlite.Connection | None = None
        self._running     = False

        # NOTE: goal detection is driven SYNCHRONOUSLY by the Brain via
        # handle_input() rather than a parallel UserInput subscription. Reacting
        # to the same event on two independent handlers raced: GoalManager pruned
        # the completed goal before the Brain could tell it had been handled,
        # producing a double response (goal notice + LLM reply). The bus is still
        # used for OUTBOUND reminders/suggestions from run().

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if self._vault is not None:
            self._vault_load()
        else:
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._create_schema()
            await self._load_goals_from_db()
        self._running = True
        log.info(
            "GoalManager ready (%s) — %d active goal(s)",
            "vault" if self._vault is not None else "sqlite",
            len(self._state.active_goals),
        )

    async def run(self) -> None:
        """Background reminder loop — mirrors brain._internal_loop."""
        while self._running:
            try:
                await asyncio.sleep(self.REMINDER_INTERVAL)
                await self._reminder_tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("GoalManager loop error: %s", e)

    async def close(self) -> None:
        self._running = False
        if self._db:
            await self._db.close()
            self._db = None

    # ── Goal CRUD ─────────────────────────────────────────────────────────────

    async def create_goal(self, name: str, priority: int, metadata: dict) -> Goal:
        goal = Goal(name=name, priority=priority, metadata=metadata)
        goal.metadata["goal_id"] = uuid.uuid4().hex[:8]

        # Check if the goal name maps to an executable tool via FastRouter.
        # If so, stash the original text so reminders can offer "Shall I X?".
        # Skip compound routes — suggestions are single-action only.
        route = self._router.route(name)
        if route is not None and not isinstance(route, list):
            goal.metadata["action_text"] = name
            log.info("Goal %r is actionable → route=%s", name, route.tool)

        self._state.add_goal(goal)          # deduplication handled in AgentState
        await self._persist_goal(goal)
        log.info("Goal created: %r (p=%d)", name, priority)
        return goal

    async def complete_goal(self, name: str) -> bool:
        goal = self._state.get_goal(name)
        if not goal:
            return False
        goal.complete()
        self._state.prune_goals()
        await self._update_goal_status(goal.metadata.get("goal_id", ""), "completed")
        log.info("Goal completed: %r", name)
        return True

    async def drop_goal(self, name: str) -> bool:
        goal = self._state.get_goal(name)
        if not goal:
            return False
        goal.drop()
        self._state.prune_goals()
        await self._update_goal_status(goal.metadata.get("goal_id", ""), "dropped")
        log.info("Goal dropped: %r", name)
        return True

    # ── Input handling (called synchronously by the Brain) ────────────────────

    async def handle_input(self, text: str) -> str | None:
        """
        Detect and act on a goal creation/completion in the utterance.

        Returns the sentence JARVIS should say if this was a goal action, or
        None if it wasn't (so the Brain proceeds to normal decision/LLM). Being
        the single, sequential owner of goal detection eliminates the double
        response that the old parallel bus subscription produced.
        """
        # Completion check first, so "cancel the X reminder" doesn't also create.
        matched = self._detect_goal_completion(text)
        if matched:
            completed = await self.complete_goal(matched)
            return (
                f"Got it, Sir. I've marked '{matched}' as complete."
                if completed else
                "I don't have an active goal matching that, Sir."
            )

        result = self._detect_goal_creation(text)
        if result:
            name, priority, metadata = result
            await self.create_goal(name, priority, metadata)
            return f"Goal noted, Sir. I'll keep track of '{name}' for you."

        return None

    # ── Detection ─────────────────────────────────────────────────────────────

    def _detect_goal_creation(self, text: str) -> tuple[str, int, dict] | None:
        stripped = text.strip().rstrip(".!?")
        if _NOT_GOAL_RE.match(stripped):
            return None
        for pattern, priority, group in _CREATION_PATTERNS:
            m = pattern.search(stripped)
            if m:
                name = m.group(group).strip().rstrip(".!?").lower()[:80]
                if len(name) < 3:
                    continue
                return name, priority, {}
        return None

    def _detect_goal_completion(self, text: str) -> str | None:
        """
        Return the matching active goal name if the utterance signals completion,
        or None if nothing matches.  Tries two syntactic forms:
          - suffix:  "done with <X>", "I've finished <X>", "cancel reminder for <X>"
          - middle:  "mark <X> as done"
        Both require ≥50 % word overlap with an active goal name.
        """
        stripped = text.strip()

        m = _COMPLETION_SUFFIX_RE.search(stripped)
        if m:
            phrase = m.group(m.lastindex).strip().lower()
            matched = self._fuzzy_match_goal(phrase)
            if matched:
                return matched

        m = _COMPLETION_MARK_RE.search(stripped)
        if m:
            phrase = m.group(1).strip().lower()
            return self._fuzzy_match_goal(phrase)

        return None

    def _fuzzy_match_goal(self, phrase: str) -> str | None:
        """
        Bidirectional word-overlap match against active goal names.

        Both directions must pass their threshold:
          - ≥50 % of goal words appear in the phrase   (goal coverage)
          - ≥30 % of phrase words match the goal name  (phrase specificity)

        The second check prevents "done with the X" from falsely completing
        a goal named "with project" just because "with" appears in both.
        """
        phrase_words = set(phrase.lower().split())
        best_name, best_ratio = None, 0.0
        for g in self._state.active_goals:
            if not g.is_active:
                continue
            goal_words = set(g.name.lower().split())
            overlap            = len(phrase_words & goal_words)
            goal_coverage      = overlap / max(len(goal_words),   1)
            phrase_specificity = overlap / max(len(phrase_words), 1)
            if goal_coverage >= 0.5 and phrase_specificity >= 0.3 and goal_coverage > best_ratio:
                best_ratio = goal_coverage
                best_name  = g.name
        return best_name

    # ── Reminder ─────────────────────────────────────────────────────────────

    async def _reminder_tick(self) -> None:
        if not self._state.has_active_goals:
            return
        if self._state.idle_seconds < self.IDLE_THRESHOLD:
            return

        candidates = [
            g for g in self._state.active_goals
            if g.is_active and g.seconds_since_last_action >= self.REMINDER_COOLDOWN
        ]
        if not candidates:
            return

        best = max(candidates, key=lambda g: g.priority)
        best.touch()

        action_text = best.metadata.get("action_text")
        if action_text:
            # Actionable goal → ask for confirmation via ActionSuggestion.
            # Brain handles the pending slot + speech + yes/no routing.
            self._bus.publish(ActionSuggestion(
                goal_id     = best.metadata.get("goal_id", ""),
                speak_text  = f"Sir, shall I {action_text}?",
                action_text = action_text,
            ))
            log.info("Suggestion fired for goal: %r", best.name)
        else:
            self._bus.publish(SpeakRequest(
                text     = f"Sir, a reminder — you have an active goal: {best.name}.",
                priority = PRIORITY_IMPORTANT,
            ))
            log.info("Reminder fired for goal: %r", best.name)

    # ── SQLite helpers ────────────────────────────────────────────────────────

    async def _create_schema(self) -> None:
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS goals (
                goal_id       TEXT    PRIMARY KEY,
                name          TEXT    NOT NULL,
                priority      INTEGER NOT NULL DEFAULT 5,
                status        TEXT    NOT NULL DEFAULT 'active',
                created_at    REAL    NOT NULL,
                metadata      TEXT    NOT NULL DEFAULT '{}',
                last_acted_at REAL    NOT NULL,
                act_count     INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
        """)
        await self._db.commit()

    async def _persist_goal(self, goal: Goal) -> None:
        if self._vault is not None:
            self._vault_upsert(goal)
            return
        goal_id = goal.metadata.get("goal_id", uuid.uuid4().hex[:8])
        await self._db.execute(
            """INSERT OR REPLACE INTO goals
               (goal_id, name, priority, status, created_at, metadata, last_acted_at, act_count)
               VALUES (?,?,?,?,?,?,?,?)""",
            (goal_id, goal.name, goal.priority, goal.status,
             goal.created_at, json.dumps(goal.metadata),
             goal.last_acted_at, goal.act_count),
        )
        await self._db.commit()

    async def _update_goal_status(self, goal_id: str, status: str) -> None:
        if not goal_id:
            return
        if self._vault is not None:
            self._vault_set_status(goal_id, status)
            return
        await self._db.execute(
            "UPDATE goals SET status=? WHERE goal_id=?", (status, goal_id)
        )
        await self._db.commit()

    async def _load_goals_from_db(self) -> None:
        async with self._db.execute(
            "SELECT name, priority, status, created_at, metadata, last_acted_at, act_count "
            "FROM goals WHERE status='active'"
        ) as cursor:
            rows = await cursor.fetchall()
        for name, priority, status, created_at, metadata_json, last_acted_at, act_count in rows:
            metadata = json.loads(metadata_json or "{}")
            goal = Goal(
                name          = name,
                priority      = priority,
                created_at    = created_at,
                status        = status,
                metadata      = metadata,
                last_acted_at = last_acted_at,
                act_count     = act_count,
            )
            self._state.add_goal(goal)
        log.info("Loaded %d active goal(s) from DB", len(rows))

    # ── Vault (goals.md) helpers ──────────────────────────────────────────────
    # Each goal is one Obsidian task line, with round-trip metadata in a trailing
    # HTML comment (invisible in rendered Obsidian):
    #   - [ ] finish the report <!--jarvis {"goal_id":"ab12",...}-->

    _GOAL_LINE_RE = re.compile(
        r"^- \[([ xX])\]\s*(?P<name>.+?)\s*<!--\s*jarvis\s*(?P<json>\{.*\})\s*-->\s*$"
    )

    def _vault_render_line(self, goal: Goal) -> str:
        box = "x" if not goal.is_active else " "
        payload = {
            "goal_id":       goal.metadata.get("goal_id", ""),
            "priority":      goal.priority,
            "status":        goal.status,
            "created_at":    goal.created_at,
            "last_acted_at": goal.last_acted_at,
            "act_count":     goal.act_count,
            "metadata":      goal.metadata,
        }
        return f"- [{box}] {goal.name} <!--jarvis {json.dumps(payload, separators=(',', ':'))}-->"

    def _vault_read_lines(self) -> list[str]:
        text = self._vault.read(self._vault.goals_path)
        return text.splitlines() if text else []

    def _vault_write_lines(self, lines: list[str]) -> None:
        # Rewrite with a clean, consistent header. Any prior heading/blank lines
        # are dropped and re-added so goals.md stays tidy in Obsidian.
        tasks = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
        body = "# Goals\n\n" + ("\n".join(tasks) + "\n" if tasks else "")
        self._vault.write(self._vault.goals_path, body)

    def _vault_load(self) -> None:
        for line in self._vault_read_lines():
            m = self._GOAL_LINE_RE.match(line.strip())
            if not m:
                continue
            try:
                data = json.loads(m.group("json"))
            except json.JSONDecodeError:
                continue
            if data.get("status", "active") != "active":
                continue
            goal = Goal(
                name          = m.group("name").strip(),
                priority      = int(data.get("priority", 5)),
                created_at    = float(data.get("created_at", 0.0)),
                status        = "active",
                metadata      = data.get("metadata", {}) or {"goal_id": data.get("goal_id", "")},
                last_acted_at = float(data.get("last_acted_at", 0.0)),
                act_count     = int(data.get("act_count", 0)),
            )
            goal.metadata.setdefault("goal_id", data.get("goal_id", ""))
            self._state.add_goal(goal)

    def _vault_upsert(self, goal: Goal) -> None:
        gid = goal.metadata.get("goal_id", "")
        lines = self._vault_read_lines()
        new_line = self._vault_render_line(goal)
        replaced = False
        for i, line in enumerate(lines):
            m = self._GOAL_LINE_RE.match(line.strip())
            if m:
                try:
                    if json.loads(m.group("json")).get("goal_id") == gid:
                        lines[i] = new_line
                        replaced = True
                        break
                except json.JSONDecodeError:
                    pass
        if not replaced:
            lines.append(new_line)
        self._vault_write_lines([ln for ln in lines if ln.strip()])

    def _vault_set_status(self, goal_id: str, status: str) -> None:
        lines = self._vault_read_lines()
        out: list[str] = []
        for line in lines:
            m = self._GOAL_LINE_RE.match(line.strip())
            if not m:
                out.append(line)
                continue
            try:
                data = json.loads(m.group("json"))
            except json.JSONDecodeError:
                out.append(line)
                continue
            if data.get("goal_id") != goal_id:
                out.append(line)
                continue
            if status == "dropped":
                continue  # remove the line entirely
            data["status"] = status
            box = "x" if status == "completed" else " "
            out.append(f"- [{box}] {m.group('name')} <!--jarvis {json.dumps(data, separators=(',', ':'))}-->")
        self._vault_write_lines([ln for ln in out if ln.strip()])
