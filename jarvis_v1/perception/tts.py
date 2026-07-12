"""
TTSEngine — non-blocking, queue-based TTS with sentence-level streaming.

Backend: edge-tts (Microsoft neural voices) + miniaudio for MP3 decode.

Architecture:
  - Background _player_loop() drains an asyncio.Queue of (fs, audio) segments
  - enqueue(text)  → synthesize in thread, push to queue, return immediately
  - speak(text)    → enqueue all sentences, then drain (blocks until done)
  - drain()        → insert sentinel; await until it's processed (all prior audio played)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis.tts")

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


class TTSEngine:
    def __init__(self, config=None):
        voice = getattr(config, "voice", None) if config else None
        # Map old piper voice names to edge-tts voices
        _VOICE_MAP = {
            "en_US-ryan-high":    "en-US-ChristopherNeural",
            "en_US-lessac-medium": "en-US-GuyNeural",
        }
        if voice and voice.startswith("en-"):       # already an edge-tts voice
            self._voice = voice
        elif voice in _VOICE_MAP:
            self._voice = _VOICE_MAP[voice]
        else:
            self._voice = _DEFAULT_VOICE

        # Prosody — composed cadence (see TTSConfig).
        self._rate  = getattr(config, "rate", "+0%") if config else "+0%"
        self._pitch = getattr(config, "pitch", "+0Hz") if config else "+0Hz"

        self._queue:  asyncio.Queue = None   # type: ignore[assignment]
        self._player: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._queue  = asyncio.Queue()
        self._player = asyncio.create_task(self._player_loop())
        log.info("TTS ready | voice=%s (edge-tts neural)", self._voice)

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

    # ── Synthesis (edge-tts + miniaudio) ──────────────────────────────────────

    def _synthesize(self, text: str) -> Optional[tuple[int, np.ndarray]]:
        """
        Synthesize text via edge-tts, decode MP3 with miniaudio.
        Always called inside asyncio.to_thread() — safe to block here.
        """
        if not text.strip():
            return None
        try:
            import edge_tts
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
