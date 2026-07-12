"""
STTEngine — Voice Activity Detection recording + faster-whisper transcription.

Key design choices:
  - ONE persistent sd.InputStream opened at initialize() and kept alive forever.
    This eliminates the 100-200ms stream-startup gap that was eating the first
    syllable of every utterance.
  - Pre-buffer accumulates continuously; at the start of each _record_vad call
    we drain the queue and seed the pre-buffer with whatever accumulated during
    the previous transcription, giving at least 800ms of look-back.
  - Whisper initial_prompt seeds the vocabulary with domain-specific terms so
    "reminders" doesn't become "remainders", "Jarvis" isn't mangled, etc.

All blocking I/O runs in asyncio.to_thread() — event loop never stalls.
"""

from __future__ import annotations

import asyncio
import logging
import queue as stdlib_queue
from typing import Optional

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

log = logging.getLogger("jarvis.stt")

# ── VAD tuning ─────────────────────────────────────────────────────────────────
_SAMPLE_RATE        = 16_000
_CHUNK_SECS         = 0.1          # 100ms per chunk
_CHUNK_SAMPLES      = int(_SAMPLE_RATE * _CHUNK_SECS)
_SILENCE_THRESHOLD  = 0.011        # RMS amplitude
_SILENCE_CHUNKS     = 8            # 0.8s of silence → stop
_PRE_SPEECH_CHUNKS  = 8            # keep 800ms before speech onset (was 4/400ms)
_MAX_CHUNKS         = 80           # 8s absolute max
_MIN_SPEECH_CHUNKS  = 3            # ignore utterances shorter than 300ms

# Vocabulary hint — biases Whisper toward these words/phrases.
# Dramatically reduces domain-specific mishears.
_INITIAL_PROMPT = (
    "Jarvis, VS Code, Visual Studio Code, reminder, reminders, "
    "clipboard, volume, Spotify, Discord, YouTube, LinkedIn, "
    "open, close, launch, timer, goal, exam, deadline"
)

# ── Hallucination gating ─────────────────────────────────────────────────────
# Whisper invents text on silence/noise. Reject segments the model itself is
# unsure are speech (high no_speech_prob) or that decoded poorly (low avg
# logprob), plus the classic phantom outputs it emits on near-silence.
_NO_SPEECH_MAX   = 0.6     # drop segments the model thinks are >60% likely non-speech
_AVG_LOGPROB_MIN = -1.0    # drop segments that decoded with low confidence
_HALLUCINATION_PHRASES = frozenset({
    "you", "thank you", "thanks for watching", "thanks", "bye", "okay", "ok",
    "please subscribe", "subtitles by the amara.org community", ".",
    "so", "yeah", "hmm", "uh", "um", "the", "i", "youtube",
})


def _is_hallucination(text: str) -> bool:
    """True if the WHOLE transcript is a known phantom emitted on silence."""
    normalized = text.lower().strip(" .,!?-—\"'")
    if not normalized:               # punctuation-only (e.g. ".") is never a command
        return True
    return normalized in _HALLUCINATION_PHRASES


class STTEngine:
    def __init__(self, config=None):
        self.fs                = _SAMPLE_RATE
        self.silence_threshold = _SILENCE_THRESHOLD

        model_size   = getattr(config, "model",        "base.en") if config else "base.en"
        device       = getattr(config, "device",       "cuda")    if config else "cuda"
        compute_type = getattr(config, "compute_type", "int8")    if config else "int8"

        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        log.info("Whisper loaded: %s on %s (%s)", model_size, device, compute_type)

        # Persistent mic stream — kept alive across all utterances.
        self._chunk_q: stdlib_queue.Queue[np.ndarray] = stdlib_queue.Queue()
        self._stream: Optional[sd.InputStream] = None

    async def initialize(self) -> None:
        """Open the mic stream once and keep it running."""
        self._stream = sd.InputStream(
            samplerate  = self.fs,
            channels    = 1,
            dtype       = "float32",
            blocksize   = _CHUNK_SAMPLES,
            callback    = self._mic_callback,
        )
        self._stream.start()
        log.info("STT mic stream open (persistent)")

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Called by sounddevice on every 100ms chunk. Runs in its own thread."""
        self._chunk_q.put(indata[:, 0].copy())

    # ── Shared audio feed (single mic owner) ────────────────────────────────────
    # The wake-word detector consumes from THIS queue instead of opening its own
    # competing stream — one owner of the microphone, no contention.

    def read_window(self, seconds: float, timeout: float = 1.0) -> Optional[np.ndarray]:
        """
        Block until ~`seconds` of audio has accumulated, then return it as one
        array. Used by the wake loop to classify rolling windows. Returns None
        if nothing arrived within `timeout`. Safe to call inside asyncio.to_thread.
        """
        n = max(1, int(round(seconds / _CHUNK_SECS)))
        chunks: list[np.ndarray] = []
        for _ in range(n):
            try:
                chunks.append(self._chunk_q.get(timeout=timeout))
            except stdlib_queue.Empty:
                break
        if not chunks:
            return None
        return np.concatenate(chunks)

    def flush(self) -> None:
        """Discard any buffered audio (e.g. the wake word + chime tail) so the
        following command capture starts clean."""
        while not self._chunk_q.empty():
            try:
                self._chunk_q.get_nowait()
            except stdlib_queue.Empty:
                break

    # ── Recording ─────────────────────────────────────────────────────────────

    async def record_utterance(self) -> np.ndarray:
        """
        VAD-gated recording. Blocks until speech is detected and ends.
        Runs entirely in a thread — never blocks the event loop.
        """
        return await asyncio.to_thread(self._record_vad)

    def _record_vad(self) -> np.ndarray:
        """
        Synchronous VAD loop. Reads from the shared persistent mic queue.
        Safe to run in asyncio.to_thread().
        """
        print("[STT] Listening...", flush=True)

        # Drain whatever accumulated while the previous transcription ran.
        # Keep the last PRE_SPEECH_CHUNKS worth as the initial pre-buffer —
        # this seeds look-back so the first word isn't clipped.
        stale: list[np.ndarray] = []
        while not self._chunk_q.empty():
            try:
                stale.append(self._chunk_q.get_nowait())
            except stdlib_queue.Empty:
                break
        pre_buffer: list[np.ndarray] = stale[-_PRE_SPEECH_CHUNKS:]

        audio_chunks:  list[np.ndarray] = []
        speech_started = False
        silence_count  = 0
        total_chunks   = 0

        while total_chunks < _MAX_CHUNKS:
            try:
                chunk = self._chunk_q.get(timeout=1.0)
            except stdlib_queue.Empty:
                log.debug("STT: chunk timeout")
                break

            total_chunks += 1
            # Clip to float32 range before RMS to avoid overflow warnings
            chunk = np.clip(chunk, -1.0, 1.0)
            amplitude = float(np.sqrt(np.mean(chunk ** 2)))

            if not speech_started:
                pre_buffer.append(chunk)
                if len(pre_buffer) > _PRE_SPEECH_CHUNKS:
                    pre_buffer.pop(0)

                if amplitude > self.silence_threshold:
                    speech_started = True
                    audio_chunks.extend(pre_buffer)
                    audio_chunks.append(chunk)
                    pre_buffer = []
                    log.debug("STT: speech onset (amp=%.4f)", amplitude)
            else:
                audio_chunks.append(chunk)

                if amplitude < self.silence_threshold:
                    silence_count += 1
                    if silence_count >= _SILENCE_CHUNKS:
                        log.debug("STT: end of speech (%.1fs)", len(audio_chunks) * _CHUNK_SECS)
                        break
                else:
                    silence_count = 0

        if not audio_chunks or len(audio_chunks) < _MIN_SPEECH_CHUNKS:
            return np.zeros(_CHUNK_SAMPLES, dtype="float32")

        return np.concatenate(audio_chunks)

    # ── Transcription ──────────────────────────────────────────────────────────

    async def transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe audio array. Returns empty string for near-silence.
        Runs Whisper in thread to keep event loop free.
        """
        if len(audio) < self.fs * 0.3:
            return ""
        if float(np.sqrt(np.mean(np.clip(audio, -1.0, 1.0) ** 2))) < self.silence_threshold * 0.5:
            return ""

        def _run() -> str:
            segments, _ = self.model.transcribe(
                audio,
                beam_size      = 5,
                language       = "en",
                initial_prompt = _INITIAL_PROMPT,
                vad_filter     = True,
                vad_parameters = {"min_silence_duration_ms": 300},
            )
            kept: list[str] = []
            for seg in segments:
                # Confidence gates — drop segments the model itself distrusts.
                if getattr(seg, "no_speech_prob", 0.0) > _NO_SPEECH_MAX:
                    log.debug("STT drop (no_speech=%.2f): %r", seg.no_speech_prob, seg.text)
                    continue
                if getattr(seg, "avg_logprob", 0.0) < _AVG_LOGPROB_MIN:
                    log.debug("STT drop (logprob=%.2f): %r", seg.avg_logprob, seg.text)
                    continue
                kept.append(seg.text)

            text = "".join(kept).strip()
            if text and _is_hallucination(text):
                log.debug("STT drop (hallucination): %r", text)
                return ""
            return text

        return await asyncio.to_thread(_run)
