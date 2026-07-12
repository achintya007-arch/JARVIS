"""
Voice-reliability rebuild — wake-word state machine + half-duplex gating.

Uses fake STT/hotword so no microphone, GPU, or Whisper is needed. Verifies:
  - wake detected -> command captured -> UserInput(source="wake") published
  - while JARVIS is speaking, the mic is ignored (no wake, no transcription)
  - STTEngine's shared-feed helpers (read_window / flush)
"""

import asyncio

import pytest

np = pytest.importorskip("numpy")

from core.config import Config
from core_common.event_bus import EventBus
from core_common.events import UserInput, SystemEvent
from core_common.speaking import SpeakingGuard
from core_v3.adapters import PerceptionAdapter


class FakeSTT:
    def __init__(self):
        self.flushed = 0

    def read_window(self, seconds, timeout=1.0):
        return np.zeros(int(16000 * seconds), dtype=np.float32)

    def flush(self):
        self.flushed += 1

    async def record_utterance(self):
        return np.zeros(16000, dtype=np.float32)

    async def transcribe(self, audio):
        return "what time is it"


class FakeHotword:
    def __init__(self, fires=True, once=False):
        self.fires = fires
        self.once = once
        self.detect_calls = 0

    def detect(self, samples):
        self.detect_calls += 1
        if self.once:
            return self.fires and self.detect_calls == 1
        return self.fires

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def _silent_chime(monkeypatch):
    # The real chime plays an audible beep and blocks ~0.16s; stub it so the
    # wake loop's _on_wake completes fast and deterministically in tests.
    import perception.cues
    monkeypatch.setattr(perception.cues, "play_chime", lambda: None)


async def _run_loop_briefly(adapter, seconds=0.15):
    adapter._running = True
    task = asyncio.create_task(adapter._wake_loop())
    await asyncio.sleep(seconds)
    adapter._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestWakeStateMachine:
    async def test_wake_captures_and_publishes_command(self):
        bus = EventBus()
        published = []
        bus.subscribe(UserInput, lambda e: published.append(e) or asyncio.sleep(0))

        stt, hot, guard = FakeSTT(), FakeHotword(fires=True, once=True), SpeakingGuard()
        adapter = PerceptionAdapter(stt, hot, bus, Config(), guard)

        await _run_loop_briefly(adapter, seconds=0.3)

        assert any(p.source == "wake" and "time" in p.text for p in published)
        assert stt.flushed >= 1  # queue cleared before command capture

    async def test_no_wake_when_not_matched(self):
        bus = EventBus()
        published = []
        bus.subscribe(UserInput, lambda e: published.append(e) or asyncio.sleep(0))

        stt, hot, guard = FakeSTT(), FakeHotword(fires=False), SpeakingGuard()
        adapter = PerceptionAdapter(stt, hot, bus, Config(), guard)

        await _run_loop_briefly(adapter)

        assert published == []
        assert hot.detect_calls > 0  # it WAS listening, just never matched

    async def test_barge_in_interrupts_and_captures(self):
        # Wake word DURING speech should cut the reply (interrupt_cb) then take
        # the new command.
        bus = EventBus()
        published = []
        bus.subscribe(UserInput, lambda e: published.append(e) or asyncio.sleep(0))

        interrupts = []
        async def interrupt_cb():
            interrupts.append(1)

        stt, hot, guard = FakeSTT(), FakeHotword(fires=True, once=True), SpeakingGuard()
        guard.value = True  # JARVIS is talking
        adapter = PerceptionAdapter(stt, hot, bus, Config(), guard, interrupt_cb=interrupt_cb)

        await _run_loop_briefly(adapter, seconds=0.3)

        assert interrupts, "barge-in should have called interrupt_cb"
        assert any(p.source == "wake" for p in published)

    async def test_no_barge_in_without_wake_word(self):
        # Speaking + no wake word => JARVIS's own voice must not trigger anything.
        bus = EventBus()
        published = []
        bus.subscribe(UserInput, lambda e: published.append(e) or asyncio.sleep(0))

        interrupts = []
        async def interrupt_cb():
            interrupts.append(1)

        stt, hot, guard = FakeSTT(), FakeHotword(fires=False), SpeakingGuard()
        guard.value = True
        adapter = PerceptionAdapter(stt, hot, bus, Config(), guard, interrupt_cb=interrupt_cb)

        await _run_loop_briefly(adapter)

        assert interrupts == []
        assert published == []


class TestSharedFeed:
    def test_read_window_and_flush(self):
        pytest.importorskip("sounddevice")
        pytest.importorskip("faster_whisper")
        import queue as stdlib_queue
        from perception.stt import STTEngine

        eng = STTEngine.__new__(STTEngine)          # skip model load
        eng._chunk_q = stdlib_queue.Queue()
        for _ in range(10):
            eng._chunk_q.put(np.zeros(1600, dtype=np.float32))  # 0.1s chunks

        win = eng.read_window(0.5)                    # 5 chunks
        assert win is not None and len(win) == 5 * 1600

        eng.flush()
        assert eng._chunk_q.empty()

    def test_read_window_empty_returns_none(self):
        pytest.importorskip("sounddevice")
        pytest.importorskip("faster_whisper")
        import queue as stdlib_queue
        from perception.stt import STTEngine

        eng = STTEngine.__new__(STTEngine)
        eng._chunk_q = stdlib_queue.Queue()
        assert eng.read_window(0.5, timeout=0.05) is None
