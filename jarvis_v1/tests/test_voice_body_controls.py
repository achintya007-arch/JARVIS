"""
VoiceOrchestrator Body controls (BBP milestone 2), driven through the real
run() loop with fakes: request_wake skips the wake word, request_text runs a
typed turn (spoken or not), push-to-talk while speaking barges in, cancel_turn
stops any turn, and set_muted closes the microphone while typed input still
works.
"""

import asyncio

import numpy as np
from test_voice_orchestrator import FakeConv, FakeEndpointer, FakePlayer, FakeSTT, NoBargeIn

from core.config import VoiceConfig
from core_v3.event_bus import EventBus
from core_v3.events import VoiceStateChanged
from core_v3.voice.orchestrator import VoiceOrchestrator
from core_v3.voice.state import VoiceState


class ControlMic:
    def __init__(self):
        self.starts = self.stops = 0
        self.subscribers = []

    async def start(self): self.starts += 1
    async def stop(self): self.stops += 1

    def subscribe(self, maxsize=50):
        q = asyncio.Queue(maxsize=maxsize)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)


class SilentWake:
    """A wake model that never fires on audio."""
    available, threshold = True, 0.4
    def reset(self): pass
    def score(self, frame): return 0.0
    def triggered(self, frame): return False


class BlockingEndpointer:
    """Captures forever (until cancelled) — a user who never stops talking."""
    def __init__(self): self.calls = 0
    async def capture(self, q, preroll=None, onset_timeout_sec=None):
        self.calls += 1
        await asyncio.Event().wait()


class Harness:
    def __init__(self, *, conv=None, endpointer=None, player=None):
        self.mic = ControlMic()
        self.conv = conv or FakeConv([])
        self.endpointer = endpointer or FakeEndpointer([(None, "onset_timeout")] * 10)
        self.player = player or FakePlayer()
        self.bus = EventBus()
        self.events = []
        cfg = VoiceConfig()
        cfg.chime_on_wake = False
        cfg.follow_up_chime = False
        self.orch = VoiceOrchestrator(
            mic=self.mic, wake=SilentWake(), endpointer=self.endpointer,
            stt=FakeSTT("hello"), tts=_TTS(), player=self.player,
            interruption=NoBargeIn(), conversation=self.conv,
            voice_config=cfg, bus=self.bus,
        )
        self.states = []
        self.orch.state.add_listener(lambda old, new: self.states.append(new))
        self.bus.subscribe(VoiceStateChanged, self._record)
        self.task = None

    async def _record(self, ev):
        self.events.append(ev)

    async def __aenter__(self):
        self.orch._running = True
        self.task = asyncio.create_task(self.orch.run())
        await self.until(lambda: self.orch._wake_q is not None)
        return self

    async def __aexit__(self, *exc):
        self.orch._running = False
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def until(self, cond, timeout=3.0):
        async def wait():
            while not cond():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), timeout)


class _TTS:
    async def synthesize(self, text):
        return 16000, np.zeros(8, dtype=np.float32)


# ── Wake ──────────────────────────────────────────────────────────────────────

class TestRequestWake:
    async def test_skips_the_wake_word(self):
        async with Harness() as h:
            assert h.orch.request_wake() == "wake"
            await h.until(lambda: h.endpointer.prerolls)          # captured an utterance
            await h.until(lambda: h.orch.state.is_(VoiceState.IDLE) and h.orch._wake_q is not None)
            assert VoiceState.LISTENING in h.states

    async def test_double_tap_queues_one_wake(self):
        async with Harness() as h:
            assert h.orch.request_wake() == "wake"
            assert h.orch.request_wake() == "wake"
            await h.until(lambda: h.orch._wake_q is not None and len(h.endpointer.prerolls) == 1)
            await asyncio.sleep(0.05)
            assert len(h.endpointer.prerolls) == 1

    async def test_while_listening_is_a_no_op(self):
        ep = BlockingEndpointer()
        async with Harness(endpointer=ep) as h:
            h.orch.request_wake()
            await h.until(lambda: ep.calls == 1)
            assert h.orch.request_wake() == "listening"


# ── Typed turns ───────────────────────────────────────────────────────────────

class TestRequestText:
    async def test_typed_turn_is_not_spoken_by_default(self):
        conv = FakeConv(["Paris, Sir."])
        async with Harness(conv=conv) as h:
            assert h.orch.request_text("capital of france") is True
            await h.until(lambda: conv.texts == ["capital of france"])
            await h.until(lambda: h.orch.state.is_(VoiceState.IDLE) and h.orch._wake_q is not None)
            assert h.player.enqueued == []
            assert VoiceState.PROCESSING in h.states and VoiceState.SPEAKING not in h.states
            assert h.endpointer.prerolls == []                     # never listened

    async def test_typed_turn_spoken_when_asked(self):
        conv = FakeConv(["Paris, Sir."])
        async with Harness(conv=conv) as h:
            h.orch.request_text("capital of france", speak=True)
            await h.until(lambda: len(h.player.enqueued) == 1)
            assert VoiceState.SPEAKING in h.states

    async def test_refused_when_not_idle(self):
        conv = FakeConv(["slow"], delay=5.0)
        async with Harness(conv=conv) as h:
            assert h.orch.request_text("one") is True
            await h.until(lambda: h.orch.state.is_(VoiceState.PROCESSING))
            assert h.orch.request_text("two") is False


# ── Cancel / push-to-talk barge-in ────────────────────────────────────────────

class TestCancelAndBarge:
    async def test_cancel_while_speaking_stops_and_returns_to_idle(self):
        conv = FakeConv(["A long answer."], delay=5.0)
        async with Harness(conv=conv) as h:
            h.orch.request_text("tell me a story", speak=True)
            await h.until(lambda: h.orch.state.is_(VoiceState.SPEAKING))
            assert h.orch.cancel_turn() is True
            await h.until(lambda: h.orch.state.is_(VoiceState.IDLE) and h.orch._wake_q is not None)
            assert conv.cancelled and h.player.interrupted
            assert VoiceState.INTERRUPTED in h.states
            assert h.endpointer.prerolls == []                     # cancel does not re-listen

    async def test_cancel_while_listening(self):
        ep = BlockingEndpointer()
        async with Harness(endpointer=ep) as h:
            h.orch.request_wake()
            await h.until(lambda: ep.calls == 1)
            assert h.orch.cancel_turn() is True
            await h.until(lambda: h.orch.state.is_(VoiceState.IDLE) and h.orch._wake_q is not None)

    async def test_cancel_when_idle_does_nothing(self):
        async with Harness() as h:
            assert h.orch.cancel_turn() is False

    async def test_push_to_talk_while_speaking_barges_in_and_listens(self):
        conv = FakeConv(["A long answer."], delay=5.0)
        async with Harness(conv=conv) as h:
            h.orch.request_text("tell me a story", speak=True)
            await h.until(lambda: h.orch.state.is_(VoiceState.SPEAKING))
            assert h.orch.request_wake() == "barge_in"
            await h.until(lambda: h.endpointer.prerolls)           # listening for the new command
            assert conv.cancelled and h.player.interrupted
            assert VoiceState.INTERRUPTED in h.states


# ── Mute ──────────────────────────────────────────────────────────────────────

class TestMute:
    async def test_mute_closes_the_mic_and_publishes(self):
        async with Harness() as h:
            await h.orch.set_muted(True)
            assert h.mic.stops == 1 and h.orch.muted
            await h.until(lambda: any(e.muted for e in h.events))
            await h.orch.set_muted(False)
            assert h.mic.starts == 1 and not h.orch.muted
            await h.until(lambda: h.events and h.events[-1].muted is False)

    async def test_mute_while_listening_abandons_the_capture(self):
        ep = BlockingEndpointer()
        async with Harness(endpointer=ep) as h:
            h.orch.request_wake()
            await h.until(lambda: ep.calls == 1)
            await h.orch.set_muted(True)
            await h.until(lambda: h.orch.state.is_(VoiceState.IDLE) and h.orch._wake_q is not None)
            assert h.mic.stops == 1

    async def test_typed_input_still_works_while_muted(self):
        conv = FakeConv(["Done, Sir."])
        async with Harness(conv=conv) as h:
            await h.orch.set_muted(True)
            assert h.orch.request_text("add milk to my list") is True
            await h.until(lambda: conv.texts == ["add milk to my list"])

    async def test_mute_twice_is_idempotent(self):
        async with Harness() as h:
            await h.orch.set_muted(True)
            await h.orch.set_muted(True)
            assert h.mic.stops == 1
