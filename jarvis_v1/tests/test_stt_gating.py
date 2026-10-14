"""Step 4 — Whisper hallucination gating (pure logic, no model/audio)."""

from perception.stt import _is_answer, _is_hallucination


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


class TestFollowUpAnswers:
    """In a follow-up window, short answers that collide with the phantom list
    ("okay", "yeah") are kept; other phantoms are still dropped."""

    def test_short_answers_recognised(self):
        for phrase in ["Yes.", "yeah", "Okay!", "ok", "No.", "nope", "sure"]:
            assert _is_answer(phrase), phrase

    def test_phantoms_are_not_answers(self):
        for phrase in ["Thank you.", "thanks for watching", "you", "."]:
            assert not _is_answer(phrase), phrase
