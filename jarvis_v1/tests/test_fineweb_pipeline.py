"""Phase D — FineWeb filtering + chunking (pure, no `datasets` needed)."""

from pathlib import Path

from data.fineweb_pipeline import BlocklistFilter, chunk_text


def _filter():
    # Uses the real blocklists shipped in data/blocklists/.
    return BlocklistFilter(Path(__file__).resolve().parent.parent / "data" / "blocklists")


class TestBlocklistDomains:
    def test_blocks_social(self):
        f = _filter()
        assert f.reject_domain("https://www.facebook.com/some/post") is True
        assert f.reject_domain("http://reddit.com/r/x") is True

    def test_allows_regular(self):
        f = _filter()
        assert f.reject_domain("https://en.wikipedia.org/wiki/Nitrogen") is False

    def test_subdomain_suffix_match(self):
        f = _filter()
        assert f.reject_domain("https://m.tiktok.com/foo") is True

    def test_empty_url(self):
        assert _filter().reject_domain("") is False


class TestBlocklistText:
    def test_phrase_blocked(self):
        assert _filter().reject_text("Please BUY NOW while stocks last") is not None

    def test_email_pii_blocked(self):
        assert _filter().reject_text("contact me at john.doe@example.com today") is not None

    def test_clean_text_ok(self):
        clean = "Nitrogen is a chemical element with the symbol N and atomic number seven."
        assert _filter().reject_text(clean) is None


class TestAcceptsChunk:
    def test_rejects_short(self):
        assert _filter().accepts_chunk("too short", "https://good.org") is False

    def test_rejects_blocked_domain(self):
        long = "Nitrogen is essential to life. " * 20
        assert _filter().accepts_chunk(long, "https://twitter.com/x") is False

    def test_accepts_clean_long(self):
        long = "The boiling point of nitrogen is about minus one hundred ninety six Celsius. " * 5
        assert _filter().accepts_chunk(long, "https://chem.org/nitrogen") is True


class TestChunking:
    def test_short_text_single_chunk(self):
        assert len(chunk_text("one short paragraph")) == 1

    def test_long_text_splits(self):
        para = "This is a paragraph about science. " * 30  # ~1050 chars
        text = "\n\n".join([para] * 4)                       # ~4200 chars
        chunks = chunk_text(text, size=2000)
        assert len(chunks) >= 2
        assert all(len(c) <= 2500 for c in chunks)  # roughly bounded

    def test_empty(self):
        assert chunk_text("") == []
