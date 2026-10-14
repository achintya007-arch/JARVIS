"""
data/fineweb_pipeline.py — download, filter, and chunk FineWeb-Edu.

Offline tooling. The filtering (BlocklistFilter) and chunking (chunk_text) are
pure and unit-tested; the actual dataset streaming lazily imports `datasets`
(install with `pip install -e ".[data]"`) so the module stays importable — and
testable — without the heavy dependency.

Pipeline:
  stream FineWeb-Edu record → blocklist domain check → chunk → per-chunk
  phrase/regex/PII check → yield (chunk_text, source_url)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("jarvis.fineweb")

_DEFAULT_BLOCKLIST_DIR = Path(__file__).parent / "blocklists"
_CHUNK_CHARS = 2000        # ≈ 500 tokens
_MIN_CHUNK_CHARS = 200     # drop tiny fragments


# ── Blocklist filtering ───────────────────────────────────────────────────────

class BlocklistFilter:
    """Loads domain/phrase/regex blocklists and decides whether text is allowed."""

    def __init__(self, blocklist_dir: Path = _DEFAULT_BLOCKLIST_DIR) -> None:
        d = Path(blocklist_dir)
        self.domains: set[str] = set(self._load_lines(d / "domains.txt"))
        self.phrases: list[str] = [p.lower() for p in self._load_lines(d / "phrases.txt")]
        self.regexes: list[re.Pattern] = []
        for pat in self._load_lines(d / "regexes.txt"):
            try:
                self.regexes.append(re.compile(pat, re.IGNORECASE))
            except re.error as e:
                log.warning("Skipping bad blocklist regex %r: %s", pat, e)

    @staticmethod
    def _load_lines(path: Path) -> list[str]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
        return out

    def reject_domain(self, url: str) -> bool:
        """True if the URL's host matches (as a suffix) a blocked domain."""
        if not url:
            return False
        host = (urlparse(url).hostname or "").lower().lstrip(".")
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def reject_text(self, text: str) -> str | None:
        """Return a reason string if the text should be dropped, else None."""
        low = text.lower()
        for phrase in self.phrases:
            if phrase in low:
                return f"phrase:{phrase}"
        for rx in self.regexes:
            if rx.search(text):
                return f"regex:{rx.pattern[:30]}"
        return None

    def accepts_chunk(self, text: str, url: str) -> bool:
        if len(text) < _MIN_CHUNK_CHARS:
            return False
        if self.reject_domain(url):
            return False
        return self.reject_text(text) is None


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str, size: int = _CHUNK_CHARS) -> list[str]:
    """Split text into ~size-char chunks on paragraph boundaries."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 > size and buf:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks or ([text.strip()] if text.strip() else [])


# ── Streaming (lazy: requires the [data] extra) ───────────────────────────────

def iter_records(target_gb: float = 1.0, subset: str = "sample-10BT") -> Iterator[dict]:
    """
    Stream FineWeb-Edu records until ~target_gb of raw text has been seen.
    Yields {"text": str, "url": str}. Lazy-imports `datasets`.
    """
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise RuntimeError(
            "The 'datasets' package is required to download FineWeb. "
            "Install it with: pip install -e \".[data]\""
        ) from e

    log.info("Streaming FineWeb-Edu (%s) up to ~%.1f GB...", subset, target_gb)
    ds = load_dataset("HuggingFaceFW/fineweb-edu", name=subset,
                      split="train", streaming=True)
    budget = int(target_gb * 1e9)
    seen = 0
    for rec in ds:
        text = rec.get("text") or ""
        seen += len(text.encode("utf-8", errors="ignore"))
        yield {"text": text, "url": rec.get("url", "")}
        if seen >= budget:
            log.info("Reached target (%.2f GB of text).", seen / 1e9)
            break


def iter_chunks(
    target_gb: float = 1.0,
    blocklist_dir: Path = _DEFAULT_BLOCKLIST_DIR,
    subset: str = "sample-10BT",
) -> Iterator[tuple[str, str]]:
    """Yield (chunk_text, source_url) for accepted, filtered chunks."""
    flt = BlocklistFilter(blocklist_dir)
    kept = dropped = 0
    for rec in iter_records(target_gb, subset=subset):
        url = rec["url"]
        if flt.reject_domain(url):
            dropped += 1
            continue
        for chunk in chunk_text(rec["text"]):
            if flt.accepts_chunk(chunk, url):
                kept += 1
                yield chunk, url
            else:
                dropped += 1
    log.info("Filtering complete: %d chunks kept, %d dropped.", kept, dropped)
