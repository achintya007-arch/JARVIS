"""
Voice rebuild — AudioPlayer: playback, interruption, and cancellation of
pending speech. sounddevice is stubbed so no real audio is produced; the stub
models sd.wait() returning when sd.stop() is called — which is exactly how
barge-in cuts playback instantly.
"""

import asyncio
import threading

import numpy as np
import pytest

pytest.importorskip("sounddevice")
import sounddevice as sd

from perception.audio_player import AudioPlayer


def _seg(n=8):
    return 16000, np.zeros(n, dtype=np.float32)


class TestPlayback:
    async def test_enqueued_segment_is_played(self, monkeypatch):
        played = []
        monkeypatch.setattr(sd, "play", lambda s, fs: played.append((fs, len(s))))
        monkeypatch.setattr(sd, "wait", lambda: None)       # completes immediately
        monkeypatch.setattr(sd, "stop", lambda: None)

        player = AudioPlayer()
        await player.start()
        player.enqueue(*_seg())
        await asyncio.wait_for(player.wait_idle(), timeout=2.0)
        await player.shutdown()

        assert len(played) == 1


class TestInterruption:
    async def test_interrupt_stops_current_and_drops_queue(self, monkeypatch):
        played = []
        evt = threading.Event()
        monkeypatch.setattr(sd, "play", lambda s, fs: played.append((fs, len(s))))
        monkeypatch.setattr(sd, "wait", lambda: evt.wait(2.0))   # blocks until stop()
        monkeypatch.setattr(sd, "stop", evt.set)

        player = AudioPlayer()
        await player.start()
        player.enqueue(*_seg())          # seg 1 — starts playing (blocks in wait)
        player.enqueue(*_seg())          # seg 2 — pending
        player.enqueue(*_seg())          # seg 3 — pending
        await asyncio.sleep(0.05)        # let seg 1 start

        player.interrupt()               # barge-in
        await asyncio.sleep(0.05)

        assert player.is_idle
        assert player._queue.empty()     # pending speech cancelled
        assert len(played) == 1          # only seg 1 ever started
        await player.shutdown()

    async def test_playback_resumes_after_interrupt(self, monkeypatch):
        played = []
        monkeypatch.setattr(sd, "play", lambda s, fs: played.append(len(s)))
        monkeypatch.setattr(sd, "wait", lambda: None)
        monkeypatch.setattr(sd, "stop", lambda: None)

        player = AudioPlayer()
        await player.start()
        player.interrupt()               # stop state
        player.enqueue(*_seg())          # new speech clears the stop state
        await asyncio.wait_for(player.wait_idle(), timeout=2.0)
        assert len(played) == 1
        await player.shutdown()
