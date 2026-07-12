"""Fuzzy wake-word matching — absorbs tiny.en mishears without self-triggering."""

from perception.hotword import _JARVIS_RE


class TestFuzzyWake:
    def test_matches_common_mishears(self):
        for w in ["jarvis", "jervis", "jarvus", "jervais", "jarvies",
                  "travis", "charvis", "hey jarvis", "okay jarvis"]:
            assert _JARVIS_RE.search(w), w

    def test_does_not_match_normal_words(self):
        # Words JARVIS itself might say — must NOT trigger a false barge-in.
        for w in ["java", "archive", "service", "harvest", "starving",
                  "carving", "marvelous", "the server", "i will travel"]:
            assert not _JARVIS_RE.search(w), w
