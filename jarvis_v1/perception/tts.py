"""
TTSEngine — non-blocking, queue-based TTS with sentence-level streaming.

Backends (selected by TTSConfig.engine):
  - "piper" : local Piper neural TTS (offline, no network). Default.
  - "edge"  : edge-tts (Microsoft cloud neural voices) + miniaudio MP3 decode.

Piper is preferred for latency: it synthesizes the first (usually short) streamed
sentence in ~200–370 ms on CPU with NO network roundtrip, versus edge-tts's
~530 ms first-audio plus network variance. If the Piper package or voice model
is unavailable, the engine falls back to edge-tts automatically, so a fresh
checkout with no model still speaks.

Architecture (backend-agnostic):
  - Background _player_loop() drains an asyncio.Queue of (fs, audio) segments
  - enqueue(text)  → synthesize in thread, push to queue, return immediately
  - speak(text)    → enqueue all sentences, then drain (blocks until done)
  - drain()        → insert sentinel; await until it's processed (all prior audio played)
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis.tts")

# Local Piper voice models live in jarvis_v1/voices/<name>.onnx (+ .onnx.json).
_VOICES_DIR = Path(__file__).resolve().parent.parent / "voices"

# Default neural voice — Christopher is deep and professional
_DEFAULT_VOICE = "en-US-ChristopherNeural"

# Sentence boundary splitter
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")

# Strip markdown before synthesis
_STRIP_RE = re.compile(
    r"```.*?```|`[^`]*`|[*_#~>|\\]|\[([^\]]+)\]\([^)]+\)",
    re.DOTALL,
)


def _clean(text: str) -> str:
    text = _STRIP_RE.sub(r"\1", text)
    text = re.sub(r"\n{2,}", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def _split_sentences(text: str) -> list[str]:
    parts = _SENT_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]


def _rate_to_length_scale(rate: str) -> float:
    """Map an edge-tts-style rate ('-4%', '+10%') to a Piper length_scale.
    Piper length_scale >1 is slower, <1 faster. '-4%' (4% slower) → 1.04."""
    try:
        pct = float(rate.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return 1.0
    return round(1.0 - pct / 100.0, 3)


class TTSEngine:
    # edge-tts voice names for the Piper model names (used by the edge backend
    # and as the fallback when a Piper model can't be loaded).
    _VOICE_MAP = {
        "en_US-ryan-high":     "en-US-ChristopherNeural",
        "en_US-lessac-medium": "en-US-GuyNeural",
    }

    def __init__(self, config=None):
        voice = getattr(config, "voice", None) if config else None
        self._engine = (getattr(config, "engine", "piper") if config else "piper").lower()

        # Raw (Piper) model name, e.g. "en_US-ryan-high" — resolved to a .onnx
        # under voices/ at initialize(). Kept even in edge mode for the map.
        self._model_name = voice if (voice and not voice.startswith("en-")) else "en_US-ryan-high"

        # edge-tts voice: either an explicit en-US-* name, a mapped model name,
        # or the default. Also the fallback voice if Piper fails to load.
        if voice and voice.startswith("en-"):
            self._voice = voice
        else:
            self._voice = self._VOICE_MAP.get(voice, _DEFAULT_VOICE)

        # Prosody — composed cadence (see TTSConfig).
        self._rate  = getattr(config, "rate", "+0%") if config else "+0%"
        self._pitch = getattr(config, "pitch", "+0Hz") if config else "+0Hz"
        self._length_scale = _rate_to_length_scale(self._rate)

        self._piper = None                   # loaded PiperVoice (piper engine only)
        self._queue:  asyncio.Queue = None   # type: ignore[assignment]
        self._player: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._queue  = asyncio.Queue()
        self._player = asyncio.create_task(self._player_loop())

        if self._engine == "piper":
            self._piper = await asyncio.to_thread(self._load_piper)
            if self._piper is not None:
                log.info(
                    "TTS ready | engine=piper voice=%s (local, offline) length_scale=%.2f",
                    self._model_name, self._length_scale,
                )
                return
            # Load failed — degrade to edge-tts so the assistant still speaks.
            self._engine = "edge"
            log.warning("Piper unavailable — falling back to edge-tts (voice=%s)", self._voice)

        log.info("TTS ready | engine=edge voice=%s (edge-tts neural)", self._voice)

    def _load_piper(self):
        """Load the Piper voice model from voices/<name>.onnx. Returns the
        PiperVoice, or None if the package or model file is missing (caller
        then falls back to edge-tts)."""
        model = _VOICES_DIR / f"{self._model_name}.onnx"
        if not model.exists():
            log.warning("Piper model not found: %s", model)
            return None
        try:
            from piper import PiperVoice
            return PiperVoice.load(str(model))
        except Exception as e:
            log.warning("Piper load failed (%s): %s", self._model_name, e)
            return None

    async def shutdown(self) -> None:
        if self._queue:
            await self._queue.put(None)
        if self._player:
            await self._player

    # ── Public API ────────────────────────────────────────────────────────────

    async def enqueue(self, text: str) -> None:
        text = _clean(text)
        if not text:
            return
        sentences = _split_sentences(text) or [text]
        for sentence in sentences:
            audio = await asyncio.to_thread(self._synthesize, sentence)
            if audio is not None:
                await self._queue.put(audio)

    async def speak(self, text: str) -> None:
        await self.enqueue(text)
        await self.drain()

    async def synthesize(self, text: str) -> tuple[int, np.ndarray] | None:
        """Synthesize one piece of text → (sample_rate, float32 mono samples),
        WITHOUT queueing/playing. The V3 voice layer uses this and drives
        playback through its own AudioPlayer; V2 uses enqueue/speak instead.
        Lazy-loads Piper on first call so callers need not run initialize()."""
        text = _clean(text)
        if not text:
            return None
        if self._piper is None and self._engine == "piper":
            self._piper = await asyncio.to_thread(self._load_piper)
            if self._piper is None:
                self._engine = "edge"
        return await asyncio.to_thread(self._synthesize, text)

    async def drain(self) -> None:
        done = asyncio.Event()
        await self._queue.put(done)
        await done.wait()

    async def interrupt(self) -> None:
        """
        Barge-in: stop current playback and flush any queued audio immediately.
        Pending drain() events are resolved (so callers awaiting them don't hang)
        and any shutdown sentinel is preserved.
        """
        if self._queue is None:
            return
        had_sentinel = False
        try:
            while True:
                item = self._queue.get_nowait()
                if isinstance(item, asyncio.Event):
                    item.set()          # unblock any drain() waiter
                elif item is None:
                    had_sentinel = True  # preserve shutdown request
        except asyncio.QueueEmpty:
            pass
        sd.stop()                        # cut the currently-playing segment
        if had_sentinel:
            await self._queue.put(None)

    # ── Player loop ───────────────────────────────────────────────────────────

    async def _player_loop(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            if isinstance(item, asyncio.Event):
                item.set()
                continue
            fs, audio = item
            sd.play(audio, fs)
            # Fix #1: timeout prevents a hung audio device from stalling the queue forever.
            try:
                await asyncio.wait_for(asyncio.to_thread(sd.wait), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("TTS playback timed out (10s) — skipping segment and continuing")
                sd.stop()

    # ── Synthesis ──────────────────────────────────────────────────────────────

    def _synthesize(self, text: str) -> tuple[int, np.ndarray] | None:
        """Dispatch to the active backend. Always called inside
        asyncio.to_thread() — safe to block here."""
        if not text.strip():
            return None
        if self._piper is not None:
            return self._synthesize_piper(text)
        return self._synthesize_edge(text)

    def _synthesize_piper(self, text: str) -> tuple[int, np.ndarray] | None:
        """Local Piper synthesis → (sample_rate, float32 mono samples)."""
        try:
            from piper import SynthesisConfig

            syn = SynthesisConfig(length_scale=self._length_scale)
            chunks = [
                chunk.audio_int16_array
                for chunk in self._piper.synthesize(text, syn_config=syn)
            ]
            if not chunks:
                return None
            pcm = np.concatenate(chunks)
            audio = pcm.astype(np.float32) / 32768.0
            return self._piper.config.sample_rate, audio
        except Exception as e:
            log.error("Piper synthesis error: %s", e)
            return None

    def _synthesize_edge(self, text: str) -> tuple[int, np.ndarray] | None:
        """
        Synthesize text via edge-tts, decode MP3 with miniaudio.
        Always called inside asyncio.to_thread() — safe to block here.
        """
        try:
            import miniaudio

            # edge-tts is async — run it in a fresh event loop in this thread
            mp3_bytes = asyncio.run(self._fetch_audio(text))
            if not mp3_bytes:
                return None

            # Decode MP3 → float32 samples (no ffmpeg needed)
            decoded = miniaudio.decode(
                mp3_bytes,
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=1,
            )
            audio = np.frombuffer(decoded.samples, dtype=np.float32)
            return decoded.sample_rate, audio

        except Exception as e:
            log.error("TTS synthesis error: %s", e)
            return None

    async def _fetch_audio(self, text: str) -> bytes:
        """
        Fetch MP3 audio bytes from edge-tts with retry on transient failures.
        Fix #2: up to 3 attempts with exponential backoff (0.5s → 1s → 2s).
        """
        import asyncio as _asyncio

        import edge_tts

        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(
                    text, self._voice, rate=self._rate, pitch=self._pitch,
                )
                mp3_data = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        mp3_data += chunk["data"]
                if mp3_data:
                    return mp3_data
                raise RuntimeError("No audio was received from edge-tts")
            except Exception as e:
                last_exc = e
                if attempt < 2:
                    wait = 0.5 * (2 ** attempt)   # 0.5s, 1s
                    log.warning("edge-tts attempt %d/3 failed (%s) — retrying in %.1fs", attempt + 1, e, wait)
                    await _asyncio.sleep(wait)

        log.error("edge-tts failed after 3 attempts: %s", last_exc)
        return b""
