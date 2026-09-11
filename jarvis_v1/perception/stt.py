"""
STTEngine — adaptive Voice-Activity recording + faster-whisper transcription.

Key design choices:
  - ONE persistent sd.InputStream opened at initialize() and kept alive forever.
    This eliminates the 100-200ms stream-startup gap that was eating the first
    syllable of every utterance.
  - Pre-buffer accumulates continuously; at the start of each capture we drain
    the queue and seed the pre-buffer with whatever accumulated during the
    previous transcription, giving look-back so the first word isn't clipped.
  - ADAPTIVE silence threshold: the trigger is calibrated from the room's noise
    floor each capture (bounded to [silence_threshold, silence_threshold *
    onset_multiplier]), so a quiet mic still registers and a noisy room still
    ends. Replaces the old fixed 0.011 RMS that only worked for one setup.
  - End-of-speech uses a longer trailing-silence window (default 1.3s) so a
    natural mid-sentence pause no longer truncates the command.
  - Whisper initial_prompt seeds domain vocabulary so "reminders" doesn't become
    "remainders", "Jarvis" isn't mangled, etc.

The VAD decision logic lives in the pure _vad_collect() state machine, which is
unit-tested with synthetic chunks (no mic/model needed). All blocking I/O runs
in asyncio.to_thread() — the event loop never stalls.
"""

from __future__ import annotations

import asyncio
import logging
import queue as stdlib_queue
from collections import deque
from collections.abc import Callable

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

log = logging.getLogger("jarvis.stt")

# ── Fixed audio-format constants ─────────────────────────────────────────────
_SAMPLE_RATE   = 16_000
_CHUNK_SECS    = 0.1                       # 100ms per chunk
_CHUNK_SAMPLES = int(_SAMPLE_RATE * _CHUNK_SECS)

# Defaults used when no config is supplied (mirrors STTConfig).
_DEF_SILENCE_THRESHOLD = 0.010
_DEF_ONSET_MULT        = 2.2
_DEF_END_SILENCE_SEC   = 1.3
_DEF_MAX_UTTER_SEC     = 15.0
_DEF_ONSET_TIMEOUT_SEC = 7.0
_DEF_MIN_SPEECH_SEC    = 0.3
_DEF_PRE_SPEECH_SEC    = 0.8
_DEF_NO_SPEECH_MAX     = 0.6
_DEF_AVG_LOGPROB_MIN   = -1.1
_ONSET_DEBOUNCE_CHUNKS = 2                 # consecutive loud chunks to confirm onset
_END_HYSTERESIS        = 0.6               # end threshold = onset_threshold * this

# Vocabulary hint — biases Whisper toward these words/phrases.
_INITIAL_PROMPT = (
    "Jarvis, VS Code, Visual Studio Code, reminder, reminders, "
    "clipboard, volume, Spotify, Discord, YouTube, LinkedIn, "
    "open, close, launch, timer, goal, exam, deadline"
)

# ── Hallucination gating ─────────────────────────────────────────────────────
# Whisper invents text on silence/noise. Reject segments the model itself is
# unsure are speech, plus the classic phantom outputs it emits on near-silence.
_HALLUCINATION_PHRASES = frozenset({
    "you", "thank you", "thanks for watching", "thanks", "bye", "okay", "ok",
    "please subscribe", "subtitles by the amara.org community", ".",
    "so", "yeah", "hmm", "uh", "um", "the", "i", "youtube",
})


def _rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.clip(samples, -1.0, 1.0) ** 2)))


def _is_hallucination(text: str) -> bool:
    """True if the WHOLE transcript is a known phantom emitted on silence."""
    normalized = text.lower().strip(" .,!?-—\"'")
    if not normalized:               # punctuation-only (e.g. ".") is never a command
        return True
    return normalized in _HALLUCINATION_PHRASES


def _vad_collect(
    next_chunk: Callable[[], np.ndarray | None],
    *,
    base_threshold: float,
    onset_multiplier: float,
    adaptive: bool,
    end_silence_chunks: int,
    max_chunks: int,
    pre_chunks: int,
    onset_timeout_chunks: int,
    min_speech_chunks: int,
) -> tuple[np.ndarray | None, dict]:
    """
    Pure VAD state machine — no mic, no model, fully unit-testable.

    `next_chunk()` returns the next mono float32 chunk, or None on stream
    stall/end. Returns (audio | None, info) where info carries the calibrated
    threshold, onset amplitude, chunk count, and the stop `reason`
    ("end_silence" | "max_duration" | "onset_timeout" | "stream_end" | "too_short").
    """
    pre_buffer: deque[np.ndarray] = deque(maxlen=max(1, pre_chunks))
    collected: list[np.ndarray] = []
    quietest = base_threshold          # noise floor, only ever tracked downward
    onset_threshold = base_threshold
    started = False
    onset_run = 0
    silence_run = 0
    onset_wait = 0
    total = 0
    onset_amp = 0.0

    while total < max_chunks:
        chunk = next_chunk()
        if chunk is None:
            reason = "stream_end" if started else "onset_timeout"
            break
        total += 1
        amp = _rms(chunk)

        if not started:
            # Calibrate: track the quietest pre-speech chunk (bounded to base),
            # then trigger at floor * multiplier — but never below base.
            quietest = min(quietest, amp)
            floor = min(quietest, base_threshold)
            onset_threshold = max(base_threshold, floor * onset_multiplier) if adaptive else base_threshold

            pre_buffer.append(chunk)
            onset_wait += 1

            if amp > onset_threshold:
                onset_run += 1
                if onset_run >= _ONSET_DEBOUNCE_CHUNKS:
                    started = True
                    onset_amp = amp
                    collected.extend(pre_buffer)
                    pre_buffer.clear()
            else:
                onset_run = 0

            if onset_wait >= onset_timeout_chunks:
                reason = "onset_timeout"
                break
        else:
            collected.append(chunk)
            if amp < onset_threshold * _END_HYSTERESIS:
                silence_run += 1
                if silence_run >= end_silence_chunks:
                    reason = "end_silence"
                    break
            else:
                silence_run = 0
    else:
        reason = "max_duration"

    info = {
        "reason": reason,
        "threshold": round(onset_threshold, 5),
        "onset_amp": round(onset_amp, 5),
        "chunks": len(collected),
    }
    if not started or len(collected) < min_speech_chunks:
        info["reason"] = "too_short" if started else info["reason"]
        return None, info
    return np.concatenate(collected), info


class STTEngine:
    def __init__(self, config=None):
        self.fs = _SAMPLE_RATE

        def _c(attr, default):
            return getattr(config, attr, default) if config else default

        model_size   = _c("model",        "base.en")
        device       = _c("device",       "cuda")
        compute_type = _c("compute_type", "int8")

        # Capture tunables (see STTConfig).
        self.silence_threshold = _c("silence_threshold", _DEF_SILENCE_THRESHOLD)
        self._adaptive         = _c("adaptive_threshold", True)
        self._onset_mult       = _c("onset_multiplier", _DEF_ONSET_MULT)
        self._end_silence_chunks   = max(1, round(_c("end_silence_sec", _DEF_END_SILENCE_SEC) / _CHUNK_SECS))
        self._max_chunks           = max(1, round(_c("max_utterance_sec", _DEF_MAX_UTTER_SEC) / _CHUNK_SECS))
        self._onset_timeout_chunks = max(1, round(_c("onset_timeout_sec", _DEF_ONSET_TIMEOUT_SEC) / _CHUNK_SECS))
        self._min_speech_chunks    = max(1, round(_c("min_speech_sec", _DEF_MIN_SPEECH_SEC) / _CHUNK_SECS))
        self._pre_chunks           = max(1, round(_c("pre_speech_sec", _DEF_PRE_SPEECH_SEC) / _CHUNK_SECS))

        # Transcription gates.
        self._vad_filter      = _c("vad_filter", False)
        self._no_speech_max   = _c("no_speech_max", _DEF_NO_SPEECH_MAX)
        self._avg_logprob_min = _c("avg_logprob_min", _DEF_AVG_LOGPROB_MIN)
        self._debug           = _c("debug_audio", False)

        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        log.info("Whisper loaded: %s on %s (%s)", model_size, device, compute_type)

        # Persistent mic stream — kept alive across all utterances.
        self._chunk_q: stdlib_queue.Queue[np.ndarray] = stdlib_queue.Queue()
        self._stream: sd.InputStream | None = None

    async def initialize(self) -> None:
        """Open the mic stream once and keep it running."""
        self._stream = sd.InputStream(
            samplerate = self.fs,
            channels   = 1,
            dtype      = "float32",
            blocksize  = _CHUNK_SAMPLES,
            callback   = self._mic_callback,
        )
        self._stream.start()
        log.info(
            "STT mic stream open (persistent) | adaptive=%s base_thresh=%.3f "
            "end_silence=%.1fs max=%.0fs",
            self._adaptive, self.silence_threshold,
            self._end_silence_chunks * _CHUNK_SECS, self._max_chunks * _CHUNK_SECS,
        )

    async def shutdown(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("STT stream close failed: %s", e)

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Called by sounddevice on every 100ms chunk. Runs in its own thread."""
        if status:
            log.debug("mic status: %s", status)
        self._chunk_q.put(indata[:, 0].copy())

    # ── Shared audio feed (single mic owner) ────────────────────────────────────
    # The wake-word detector consumes from THIS queue instead of opening its own
    # competing stream — one owner of the microphone, no contention.

    def read_window(self, seconds: float, timeout: float = 1.0) -> np.ndarray | None:
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
        Adaptive VAD-gated recording. Blocks until speech is detected and ends.
        Runs entirely in a thread — never blocks the event loop.
        """
        return await asyncio.to_thread(self._record_vad)

    def _record_vad(self) -> np.ndarray:
        """Synchronous capture built on the pure _vad_collect() state machine,
        reading from the shared persistent mic queue."""
        print("[STT] Listening...", flush=True)

        # Seed the pre-buffer with the tail of whatever accumulated during the
        # previous transcription, so look-back covers the first word.
        stale: list[np.ndarray] = []
        while not self._chunk_q.empty():
            try:
                stale.append(self._chunk_q.get_nowait())
            except stdlib_queue.Empty:
                break
        seed = deque(stale[-self._pre_chunks:])

        def _next() -> np.ndarray | None:
            if seed:
                return seed.popleft()
            try:
                return self._chunk_q.get(timeout=1.0)
            except stdlib_queue.Empty:
                return None

        audio, info = _vad_collect(
            _next,
            base_threshold       = self.silence_threshold,
            onset_multiplier     = self._onset_mult,
            adaptive             = self._adaptive,
            end_silence_chunks   = self._end_silence_chunks,
            max_chunks           = self._max_chunks,
            pre_chunks           = self._pre_chunks,
            onset_timeout_chunks = self._onset_timeout_chunks,
            min_speech_chunks    = self._min_speech_chunks,
        )

        if self._debug:
            dur = info["chunks"] * _CHUNK_SECS
            print(
                f"[STT] stop={info['reason']} thresh={info['threshold']:.4f} "
                f"onset_amp={info['onset_amp']:.4f} dur={dur:.1f}s",
                flush=True,
            )
        log.debug("VAD: %s", info)

        if audio is None:
            return np.zeros(_CHUNK_SAMPLES, dtype="float32")
        return audio

    # ── Transcription ──────────────────────────────────────────────────────────

    async def transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe audio array. Returns empty string for near-silence.
        Runs Whisper in a thread to keep the event loop free.
        """
        if len(audio) < self.fs * 0.3:
            return ""
        if _rms(audio) < self.silence_threshold * 0.5:
            return ""

        def _run() -> str:
            segments, _ = self.model.transcribe(
                audio,
                beam_size      = 5,
                language       = "en",
                initial_prompt = _INITIAL_PROMPT,
                vad_filter     = self._vad_filter,
                vad_parameters = {"min_silence_duration_ms": 500},
            )
            kept: list[str] = []
            for seg in segments:
                nsp = getattr(seg, "no_speech_prob", 0.0)
                alp = getattr(seg, "avg_logprob", 0.0)
                if nsp > self._no_speech_max:
                    if self._debug:
                        print(f"[STT] drop no_speech={nsp:.2f}: {seg.text!r}", flush=True)
                    continue
                if alp < self._avg_logprob_min:
                    if self._debug:
                        print(f"[STT] drop logprob={alp:.2f}: {seg.text!r}", flush=True)
                    continue
                kept.append(seg.text)

            text = "".join(kept).strip()
            if text and _is_hallucination(text):
                log.debug("STT drop (hallucination): %r", text)
                return ""
            return text

        return await asyncio.to_thread(_run)
