"""
TTS backend selection + Piper/edge fallback (CI-safe).

Covers the engine-selection and rate-mapping logic added with the local Piper
backend. Synthesis itself (network edge-tts / the Piper model) is not exercised
here — only that the right backend is chosen and that a missing Piper model
degrades to edge-tts instead of crashing.
"""

import pytest

pytest.importorskip("numpy")
pytest.importorskip("sounddevice")

from core.config import TTSConfig
from perception.tts import TTSEngine, _rate_to_length_scale


class TestRateToLengthScale:
    def test_slower_rate_maps_above_one(self):
        assert _rate_to_length_scale("-4%") == 1.04   # 4% slower → longer

    def test_faster_rate_maps_below_one(self):
        assert _rate_to_length_scale("+10%") == 0.9

    def test_zero_rate_is_unity(self):
        assert _rate_to_length_scale("+0%") == 1.0

    def test_garbage_rate_is_unity(self):
        assert _rate_to_length_scale("fast") == 1.0


class TestEngineSelection:
    def test_defaults_to_piper(self):
        eng = TTSEngine(TTSConfig())
        assert eng._engine == "piper"
        assert eng._model_name == "en_US-ryan-high"
        # edge fallback voice is resolved up front from the model name
        assert eng._voice == "en-US-ChristopherNeural"

    def test_explicit_edge_voice_preserved(self):
        cfg = TTSConfig(); cfg.engine = "edge"; cfg.voice = "en-US-JennyNeural"
        eng = TTSEngine(cfg)
        assert eng._engine == "edge"
        assert eng._voice == "en-US-JennyNeural"
        assert eng._piper is None   # edge mode never loads a model


class TestPiperFallback:
    def test_missing_model_loads_to_none(self):
        # A voice with no matching .onnx under voices/ must not raise — it
        # returns None so initialize() can fall back to edge-tts.
        cfg = TTSConfig(); cfg.voice = "en_US-does-not-exist"
        eng = TTSEngine(cfg)
        assert eng._load_piper() is None
