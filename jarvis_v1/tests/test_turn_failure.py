"""
A failed turn must not end the session.

Repro this guards: with Ollama stopped, an LLM question in V3 text mode raised
httpx.ConnectError out of ConversationEngine.respond and Brain._text_loop, so
Brain.start's finally ran stop() and JARVIS exited.

Now: an unreachable LLM gets a spoken fallback (shared by voice and text), any
other turn error gets a short apology, and the loop carries on. Fakes only —
no network, no audio.
"""

import asyncio
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from action.fast_router import FastRouter
from cognition.agent_state import AgentState
from cognition.decision_engine import DecisionEngine
from core.config import VoiceConfig
from core_v3.brain import Brain
from core_v3.conversation import (
    LLM_UNREACHABLE_REPLY,
    TURN_FAILED_REPLY,
    ConversationEngine,
)
from core_v3.events import ReplyDone
from core_v3.voice.orchestrator import VoiceOrchestrator


# ── Fakes ─────────────────────────────────────────────────────────────────
class _Goals:
    async def handle_input(self, text):
        return None


class _Vault:
    def __init__(self):
        self.persisted = []

    async def build_messages(self, text, intent=None):
        return [{"role": "user", "content": text}]

    async def add_exchange(self, user, assistant):
        self.persisted.append((user, assistant))


class _DownLLM:
    """Ollama stopped: the first request fails to connect."""
    async def sentence_stream(self, messages, tools=None, system_prompt=None, last_message_out=None):
        raise httpx.ConnectError("All connection attempts failed")
        yield  # pragma: no cover  (makes this an async generator)


class _DropsMidReplyLLM:
    """Connection lost after one sentence was already spoken."""
    async def sentence_stream(self, messages, tools=None, system_prompt=None, last_message_out=None):
        yield "Paris is the capital of France, Sir."
        raise httpx.ReadError("connection reset")


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


def _engine(llm, vault=None, bus=None):
    return ConversationEngine(
        config=SimpleNamespace(agent=SimpleNamespace(screen_enabled=True)),
        llm=llm, agent_state=AgentState(),
        decision_engine=DecisionEngine(fast_router=FastRouter()),
        goal_manager=_Goals(), vault_memory=vault or _Vault(),
        executor=None, agent_core=None, fast_router=FastRouter(), bus=bus,
    )


async def _collect(gen):
    return [s async for s in gen]


def _text_brain(conversation):
    """A Brain with only what _text_loop touches (no mic, TTS, Ollama...)."""
    brain = Brain.__new__(Brain)
    brain._stopped = False
    brain._conversation = conversation
    brain._turn_lock = asyncio.Lock()
    return brain


def _feed_input(monkeypatch, lines):
    """Replace input() with scripted lines, then EOF (ends the loop)."""
    it = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


# ── ConversationEngine: unreachable LLM → spoken fallback ──────────────────
class TestLLMUnreachable:
    async def test_connect_error_yields_fallback(self):
        bus = _Bus()
        vault = _Vault()
        out = await _collect(_engine(_DownLLM(), vault, bus).respond("what is the capital of France"))
        assert out == [LLM_UNREACHABLE_REPLY]
        # The turn completed with a reply, so the Body sees a normal ReplyDone.
        done = [e for e in bus.events if isinstance(e, ReplyDone)]
        assert len(done) == 1 and done[0].interrupted is False
        # The canned fallback is not stored as conversation history.
        assert vault.persisted == []

    async def test_drop_mid_reply_propagates_as_interrupted(self):
        bus = _Bus()
        gen = _engine(_DropsMidReplyLLM(), bus=bus).respond("what is the capital of France")
        assert await gen.__anext__() == "Paris is the capital of France, Sir."
        with pytest.raises(httpx.ReadError):
            await gen.__anext__()
        done = [e for e in bus.events if isinstance(e, ReplyDone)]
        assert len(done) == 1 and done[0].interrupted is True


# ── Brain._text_loop survives a failed turn ────────────────────────────────
class _FlakyConversation:
    """First turn raises; later turns answer normally."""
    def __init__(self, exc):
        self._exc = exc
        self.texts = []

    async def respond(self, text, followup=False):
        self.texts.append(text)
        if len(self.texts) == 1:
            raise self._exc
        yield f"Echo: {text}"


class TestTextLoopSurvives:
    async def test_llm_down_then_next_input_processed(self, monkeypatch, capsys):
        _feed_input(monkeypatch, ["what is the capital of France", "what is the capital of Spain"])
        brain = _text_brain(_engine(_DownLLM()))
        await asyncio.wait_for(brain._text_loop(), timeout=5.0)
        out = capsys.readouterr().out
        assert out.count(f"JARVIS: {LLM_UNREACHABLE_REPLY}") == 2
        assert brain._stopped is False          # loop ended on EOF, not via stop()

    async def test_raising_turn_apologises_and_continues(self, monkeypatch, capsys):
        _feed_input(monkeypatch, ["first", "second"])
        conv = _FlakyConversation(httpx.ConnectError("All connection attempts failed"))
        brain = _text_brain(conv)
        await asyncio.wait_for(brain._text_loop(), timeout=5.0)
        out = capsys.readouterr().out
        assert conv.texts == ["first", "second"]
        assert f"JARVIS: {TURN_FAILED_REPLY}" in out
        assert "JARVIS: Echo: second" in out

    async def test_cancellation_is_not_swallowed(self, monkeypatch):
        _feed_input(monkeypatch, ["first", "second"])
        conv = _FlakyConversation(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _text_brain(conv)._text_loop()
        assert conv.texts == ["first"]


# ── Voice path: same apology, spoken ───────────────────────────────────────
class _Mic:
    async def start(self): pass
    async def stop(self): pass
    def subscribe(self, maxsize=50): return asyncio.Queue()
    def unsubscribe(self, q): pass


class _Endpointer:
    async def capture(self, q, preroll=None, onset_timeout_sec=None):
        return np.zeros(16000, dtype=np.float32), "endpoint"


class _STT:
    async def transcribe(self, audio, allow_answers=False):
        return "what is the capital of France"


class _TTS:
    def __init__(self): self.spoken = []
    async def synthesize(self, text):
        self.spoken.append(text)
        return 16000, np.zeros(8, dtype=np.float32)


class _Player:
    def __init__(self): self.enqueued = 0
    def enqueue(self, fs, samples): self.enqueued += 1
    def interrupt(self): pass
    async def wait_idle(self): return


class _RaisingConversation:
    async def respond(self, text, followup=False):
        raise RuntimeError("boom")
        yield  # pragma: no cover


class TestVoiceTurnFailure:
    async def test_voice_turn_error_is_spoken_not_silent(self):
        cfg = VoiceConfig()
        cfg.allow_barge_in = False
        tts, player = _TTS(), _Player()
        orch = VoiceOrchestrator(
            mic=_Mic(), wake=None, endpointer=_Endpointer(), stt=_STT(),
            tts=tts, player=player, interruption=None,
            conversation=_RaisingConversation(), voice_config=cfg,
        )
        orch._running = True
        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)
        assert tts.spoken == [TURN_FAILED_REPLY]
        assert player.enqueued == 1
