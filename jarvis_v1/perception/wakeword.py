"""
WakeWordDetector — dedicated wake-word spotting via openWakeWord.

Replaces the old tiny.en-Whisper-window approach: openWakeWord runs a small
purpose-built model (~3 ms per 80 ms frame on CPU, onnxruntime backend), which
is both far cheaper and far more reliable than transcribing rolling windows.

The default model is the pretrained "hey_jarvis". Other names (e.g. "Echo")
need a custom-trained openWakeWord model — point WakeConfig.model at its .onnx.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("jarvis.wake")


class WakeWordDetector:
    def __init__(self, config=None) -> None:
        self.enabled   = getattr(config, "enabled", True) if config else True
        self.model_name = getattr(config, "model", "hey_jarvis") if config else "hey_jarvis"
        self.threshold = getattr(config, "threshold", 0.5) if config else 0.5
        self._framework = getattr(config, "inference_framework", "onnx") if config else "onnx"
        self._model = None
        if self.enabled:
            self._model = self._load()

    def _load(self):
        try:
            import openwakeword
            from openwakeword.model import Model
            openwakeword.utils.download_models()   # no-op once cached
            model = Model(wakeword_models=[self.model_name], inference_framework=self._framework)
            log.info("Wake word ready: %r (openWakeWord/%s)", self.model_name, self._framework)
            return model
        except Exception as e:
            log.error("openWakeWord unavailable (%s) — wake detection disabled", e)
            return None

    @property
    def available(self) -> bool:
        return self._model is not None

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset()

    def score(self, frame: np.ndarray) -> float:
        """Speech→wake score for one ~80 ms frame (float32). openWakeWord wants
        int16 PCM; we convert. Returns the max score across loaded models."""
        if self._model is None:
            return 0.0
        pcm = np.clip(frame, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype(np.int16)
        preds = self._model.predict(pcm16)
        return max((float(v) for v in preds.values()), default=0.0)

    def triggered(self, frame: np.ndarray) -> bool:
        return self.score(frame) >= self.threshold
