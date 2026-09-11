"""
Hot-path latency invariant (CI-safe — only needs FastRouter + AgentState).

core_v3.brain._handle_user_input defers vault semantic recall
(build_messages → Ollama embed + Chroma search) until it knows the decision is
the LLM path. That deferral is only correct if DecisionEngine.decide() can reach
every *non-LLM* outcome WITHOUT reading `messages`. These tests pin that
invariant: pass messages=[] and assert the fast paths still resolve, so a future
change that makes decide() depend on messages for a reflex/rule/ignore outcome
fails here instead of silently reintroducing the per-utterance embedding cost.
"""

from action.fast_router import FastRouter
from cognition.agent_state import AgentState
from cognition.decision_engine import DecisionEngine


def _engine() -> DecisionEngine:
    return DecisionEngine(fast_router=FastRouter())


def _snapshot(text: str):
    s = AgentState()
    s.update_from_input(text)
    return s.snapshot()


class TestFastPathNeedsNoMessages:
    """Reflex / rule / ignore outcomes must resolve with an empty message list."""

    async def test_reflex_tool_without_messages(self):
        text = "what time is it"
        decision = await _engine().decide(
            text=text, state=_snapshot(text), memories=[], messages=[],
        )
        assert decision.action_type == "tool"
        assert decision.content.tool == "get_time"

    async def test_reflex_compound_plan_without_messages(self):
        text = "open chrome and open spotify"
        decision = await _engine().decide(
            text=text, state=_snapshot(text), memories=[], messages=[],
        )
        assert decision.action_type == "plan"

    async def test_gratitude_rule_without_messages(self):
        text = "thank you so much"
        decision = await _engine().decide(
            text=text, state=_snapshot(text), memories=[], messages=[],
        )
        # Rule stage answers gratitude directly — no LLM, no messages.
        assert decision.action_type == "speak"

    async def test_low_signal_ignored_without_messages(self):
        text = "ok"
        decision = await _engine().decide(
            text=text, state=_snapshot(text), memories=[], messages=[],
        )
        assert decision.action_type == "ignore"


class TestLlmPathIsTheOnlyMessageConsumer:
    """Only the LLM fallback is expected to use messages — so it's the only
    outcome for which the brain builds them."""

    async def test_conversational_input_routes_to_llm(self):
        text = "tell me what you think about jazz"
        decision = await _engine().decide(
            text=text, state=_snapshot(text), memories=[], messages=[],
        )
        assert decision.action_type == "llm"
