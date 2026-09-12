"""
EndpointDetector — decides when the user has actually finished speaking.

Uses Silero VAD (neural speech probability) as the primary detector, with a
simple RMS energy fallback if Silero isn't installed. The decision logic lives
in the pure EndpointSM state machine (fed speech scores + frames), so it is
unit-tested with synthetic input — onset, brief mid-utterance pause, endpoint,
short/long utterance, noise rejection, and timeouts.

Crucially, a brief pause inside an utterance does NOT end the turn: only a
sustained trailing silence (end_silence) does, so
"open Chrome and then— actually, open VS Code instead" is captured whole.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import numpy as np

log = logging.getLogger("jarvis.endpoint")

_SAMPLE_RATE = 16_000
_END_HYSTERESIS = 0.6            # end threshold = onset_threshold * this
_ONSET_DEBOUNCE = 2              # consecutive speech frames to confirm onset


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.clip(x, -1.0, 1.0) ** 2)))


class _SileroVad:
    """Maps arbitrary-length chunks to a speech probability; buffers to Silero's
    required 512-sample frames and returns the max prob over them."""
    FRAME = 512

    def __init__(self) -> None:
        import torch  # noqa: F401
        from silero_vad import load_silero_vad

        self._torch = torch
        self._model = load_silero_vad(onnx=True)
        self._buf = np.empty(0, dtype=np.float32)

    def reset(self) -> None:
        self._model.reset_states()
        self._buf = np.empty(0, dtype=np.float32)

    def prob(self, chunk: np.ndarray) -> float:
        self._buf = np.concatenate([self._buf, chunk.astype(np.float32)])
        best = 0.0
        while len(self._buf) >= self.FRAME:
            frame = self._buf[: self.FRAME]
            self._buf = self._buf[self.FRAME:]
            p = float(self._model(self._torch.from_numpy(frame), _SAMPLE_RATE))
            best = max(best, p)
        return best


class EndpointSM:
    """Pure endpoint state machine — no audio I/O, fully unit-testable.

    feed(score, frame) returns None while still capturing, or a stop reason:
    "endpoint" | "max_duration" | "onset_timeout". A score above `threshold`
    counts as speech. Call audio() afterward for the captured samples (None if
    nothing usable was captured).
    """

    def __init__(
        self,
        *,
        threshold: float,
        end_silence_frames: int,
        max_frames: int,
        pre_frames: int,
        onset_timeout_frames: int,
        min_speech_frames: int,
    ) -> None:
        self.threshold = threshold
        self.end_silence_frames = end_silence_frames
        self.max_frames = max_frames
        self.onset_timeout_frames = onset_timeout_frames
        self.min_speech_frames = min_speech_frames
        self._pre = deque(maxlen=max(1, pre_frames))
        self._collected: list[np.ndarray] = []
        self._started = False
        self._onset_run = 0
        self._silence_run = 0
        self._wait = 0
        self._total = 0
        self.onset_score = 0.0

    def feed(self, score: float, frame: np.ndarray) -> str | None:
        self._total += 1
        if not self._started:
            self._pre.append(frame)
            self._wait += 1
            if score >= self.threshold:
                self._onset_run += 1
                if self._onset_run >= _ONSET_DEBOUNCE:
                    self._started = True
                    self.onset_score = score
                    self._collected.extend(self._pre)
                    self._pre.clear()
            else:
                self._onset_run = 0
            if self._wait >= self.onset_timeout_frames:
                return "onset_timeout"
            return None

        self._collected.append(frame)
        if score < self.threshold * _END_HYSTERESIS:
            self._silence_run += 1
            if self._silence_run >= self.end_silence_frames:
                return "endpoint"
        else:
            self._silence_run = 0
        if self._total >= self.max_frames:
            return "max_duration"
        return None

    def audio(self) -> np.ndarray | None:
        if not self._started or len(self._collected) < self.min_speech_frames:
            return None
        return np.concatenate(self._collected)


class EndpointDetector:
    def __init__(self, stt_config=None, frame_ms: int = 80) -> None:
        def _c(a, d):
            return getattr(stt_config, a, d) if stt_config else d

        fps = 1000.0 / frame_ms
        self._end_silence_frames   = max(1, round(_c("end_silence_sec", 1.3) * fps))
        self._max_frames           = max(1, round(_c("max_utterance_sec", 15.0) * fps))
        self._onset_timeout_frames = max(1, round(_c("onset_timeout_sec", 7.0) * fps))
        self._min_speech_frames    = max(1, round(_c("min_speech_sec", 0.3) * fps))
        self._pre_frames           = max(1, round(_c("pre_speech_sec", 0.8) * fps))
        self._silero_threshold     = _c("silero_threshold", 0.5)
        self._rms_threshold        = _c("silence_threshold", 0.010)

        self._backend = _c("vad_backend", "silero")
        self._silero: _SileroVad | None = None
        if self._backend == "silero":
            try:
                self._silero = _SileroVad()
                log.info("Endpointer: silero (neural)")
            except Exception as e:
                self._backend = "rms"
                log.warning("Silero unavailable (%s) — endpointer falling back to RMS", e)

    def _new_sm(self) -> EndpointSM:
        threshold = self._silero_threshold if self._silero is not None else self._rms_threshold
        return EndpointSM(
            threshold            = threshold,
            end_silence_frames   = self._end_silence_frames,
            max_frames           = self._max_frames,
            pre_frames           = self._pre_frames,
            onset_timeout_frames = self._onset_timeout_frames,
            min_speech_frames    = self._min_speech_frames,
        )

    def _score(self, frame: np.ndarray) -> float:
        if self._silero is not None:
            return self._silero.prob(frame)
        return _rms(frame)

    async def capture(self, frame_q: asyncio.Queue, *, preroll: list | None = None) -> tuple[np.ndarray | None, str]:
        """Capture one utterance from a mic frame queue. `preroll` seeds the
        look-back (frames buffered before LISTENING began). Returns (audio, reason)."""
        if self._silero is not None:
            self._silero.reset()
        sm = self._new_sm()
        for f in (preroll or []):
            r = sm.feed(self._score(f), f)
            if r:
                return sm.audio(), r
        while True:
            frame = await frame_q.get()
            reason = sm.feed(self._score(frame), frame)
            if reason:
                return sm.audio(), reason
