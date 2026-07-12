"""Unit tests for the unified AgentState (mood smoothing, goals, idle clock)."""

import time

from cognition.agent_state import AgentState
from cognition.goal import Goal


class TestMoodStreak:
    def test_single_signal_does_not_flip(self):
        # Audit fix #3: streak must require 2 consecutive hits, not 1.
        s = AgentState()
        s.update_from_input("I'm so tired")
        assert s.snapshot().user_state == "neutral"

    def test_two_signals_flip(self):
        s = AgentState()
        s.update_from_input("I'm so tired")
        s.update_from_input("still exhausted")
        assert s.snapshot().user_state == "tired"


class TestIntent:
    def test_command(self):
        s = AgentState()
        s.update_from_input("open the door")
        assert s.snapshot().conversation_intent == "command"

    def test_query(self):
        s = AgentState()
        s.update_from_input("what is the capital of France")
        assert s.snapshot().conversation_intent == "query"

    def test_gratitude(self):
        s = AgentState()
        s.update_from_input("thanks a lot")
        assert s.snapshot().conversation_intent == "gratitude"


class TestGoals:
    def test_add_and_get(self):
        s = AgentState()
        s.add_goal(Goal(name="finish report", priority=5))
        assert s.get_goal("finish report") is not None
        assert s.has_active_goals

    def test_dedup_same_name(self):
        s = AgentState()
        s.add_goal(Goal(name="finish report", priority=5))
        s.add_goal(Goal(name="finish report", priority=5))
        assert len(s.active_goals) == 1

    def test_top_goal_by_priority(self):
        s = AgentState()
        s.add_goal(Goal(name="low", priority=2))
        s.add_goal(Goal(name="high", priority=9))
        assert s.top_goal.name == "high"

    def test_prune_removes_completed(self):
        s = AgentState()
        g = Goal(name="task", priority=5)
        s.add_goal(g)
        g.complete()
        s.prune_goals()
        assert not s.has_active_goals


class TestIdleClock:
    def test_idle_seconds_resets_on_input(self):
        s = AgentState()
        s._last_interaction_ts = time.time() - 100
        assert s.idle_seconds > 90
        s.update_from_input("hello")
        assert s.idle_seconds < 1


class TestSystemHysteresis:
    def test_escalation_requires_two_ticks(self):
        # Audit fix: one tick above threshold must not flip to high_load.
        s = AgentState()
        s.update_from_system(cpu_pct=80.0)
        assert s.snapshot().system_state == "normal"
        s.update_from_system(cpu_pct=80.0)
        assert s.snapshot().system_state == "high_load"

    def test_return_to_normal_is_immediate(self):
        s = AgentState()
        s.update_from_system(cpu_pct=95.0)
        s.update_from_system(cpu_pct=95.0)
        assert s.snapshot().system_state == "critical"
        s.update_from_system(cpu_pct=10.0)
        assert s.snapshot().system_state == "normal"
