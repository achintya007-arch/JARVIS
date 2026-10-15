"""
The V3 events the BBP server forwards are actually published at their hook
points: VoiceOrchestrator → VoiceStateChanged + TranscriptReady,
ConversationEngine → ReplyChunk… then ReplyDone (interrupted on cancel).
Also STT's per-utterance confidence. Fakes only; no audio, no models.
"""

import asyncio
import math

import numpy as np
from test_conversation_followup import _Goals, _Vault
from test_voice_orchestrator import FakeConv, FakeEndpointer, FakeSTT, _orch

from core_v3.event_bus import EventBus
from core_v3.events import ReplyChunk, ReplyDone, TranscriptReady, VoiceStateChanged
from perception.stt import _confidence


class Recorder:
    def __init__(self, bus, *types):
        self.events = []
        for t in types:
            bus.subscribe(t, self._on)

    async def _on(self, event):
        self.events.append(event)


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


# ── Orchestrator ──────────────────────────────────────────────────────────────

class TestOrchestratorEvents:
    async def test_turn_publishes_states_and_transcript(self):
        bus = EventBus()
        rec = Recorder(bus, VoiceStateChanged, TranscriptReady)
        audio = np.zeros(16000, dtype=np.float32)
        stt = FakeSTT("open chrome"); stt.last_confidence = 0.82
        orch = _orch(endpointer=FakeEndpointer([(audio, "endpoint")]), stt=stt,
                     conv=FakeConv(["Opening Chrome, Sir."]))
        orch._bus = bus
        orch._running = True
        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)
        await _drain()

        states = [e.state for e in rec.events if isinstance(e, VoiceStateChanged)]
        assert states[:3] == ["listening", "processing", "speaking"]
        assert all(s in {"idle", "listening", "processing", "speaking", "interrupted"} for s in states)
        assert all(e.muted is False for e in rec.events if isinstance(e, VoiceStateChanged))
        transcripts = [e for e in rec.events if isinstance(e, TranscriptReady)]
        assert len(transcripts) == 1
        assert transcripts[0].text == "open chrome" and transcripts[0].final
        assert transcripts[0].confidence == 0.82

    async def test_no_bus_is_fine(self):
        audio = np.zeros(16000, dtype=np.float32)
        orch = _orch(endpointer=FakeEndpointer([(audio, "endpoint")]), stt=FakeSTT("hi"),
                     conv=FakeConv(["Hello."]))
        orch._running = True
        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)


# ── ConversationEngine ────────────────────────────────────────────────────────

def _engine(bus, llm):
    from types import SimpleNamespace

    from action.fast_router import FastRouter
    from cognition.agent_state import AgentState
    from cognition.decision_engine import DecisionEngine
    from core_v3.conversation import ConversationEngine
    return ConversationEngine(
        config=SimpleNamespace(agent=SimpleNamespace(screen_enabled=True)),
        llm=llm, agent_state=AgentState(),
        decision_engine=DecisionEngine(fast_router=FastRouter()),
        goal_manager=_Goals(), vault_memory=_Vault(),
        executor=None, agent_core=None, fast_router=FastRouter(), bus=bus,
    )


class _TwoSentenceLLM:
    def __init__(self, delay=0.0):
        self.delay = delay

    async def sentence_stream(self, messages, tools=None, system_prompt=None, last_message_out=None):
        if last_message_out is not None:
            last_message_out.append({})
        for s in ("The capital of France is Paris.", "It has about two million people."):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield s


class TestConversationEvents:
    async def test_reply_chunks_then_done(self):
        bus = EventBus()
        rec = Recorder(bus, ReplyChunk, ReplyDone)
        out = [s async for s in _engine(bus, _TwoSentenceLLM()).respond("what is the capital of france")]
        await _drain()
        chunks = [e for e in rec.events if isinstance(e, ReplyChunk)]
        done = [e for e in rec.events if isinstance(e, ReplyDone)]
        assert [c.text for c in chunks] == out and len(out) == 2
        assert [c.seq for c in chunks] == [0, 1]
        assert len(done) == 1 and done[0].interrupted is False
        assert {c.turn_id for c in chunks} == {done[0].turn_id}

    async def test_cancelled_turn_is_interrupted(self):
        bus = EventBus()
        rec = Recorder(bus, ReplyChunk, ReplyDone)
        engine = _engine(bus, _TwoSentenceLLM(delay=0.2))

        async def consume():
            async for _ in engine.respond("what is the capital of france"):
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)          # first sentence out, second pending
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await _drain()
        done = [e for e in rec.events if isinstance(e, ReplyDone)]
        assert len(done) == 1 and done[0].interrupted is True

    async def test_each_turn_gets_its_own_id(self):
        bus = EventBus()
        rec = Recorder(bus, ReplyDone)
        engine = _engine(bus, _TwoSentenceLLM())
        for _ in range(2):
            async for _ in engine.respond("what is the capital of france"):
                pass
        await _drain()
        assert len({e.turn_id for e in rec.events}) == 2


# ── STT confidence ────────────────────────────────────────────────────────────

class TestConfidence:
    def test_none_without_segments(self):
        assert _confidence([]) is None

    def test_mean_logprob_mapped_to_0_1(self):
        assert _confidence([0.0]) == 1.0
        assert _confidence([-0.5, -0.5]) == round(math.exp(-0.5), 3)
        assert 0.0 <= _confidence([-50.0]) <= 1.0
