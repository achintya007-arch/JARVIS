"""
Safety policy for clicking and typing into other apps (BBP amendments 19–20).

is_risky(*texts)  True when an element's name or automation id looks
                  destructive or outbound (send, delete, pay, sign out, …):
                  pressing it needs your approval. The words live in
                  protocol/policy/risky-words.json, shared with the Body, which
                  checks the element it actually finds a second time.
unsafe_text(text) Why text may not be typed into an app, or None. Control
                  characters (except tab and newline), bidirectional overrides
                  and zero-width characters are refused, so what the
                  confirmation dialog shows is exactly what gets typed.

Matching can't be dodged by case, full-width letters, zero-width characters or
common Cyrillic/Greek look-alikes; a word that mixes Latin with other letters
counts as risky. If the word list can't be loaded, everything is risky.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("jarvis.ui_policy")

POLICY_FILE = Path(__file__).resolve().parents[2] / "protocol" / "policy" / "risky-words.json"
MAX_TEXT = 2000
_ENDINGS = ("ing", "es", "ed", "s", "d")

# Look-alikes that NFKC leaves alone: Cyrillic and Greek letters drawn like Latin.
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d",
    "ԛ": "q", "ԝ": "w", "ɡ": "g", "ɩ": "i", "ʟ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ј": "J",
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}


@lru_cache(maxsize=1)
def _policy() -> tuple[frozenset[str], tuple[tuple[str, ...], ...]] | None:
    try:
        data = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        words = frozenset(w.lower() for w in data["words"])
        phrases = tuple(tuple(p.lower().split()) for p in data["phrases"])
        return words, phrases
    except Exception as e:                      # fail closed: every invoke is risky
        log.warning("risky-word list unavailable (%s): every click will need approval", e)
        return None


def words_of(text: str) -> list[str]:
    """The words of a label, normalised as the policy compares them."""
    text = unicodedata.normalize("NFKC", str(text))
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")      # zero-width etc.
    text = text.translate(_CONFUSABLES)
    # Accents off (é → e), so accented Latin isn't mistaken for a mixed script.
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) != "Mn")
    text = _CAMEL.sub(" ", text)
    return [w for w in re.split(r"[^\w]+|_", text.casefold()) if w]


def _mixed_script(word: str) -> bool:
    latin = any("a" <= c <= "z" for c in word)
    other = any(c.isalpha() and not ("a" <= c <= "z") for c in word)
    return latin and other


def _hits(word: str, words: frozenset[str]) -> bool:
    if word in words:
        return True
    return any(word.endswith(e) and word[: -len(e)] in words for e in _ENDINGS)


def is_risky(*texts: str | None) -> bool:
    policy = _policy()
    if policy is None:
        return True
    words, phrases = policy
    for text in texts:
        if not text:
            continue
        tokens = words_of(text)
        if any(_hits(t, words) or _mixed_script(t) for t in tokens):
            return True
        for phrase in phrases:
            n = len(phrase)
            joined = "".join(phrase)
            if any(t == joined or _hits(t, frozenset({joined})) for t in tokens):
                return True
            if any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1)):
                return True
    return False


def unsafe_text(text: str) -> str | None:
    if len(text) > MAX_TEXT:
        return f"it's {len(text)} characters; the limit is {MAX_TEXT}"
    for c in text:
        cp = ord(c)
        if c in "\t\n":
            continue
        if cp in _BIDI:
            return "it contains a right-to-left override character"
        cat = unicodedata.category(c)
        if cat == "Cc":
            return "it contains a control character"
        if cat == "Cf":
            return "it contains an invisible formatting character"
    return None
