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


# NOTE: barge-in is now VAD-based in the V3 voice rebuild (InterruptionController
# via Silero), not phrase-matched — see tests/test_voice_orchestrator.py. The old
# _INTERRUPT_RE/_STOP_ONLY_RE phrase-gating test was removed with that code.


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
