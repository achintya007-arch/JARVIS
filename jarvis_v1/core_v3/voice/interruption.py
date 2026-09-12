"""
InterruptionController — barge-in detection while Echo is speaking.

Runs concurrently with playback, consuming its own mic subscription through
Silero VAD. When it sees sustained speech (interrupt_speech_sec of frames above
threshold) it reports a barge-in and hands back the frames captured so far, so
the interrupting utterance can be carried straight into the next LISTENING
capture as look-back (the user's words aren't lost).

Open-speaker caveat: without acoustic echo cancellation the mic also hears
Echo's own voice, which can self-trigger. The sustained-speech + higher
threshold guard reduces this; headphones eliminate it. Tune via VoiceConfig
(interrupt_speech_sec, interrupt_silero_threshold) or disable (allow_barge_in).
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from perception.endpointer import _rms

log = logging.getLogger("jarvis.voice.interrupt")


class InterruptionController:
    def __init__(self, voice_config=None, stt_config=None, frame_ms: int = 80) -> None:
        self._needed = max(1, round(
            (getattr(voice_config, "interrupt_speech_sec", 0.4) if voice_config else 0.4)
            / (frame_ms / 1000.0)
        ))
        self._threshold = getattr(voice_config, "interrupt_silero_threshold", 0.6) if voice_config else 0.6
        self._rms_threshold = getattr(stt_config, "silence_threshold", 0.010) if stt_config else 0.010

        self._silero = None
        backend = getattr(stt_config, "vad_backend", "silero") if stt_config else "silero"
        if backend == "silero":
            try:
                from perception.endpointer import _SileroVad
                self._silero = _SileroVad()
            except Exception as e:
                log.warning("Silero unavailable for barge-in (%s) — using RMS", e)

    def _score(self, frame: np.ndarray) -> float:
        if self._silero is not None:
            return self._silero.prob(frame)
        return _rms(frame)

    def _threshold_for(self) -> float:
        return self._threshold if self._silero is not None else max(self._rms_threshold * 2.0, 0.02)

    async def monitor(self, frame_q: asyncio.Queue) -> list[np.ndarray]:
        """Block until sustained speech (barge-in) is detected; return the frames
        captured leading up to it (for use as look-back). Cancel this task to
        stop monitoring when speech ends without interruption."""
        if self._silero is not None:
            self._silero.reset()
        thr = self._threshold_for()
        run = 0
        recent: list[np.ndarray] = []
        while True:
            frame = await frame_q.get()
            recent.append(frame)
            if len(recent) > self._needed * 3:
                recent.pop(0)
            if self._score(frame) >= thr:
                run += 1
                if run >= self._needed:
                    log.info("Barge-in detected (%d speech frames)", run)
                    return recent
            else:
                run = 0
