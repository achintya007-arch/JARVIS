"""Step 4 — Whisper hallucination gating (pure logic, no model/audio)."""

from perception.stt import _is_hallucination


class TestHallucinationBlocklist:
    def test_common_phantoms_rejected(self):
        for phrase in ["you", "You.", "Thank you.", "thanks for watching",
                       "Bye.", "  okay  ", ".", "Subtitles by the Amara.org community"]:
            assert _is_hallucination(phrase), phrase

    def test_real_commands_kept(self):
        for phrase in ["what time is it", "open chrome", "remind me to call mom",
                       "set a timer for ten minutes", "thank you for setting that timer"]:
            assert not _is_hallucination(phrase), phrase

    def test_punctuation_and_case_insensitive(self):
        assert _is_hallucination("THANK YOU!!!")
        assert _is_hallucination("— you —")
