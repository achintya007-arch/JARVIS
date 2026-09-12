"""
MicrophoneInput — the single owner of the microphone.

One persistent sd.InputStream captures 16 kHz mono float32 frames and fans them
out to any number of async consumers (wake detector, endpointer, interruption
monitor). Consumers subscribe/unsubscribe as the voice state changes, so there
is never a second competing stream. The PortAudio callback thread hands frames
to the event loop via call_soon_threadsafe — the loop never blocks on audio.

Frames are `frame_ms` long (default 80 ms = 1280 samples at 16 kHz), which is
openWakeWord's native frame size; the Silero endpointer re-windows to 512.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger("jarvis.mic")

_SAMPLE_RATE = 16_000


class MicrophoneInput:
    def __init__(self, sample_rate: int = _SAMPLE_RATE, frame_ms: int = 80) -> None:
        self.fs = sample_rate
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self._stream: sd.InputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(
            samplerate = self.fs,
            channels   = 1,
            dtype      = "float32",
            blocksize  = self.frame_samples,
            callback   = self._callback,
        )
        self._stream.start()
        log.info("Microphone open | %d Hz, %d-sample frames", self.fs, self.frame_samples)

    async def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("Mic close failed: %s", e)
            self._stream = None

    # ── Fan-out ─────────────────────────────────────────────────────────────
    def subscribe(self, maxsize: int = 50) -> asyncio.Queue:
        """Return a fresh frame queue that receives every subsequent frame.
        Bounded; when full the oldest frame is dropped so consumers always see
        the most recent audio (real-time, no unbounded backlog)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @staticmethod
    def drain(q: asyncio.Queue) -> None:
        """Discard everything currently queued for a consumer (e.g. to reset
        look-back after JARVIS finishes speaking)."""
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break

    # ── Internal ────────────────────────────────────────────────────────────
    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.debug("mic status: %s", status)
        frame = indata[:, 0].copy()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._dispatch, frame)

    def _dispatch(self, frame: np.ndarray) -> None:
        for q in self._subscribers:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()         # drop oldest, keep latest
                    q.put_nowait(frame)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
