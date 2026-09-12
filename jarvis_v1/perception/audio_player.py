"""
AudioPlayer — plays synthesized speech segments, cancellable on barge-in.

A background task plays queued (sample_rate, samples) segments in order. The
critical operation is interrupt(): it stops the current segment immediately
(sd.stop() makes the in-thread sd.wait() return at once) and drops everything
still queued — so when the user barges in, Echo goes silent right away.

Playback is overlap-friendly: the orchestrator enqueues sentence N's audio
while sentence N+1 is still being generated, so speech starts early.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

log = logging.getLogger("jarvis.player")


class AudioPlayer:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._running = False
        self._stop_flag = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        self._running = False
        self.interrupt()
        await self._queue.put(None)
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    def enqueue(self, fs: int, samples: np.ndarray) -> None:
        """Queue a segment for playback. Clears any prior stop state (this is
        fresh speech), so playback resumes after an interruption."""
        self._stop_flag = False
        self._idle.clear()
        self._queue.put_nowait((fs, samples))

    def interrupt(self) -> None:
        """Stop current playback NOW and drop all queued segments."""
        self._stop_flag = True
        try:
            import sounddevice as sd
            sd.stop()
        except Exception as e:
            log.debug("sd.stop failed: %s", e)
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    @property
    def is_idle(self) -> bool:
        return self._idle.is_set()

    async def _loop(self) -> None:
        import sounddevice as sd
        while self._running:
            item = await self._queue.get()
            if item is None:
                break
            if self._stop_flag:
                continue                      # dropped by a prior interrupt()
            fs, samples = item
            try:
                sd.play(samples, fs)
                # Returns on natural completion OR immediately when interrupt()
                # calls sd.stop() — this is what makes barge-in instant.
                await asyncio.to_thread(sd.wait)
            except Exception as e:
                log.warning("playback error: %s", e)
            if self._queue.empty():
                self._idle.set()
