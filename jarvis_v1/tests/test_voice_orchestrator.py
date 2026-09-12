"""
Voice rebuild — VoiceOrchestrator integration with fakes (no mic/model/audio).

Covers: a full wake→listen→process→speak turn returning to IDLE, new speech
while Echo is speaking (barge-in → cancel generation + playback → back to
LISTENING carrying the interrupting audio), cancellation of pending speech, and
failure/fallback (wake unavailable; TTS synth returns None).
"""

import asyncio

import numpy as np
import pytest

from core.config import VoiceConfig
from core_v3.voice.orchestrator import VoiceOrchestrator
from core_v3.voice.state import VoiceState


# ── Fakes ─────────────────────────────────────────────────────────────────
class FakeMic:
    async def start(self): pass
    async def stop(self): pass
    def subscribe(self, maxsize=50): return asyncio.Queue()
    def unsubscribe(self, q): pass


class FakeEndpointer:
    def __init__(self, results): self.results = list(results); self.prerolls = []
    async def capture(self, q, preroll=None):
        self.prerolls.append(preroll)
        return self.results.pop(0)


class FakeSTT:
    def __init__(self, text): self._text = text
    async def transcribe(self, audio): return self._text


class FakeConv:
    def __init__(self, sentences, delay=0.0):
        self.sentences, self.delay = sentences, delay
        self.text = None; self.cancelled = False
    async def respond(self, text):
        self.text = text
        try:
            for s in self.sentences:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield s
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FakeTTS:
    def __init__(self, seg=(16000, None)):
        self._seg = (seg[0], np.zeros(8, dtype=np.float32)) if seg[1] is None else seg
        self.none = False
    async def synthesize(self, text):
        return None if self.none else self._seg


class FakePlayer:
    def __init__(self): self.enqueued = []; self.interrupted = False
    async def start(self): pass
    async def shutdown(self): pass
    def enqueue(self, fs, samples): self.enqueued.append((fs, len(samples)))
    def interrupt(self): self.interrupted = True
    async def wait_idle(self): return
    @property
    def is_idle(self): return True


class NoBargeIn:
    async def monitor(self, q):
        await asyncio.Event().wait()       # never fires


class FakeWake:
    def __init__(self, available=True): self.available = available
    def reset(self): pass
    def triggered(self, frame): return True


def _orch(*, endpointer, stt, conv, tts=None, player=None, interruption=None, wake=None):
    return VoiceOrchestrator(
        mic=FakeMic(), wake=wake or FakeWake(), endpointer=endpointer,
        stt=stt, tts=tts or FakeTTS(), player=player or FakePlayer(),
        interruption=interruption or NoBargeIn(), conversation=conv,
        voice_config=VoiceConfig(),
    )


# ── Tests ─────────────────────────────────────────────────────────────────
class TestFullTurn:
    async def test_turn_speaks_then_completes(self):
        audio = np.zeros(16000, dtype=np.float32)
        ep = FakeEndpointer([(audio, "endpoint")])
        conv = FakeConv(["Opening Chrome, Sir."])
        player = FakePlayer()
        orch = _orch(endpointer=ep, stt=FakeSTT("open chrome"), conv=conv, player=player)
        orch._running = True

        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)

        assert conv.text == "open chrome"          # cognition received the utterance
        assert len(player.enqueued) == 1           # the response was spoken
        assert not player.interrupted              # no barge-in


class TestReturnToIdle:
    async def test_no_speech_returns_without_speaking(self):
        ep = FakeEndpointer([(None, "onset_timeout")])   # nothing captured
        conv = FakeConv(["should not be said"])
        player = FakePlayer()
        orch = _orch(endpointer=ep, stt=FakeSTT(""), conv=conv, player=player)
        orch._running = True
        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)
        assert conv.text is None
        assert player.enqueued == []


class TestBargeIn:
    async def test_new_speech_interrupts_and_relistens(self):
        audio = np.zeros(16000, dtype=np.float32)
        barge_frames = [np.zeros(1280, dtype=np.float32)]

        class BargeOnce:
            def __init__(self): self.fired = False
            async def monitor(self, q):
                if self.fired:
                    await asyncio.Event().wait()
                self.fired = True
                await asyncio.sleep(0.02)
                return barge_frames

        # First turn produces slowly so the barge-in wins the race; second
        # LISTENING returns nothing → back to IDLE.
        ep = FakeEndpointer([(audio, "endpoint"), (None, "onset_timeout")])
        conv = FakeConv(["Sure, the weather is—"], delay=5.0)
        player = FakePlayer()
        orch = _orch(endpointer=ep, stt=FakeSTT("tell me about"), conv=conv,
                     player=player, interruption=BargeOnce())
        orch._running = True

        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)

        assert player.interrupted                 # playback stopped on barge-in
        assert conv.cancelled                     # generation cancelled
        assert ep.prerolls[-1] == barge_frames    # interrupting audio carried into re-LISTEN
        assert orch.state.state in (VoiceState.INTERRUPTED, VoiceState.LISTENING, VoiceState.SPEAKING)


class TestFallback:
    async def test_wake_unavailable_does_not_hang(self):
        orch = _orch(endpointer=FakeEndpointer([]), stt=FakeSTT(""),
                     conv=FakeConv([]), wake=FakeWake(available=False))
        orch._running = True
        await asyncio.wait_for(orch._await_wake(), timeout=2.0)   # returns via sleep, no crash

    async def test_tts_returning_none_is_handled(self):
        audio = np.zeros(16000, dtype=np.float32)
        tts = FakeTTS(); tts.none = True
        ep = FakeEndpointer([(audio, "endpoint")])
        player = FakePlayer()
        orch = _orch(endpointer=ep, stt=FakeSTT("hello"), conv=FakeConv(["hi"]),
                     tts=tts, player=player)
        orch._running = True
        await asyncio.wait_for(orch._conversation_turn(), timeout=3.0)
        assert player.enqueued == []              # nothing enqueued, no crash
