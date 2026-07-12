"""
Phase F — movie-JARVIS presence: voice tuning, barge-in, live context.

The system-prompt test is CI-safe (only needs httpx). The barge-in and TTS
tests import heavy audio/STT deps and are skipped where those aren't installed.
"""

import asyncio

import pytest


# ── Live context in the system prompt (CI-safe) ───────────────────────────────

class TestSystemPromptScreenContext:
    def test_screen_context_injected(self):
        from cognition.llm_client import build_system_prompt
        p = build_system_prompt(now_str="Monday", screen_context="VS Code — jarvis_v1")
        assert "On screen now" in p
        assert "VS Code" in p

    def test_no_screen_line_when_empty(self):
        from cognition.llm_client import build_system_prompt
        assert "On screen now" not in build_system_prompt(now_str="Monday")


# ── Voice tuning config (CI-safe) ─────────────────────────────────────────────

class TestVoiceConfig:
    def test_prosody_defaults(self):
        from core.config import TTSConfig
        c = TTSConfig()
        assert c.rate.endswith("%")
        assert "Hz" in c.pitch


# ── Barge-in phrase gating (needs the brain stack) ────────────────────────────

class TestBargeInPatterns:
    def setup_method(self):
        pytest.importorskip("numpy")
        pytest.importorskip("sounddevice")
        pytest.importorskip("faster_whisper")
        from core_v3.brain import _INTERRUPT_RE, _STOP_ONLY_RE
        self.interrupt = _INTERRUPT_RE
        self.stop_only = _STOP_ONLY_RE

    def test_interrupt_words_detected(self):
        for phrase in ["stop", "wait", "hold on", "cancel that", "jarvis do this"]:
            assert self.interrupt.search(phrase), phrase

    def test_ambient_not_interrupt(self):
        # Ordinary chatter shouldn't trip barge-in
        for phrase in ["the weather is nice", "i think so", "okay then"]:
            assert not self.interrupt.search(phrase), phrase

    def test_stop_only_vs_command(self):
        assert self.stop_only.match("stop")
        assert self.stop_only.match("stop jarvis")
        assert self.stop_only.match("hold on")
        # Carries a new instruction — NOT a bare stop
        assert not self.stop_only.match("jarvis what's the weather")
        assert not self.stop_only.match("wait what time is it")


# ── TTS interrupt mechanics (needs audio deps) ────────────────────────────────

class TestTTSInterrupt:
    async def test_interrupt_flushes_and_preserves_sentinel(self):
        pytest.importorskip("numpy")
        pytest.importorskip("sounddevice")
        from perception.tts import TTSEngine

        eng = TTSEngine(None)
        eng._queue = asyncio.Queue()
        await eng._queue.put((16000, [0.0, 0.0]))   # fake audio segment
        drain_evt = asyncio.Event()
        await eng._queue.put(drain_evt)             # a pending drain() waiter
        await eng._queue.put(None)                  # shutdown sentinel

        await eng.interrupt()

        assert drain_evt.is_set()                   # drain waiter released
        remaining = []
        try:
            while True:
                remaining.append(eng._queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        assert remaining == [None]                  # only the sentinel survives
