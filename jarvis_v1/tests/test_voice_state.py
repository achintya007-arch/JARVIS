"""Voice rebuild — VoiceStateManager transitions and WakeWordDetector logic.
Wake word detection here uses a stub model (the real openWakeWord model is
exercised by tools/mic_smoke.py), plus the unavailable/fallback path."""

from types import SimpleNamespace

import numpy as np

from core_v3.voice.state import VoiceState, VoiceStateManager
from perception.wakeword import WakeWordDetector

# enabled=False so constructing the detector does NOT download/load a real model
_DISABLED = SimpleNamespace(enabled=False, model="hey_jarvis", threshold=0.5,
                            inference_framework="onnx")


class TestStateManager:
    def test_starts_idle(self):
        assert VoiceStateManager().state is VoiceState.IDLE

    def test_transition_and_callback(self):
        seen = []
        m = VoiceStateManager(on_change=lambda o, n: seen.append((o, n)))
        m.transition(VoiceState.LISTENING)
        assert m.state is VoiceState.LISTENING
        assert seen == [(VoiceState.IDLE, VoiceState.LISTENING)]

    def test_full_cycle_back_to_idle(self):
        m = VoiceStateManager()
        for s in (VoiceState.LISTENING, VoiceState.PROCESSING, VoiceState.SPEAKING, VoiceState.IDLE):
            m.transition(s)
        assert m.state is VoiceState.IDLE

    def test_is_helper(self):
        m = VoiceStateManager()
        assert m.is_(VoiceState.IDLE)
        assert not m.is_(VoiceState.SPEAKING)


class _StubModel:
    def __init__(self, score):
        self._score = score
    def predict(self, pcm16):
        return {"hey_jarvis": self._score}
    def reset(self):
        pass


class TestWakeWord:
    def test_triggered_above_threshold(self):
        det = WakeWordDetector(_DISABLED)
        det._model = _StubModel(0.9)
        det.threshold = 0.5
        assert det.triggered(np.zeros(1280, dtype=np.float32))

    def test_not_triggered_below_threshold(self):
        det = WakeWordDetector(_DISABLED)
        det._model = _StubModel(0.2)
        det.threshold = 0.5
        assert not det.triggered(np.zeros(1280, dtype=np.float32))

    def test_unavailable_is_safe(self):
        # Fallback (scenario 13): no model loaded → never triggers, never raises.
        det = WakeWordDetector(_DISABLED)
        det._model = None
        assert not det.available
        assert det.score(np.zeros(1280, dtype=np.float32)) == 0.0
        assert not det.triggered(np.zeros(1280, dtype=np.float32))
