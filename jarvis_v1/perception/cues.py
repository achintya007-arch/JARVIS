"""
perception/cues.py — short non-speech audio earcons.

A wake chime gives immediate feedback that JARVIS is listening (movie-accurate)
without playing speech that the open mic could transcribe back as text. Fully
synthesized — no files, no network.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("jarvis.cues")

_FS = 16_000


def _tone(freq: float, secs: float, amp: float = 0.18) -> np.ndarray:
    t = np.linspace(0, secs, int(_FS * secs), endpoint=False)
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    # Short fade in/out to avoid clicks
    fade = max(1, int(_FS * 0.008))
    wave[:fade]  *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
    wave[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return wave


def play_chime() -> None:
    """Two quick ascending blips ('ready to listen'). Safe to call in a thread;
    silently no-ops if audio output is unavailable."""
    try:
        import sounddevice as sd
        chime = np.concatenate([_tone(660, 0.07), _tone(990, 0.09)])
        sd.play(chime, _FS)
        sd.wait()
    except Exception as e:
        log.debug("Chime playback skipped: %s", e)
