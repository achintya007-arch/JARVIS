"""
perception/hotword.py — Wake-word detector using faster-whisper tiny.en.

Strategy (no API key, no pvporcupine):
  - Record 1.5-second audio windows continuously via sounddevice
  - Skip transcription when RMS is below a silence threshold (cheap gate)
  - Run tiny.en (int8/CUDA) on loud windows — ~50ms per window on GPU
  - If the wake keyword appears in the transcription → fire callback
  - After callback completes, pause briefly before resuming detection
    (prevents re-triggering while the user is giving their command)

tiny.en is kept separate from the main STT engine (base.en) so the two
models are loaded independently and don't compete for resources.
tiny.en uses ~250 MB VRAM at int8; base.en uses ~500 MB.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

import numpy as np

log = logging.getLogger("jarvis.hotword")

# Fuzzy match for "jarvis" — tiny.en frequently mishears it, especially when the
# user's voice is masked by JARVIS's own (barge-in). The jarv/jerv prefix covers
# jarvis / jervis / jarvus / jervais / jarvies; the whole-word confusions are the
# other common ones. Kept narrow so JARVIS's own speech won't self-trigger.
_JARVIS_RE = re.compile(r"\b(?:j[ae]rv\w*|charvis|garvis|harvis|travis|jarvez)\b", re.IGNORECASE)

# ── Tuning ─────────────────────────────────────────────────────────────────────
_SAMPLE_RATE    = 16_000
_WINDOW_SECS    = 1.5          # seconds per recognition window
# Cheap pre-gate: skip tiny.en when the window is below this RMS. Kept low
# (0.006) so a quietly-spoken "Jarvis" still reaches the detector — the fuzzy
# keyword match, not this gate, decides whether it was the wake word. The only
# cost of a lower gate is tiny.en running on a few more near-silent windows.
_SILENCE_RMS    = 0.006
_POST_WAKE_SLEEP = 0.3         # brief pause after callback before resuming


class HotwordDetector:
    """
    Wake-word detector that uses faster-whisper tiny.en for keyword spotting.
    No external API, no pyaudio dependency.
    """

    def __init__(self, config=None) -> None:
        self.keyword  = getattr(config, "keyword",     "jarvis").lower()
        self.enabled  = getattr(config, "enabled",     True)
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Fix: load model once at construction rather than inside the worker
        # thread, preventing a fresh ~250 MB VRAM load on every listen() call.
        self._model = self._load_model() if self.enabled else None

    # ── Public interface ───────────────────────────────────────────────────────

    async def listen(self, callback: Callable[[], Awaitable[None]]) -> None:
        """
        Main entry point. Awaited by PerceptionAdapter as an asyncio.Task.
        Runs the blocking hotword loop in a thread so the event loop stays free.
        """
        self._loop    = asyncio.get_running_loop()
        self._running = True
        try:
            await asyncio.to_thread(self._hotword_loop, callback)
        except asyncio.CancelledError:
            self._running = False
            raise

    def stop(self) -> None:
        self._running = False

    # ── Pure detection (V3 wake loop) ───────────────────────────────────────────

    def detect(self, samples) -> bool:
        """
        Return True if the wake keyword appears in this audio window.

        Pure classifier — does NOT own the microphone. The V3 wake loop feeds it
        rolling windows drawn from STTEngine's single shared stream, so there is
        exactly one mic owner (fixes the old competing-sd.rec-stream bug).
        Cheap: RMS-gated, tiny.en, beam_size=1.
        """
        if self._model is None:
            return False
        rms = float(np.sqrt(np.mean(samples ** 2)))
        if rms < _SILENCE_RMS:
            return False
        try:
            segments, _ = self._model.transcribe(samples, beam_size=1, language="en")
            text = " ".join(seg.text for seg in segments).strip().lower()
        except Exception as exc:
            log.debug("Wake detect error: %s", exc)
            return False
        if not text:
            return False
        log.debug("Wake window: %r (rms=%.4f)", text, rms)
        # Fuzzy match for the default keyword (absorbs tiny.en mishears); exact
        # substring for any custom keyword.
        if self.keyword == "jarvis":
            return bool(_JARVIS_RE.search(text))
        return self.keyword in text

    # ── Internal ───────────────────────────────────────────────────────────────

    def _load_model(self):
        from faster_whisper import WhisperModel
        log.info("Loading hotword model (tiny.en)...")
        model = WhisperModel("tiny.en", device="cuda", compute_type="int8")
        log.info("Hotword model ready")
        return model

    def _hotword_loop(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Blocking loop — runs inside asyncio.to_thread()."""
        import sounddevice as sd

        # Model already loaded in __init__ — no reload needed.
        window_samples = int(_SAMPLE_RATE * _WINDOW_SECS)
        print(f'\n[Hotword] Active -- say "{self.keyword}" to wake me up\n')

        while self._running:
            try:
                # Record a fixed window (blocking)
                audio = sd.rec(
                    window_samples,
                    samplerate=_SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                )
                sd.wait()

                if not self._running:
                    break

                samples = audio[:, 0]
                rms = float(np.sqrt(np.mean(samples ** 2)))

                # Cheap silence gate — skip Whisper when quiet
                if rms < _SILENCE_RMS:
                    continue

                # Transcribe the window with tiny.en
                segments, _ = self._model.transcribe(
                    samples,
                    beam_size=1,           # fast — just need keyword detection
                    language="en",
                )
                text = " ".join(seg.text for seg in segments).strip().lower()

                if not text:
                    continue

                log.debug("Hotword window: %r (rms=%.4f)", text, rms)

                if self.keyword in text:
                    log.info("Wake word detected in: %r", text)
                    print("\n[Hotword] Wake word detected!\n")

                    # Fire callback on the event loop and wait for it
                    fut = asyncio.run_coroutine_threadsafe(callback(), self._loop)
                    try:
                        fut.result(timeout=30)   # wait for full command cycle
                    except Exception as exc:
                        log.error("Hotword callback error: %s", exc)

                    time.sleep(_POST_WAKE_SLEEP)

            except Exception as exc:
                if self._running:
                    log.error("Hotword loop error: %s", exc)
                    time.sleep(0.5)
