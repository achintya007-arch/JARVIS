"""
Voice rebuild — endpoint detection (EndpointSM), pure logic, no audio/model.

Covers required scenarios: speech onset, speech endpoint, short utterance, long
utterance, brief mid-utterance pause (not truncated), and background noise
(never triggers). Scores simulate Silero speech probabilities (>threshold =
speech); the frame contents are irrelevant to the decision, so a dummy array
is used.
"""

import numpy as np

from perception.endpointer import EndpointSM

_F = np.zeros(4, dtype=np.float32)   # dummy frame


def _sm(**over):
    params = dict(
        threshold=0.5, end_silence_frames=3, max_frames=100,
        pre_frames=2, onset_timeout_frames=20, min_speech_frames=2,
    )
    params.update(over)
    return EndpointSM(**params)


def _run(scores, **over):
    sm = _sm(**over)
    reason = None
    for s in scores:
        reason = sm.feed(s, _F)
        if reason:
            break
    return reason, sm.audio()


class TestOnset:
    def test_onset_requires_sustained_speech(self):
        # a single high frame must NOT start capture (debounce = 2)
        sm = _sm()
        assert sm.feed(0.9, _F) is None
        assert sm.feed(0.1, _F) is None          # reset
        assert not sm._started
        sm.feed(0.9, _F); sm.feed(0.9, _F)       # two in a row → onset
        assert sm._started


class TestEndpoint:
    def test_ends_on_sustained_silence(self):
        reason, audio = _run([0.9, 0.9, 0.9, 0.0, 0.0, 0.0])
        assert reason == "endpoint"
        assert audio is not None


class TestShortUtterance:
    def test_short_command_captured(self):
        reason, audio = _run([0.9, 0.9, 0.0, 0.0, 0.0])
        assert reason == "endpoint"
        assert audio is not None


class TestLongUtterance:
    def test_long_command_not_cut_by_max(self):
        reason, audio = _run([0.9] * 40 + [0.0, 0.0, 0.0], max_frames=200)
        assert reason == "endpoint"
        assert audio is not None and len(audio) >= 40 * len(_F)


class TestBriefPause:
    def test_pause_inside_utterance_does_not_truncate(self):
        # "open Chrome and then— [pause] actually, VS Code instead"
        scores = [0.9, 0.9, 0.9, 0.1, 0.1, 0.9, 0.9, 0.0, 0.0, 0.0]
        reason, audio = _run(scores)
        assert reason == "endpoint"
        # captured past the mid pause (≥7 speech+pause frames)
        assert len(audio) >= 7 * len(_F)


class TestBackgroundNoise:
    def test_subthreshold_noise_never_triggers(self):
        reason, audio = _run([0.3, 0.2, 0.35, 0.1] * 6, onset_timeout_frames=20)
        assert reason == "onset_timeout"
        assert audio is None

    def test_single_noise_spike_ignored(self):
        reason, audio = _run([0.0, 0.9, 0.0, 0.0, 0.0, 0.0], onset_timeout_frames=6)
        assert reason == "onset_timeout"
        assert audio is None
