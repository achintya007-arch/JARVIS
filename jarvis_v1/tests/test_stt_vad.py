"""
Adaptive VAD state machine (_vad_collect) — pure logic, no mic/model/GPU.

Pins the behavior that makes the voice engine "listen completely" (mid-sentence
pauses don't truncate; onset needs real speech, not a blip) and "listen
properly" (the trigger adapts to the room's noise floor, bounded sensibly).
"""

import numpy as np

from perception.stt import _vad_collect


def _chunk(amp: float) -> np.ndarray:
    # A constant-amplitude array has RMS == |amp|, so tests can set "loudness".
    return np.full(4, amp, dtype=np.float32)


def _source(amps):
    it = iter(amps)

    def _next():
        try:
            return _chunk(next(it))
        except StopIteration:
            return None
    return _next


_BASE = dict(
    base_threshold=0.01, onset_multiplier=2.0, adaptive=True,
    end_silence_chunks=3, max_chunks=100, pre_chunks=2,
    onset_timeout_chunks=50, min_speech_chunks=1,
)


def _run(amps, **over):
    params = {**_BASE, **over}
    return _vad_collect(_source(amps), **params)


class TestOnsetAndEnd:
    def test_basic_capture_ends_on_silence(self):
        # quiet, quiet, LOUD LOUD LOUD, then 3 silent → end_silence
        audio, info = _run([0.0, 0.0, 0.1, 0.1, 0.1, 0.0, 0.0, 0.0])
        assert audio is not None
        assert info["reason"] == "end_silence"

    def test_single_spike_does_not_trigger(self):
        # One loud blip between silence must NOT start capture (debounce = 2).
        audio, info = _run([0.0, 0.1, 0.0, 0.0, 0.0], onset_timeout_chunks=5)
        assert audio is None
        assert info["reason"] == "onset_timeout"

    def test_no_speech_times_out(self):
        audio, info = _run([0.0] * 6, onset_timeout_chunks=5)
        assert audio is None
        assert info["reason"] == "onset_timeout"


class TestMidSentencePause:
    def test_short_pause_does_not_truncate(self):
        # LOUD x3, pause x2 (< end_silence_chunks=3), LOUD x2, silence x3 → one
        # utterance, ended only by the final long silence.
        amps = [0.1, 0.1, 0.1, 0.0, 0.0, 0.1, 0.1, 0.0, 0.0, 0.0]
        audio, info = _run(amps)
        assert audio is not None
        assert info["reason"] == "end_silence"
        # The captured audio must span past the mid pause (≥ 7 chunks of speech).
        assert info["chunks"] >= 7


class TestDurationBounds:
    def test_max_duration_cap(self):
        audio, info = _run([0.1] * 10, max_chunks=5)
        assert audio is not None
        assert info["reason"] == "max_duration"

    def test_too_short_utterance_rejected(self):
        # Onsets then the stream ends before min_speech_chunks is met.
        audio, info = _run([0.1, 0.1], min_speech_chunks=6)
        assert audio is None
        assert info["reason"] == "too_short"


class TestAdaptiveThreshold:
    def test_quiet_room_uses_base_threshold(self):
        # A near-silent floor keeps the trigger at the base floor (most sensitive).
        _, info = _run([0.001, 0.001, 0.05, 0.05, 0.0, 0.0, 0.0])
        assert info["threshold"] == 0.01   # == base_threshold

    def test_noisy_room_raises_threshold(self):
        # Ambient sitting at the base floor raises the trigger to base*multiplier,
        # so near-floor noise won't be mistaken for speech.
        _, info = _run([0.01, 0.01, 0.1, 0.1, 0.0, 0.0, 0.0])
        assert info["threshold"] == 0.02   # base * onset_multiplier

    def test_non_adaptive_is_fixed(self):
        _, info = _run([0.01, 0.01, 0.1, 0.1, 0.0, 0.0, 0.0], adaptive=False)
        assert info["threshold"] == 0.01


class TestScoreFnBackend:
    """The score_fn path (e.g. Silero speech probabilities) uses a fixed
    threshold and the same onset/endpoint state machine."""

    def _run_scored(self, probs, **over):
        # Each "chunk" encodes its speech probability in element 0; score_fn
        # reads it back. Exercises the neural-VAD branch without a model.
        chunks = [np.full(4, p, dtype=np.float32) for p in probs]
        it = iter(chunks)

        def _next():
            return next(it, None)

        params = {**_BASE, **over,
                  "base_threshold": 0.5, "adaptive": True,
                  "score_fn": lambda c: float(c[0])}
        return _vad_collect(_next, **params)

    def test_speech_probs_capture_and_end(self):
        # low, HIGH HIGH HIGH (prob>0.5), then low (prob<0.3 end) x3
        audio, info = self._run_scored([0.1, 0.9, 0.9, 0.9, 0.0, 0.0, 0.0])
        assert audio is not None
        assert info["reason"] == "end_silence"
        assert info["threshold"] == 0.5   # fixed, not adaptively calibrated

    def test_low_prob_noise_does_not_trigger(self):
        # Non-speech noise sits at prob ~0.1 — must never start capture.
        audio, info = self._run_scored([0.1] * 8, onset_timeout_chunks=6)
        assert audio is None
        assert info["reason"] == "onset_timeout"
