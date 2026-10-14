"""
Follow-up answers: a short reply to Echo's question ("yes") must reach the LLM.

Outside a follow-up, a bare "yes" is low-signal and ignored (as before). When
it answers a question Echo just asked, ConversationEngine routes it to the LLM,
whose context (the vault's rolling window) holds the question.
"""

from types import SimpleNamespace

from action.fast_router import FastRouter
from cognition.agent_state import AgentState
from cognition.decision_engine import DecisionEngine
from core_v3.conversation import ConversationEngine


class _Goals:
    async def handle_input(self, text):
        return None


class _Vault:
    def __init__(self):
        self.built_for = []

    async def build_messages(self, text, intent=None):
        self.built_for.append(text)
        return [{"role": "user", "content": text}]

    async def add_exchange(self, user, assistant):
        pass


class _LLM:
    async def sentence_stream(self, messages, tools=None, system_prompt=None, last_message_out=None):
        if last_message_out is not None:
            last_message_out.append({})
        yield "Setting it up now, Sir."


def _engine(vault):
    return ConversationEngine(
        config=SimpleNamespace(agent=SimpleNamespace(screen_enabled=True)),
        llm=_LLM(), agent_state=AgentState(),
        decision_engine=DecisionEngine(fast_router=FastRouter()),
        goal_manager=_Goals(), vault_memory=vault,
        executor=None, agent_core=None, fast_router=FastRouter(),
    )


async def _collect(gen):
    return [s async for s in gen]


class TestFollowUpAnswers:
    async def test_yes_answering_a_question_reaches_llm(self):
        vault = _Vault()
        out = await _collect(_engine(vault).respond("yes", followup=True))
        assert out == ["Setting it up now, Sir."]
        assert vault.built_for == ["yes"]           # LLM got context for the answer

    async def test_bare_yes_outside_followup_still_ignored(self):
        vault = _Vault()
        out = await _collect(_engine(vault).respond("yes"))
        assert out == []
        assert vault.built_for == []
