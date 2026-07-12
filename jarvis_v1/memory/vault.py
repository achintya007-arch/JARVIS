"""
memory/vault.py — Obsidian vault filesystem primitives.

Pure I/O over a markdown vault. No embeddings, no LLM, no async — just the
mechanics of reading/writing notes, frontmatter, daily logs, and task lines.
Higher layers (vault_indexer, vault_memory) build on top of this.

Layout under <vault_root>/<jarvis_subdir> (defaults: vault/JARVIS):
    daily/YYYY-MM-DD.md   one note per day: conversation log + goal events
    memory/<slug>.md      long-form notes JARVIS authors (one idea per file)
    goals.md              live task list (Obsidian checkboxes)

Sir's own notes live anywhere else in the vault and are read-only to indexing.
"""

from __future__ import annotations

import logging
import re
from datetime import date as _date, datetime
from pathlib import Path
from typing import Iterator, Optional

import yaml

log = logging.getLogger("jarvis.vault")

# Frontmatter delimiter block at the top of a note: ---\n...\n---
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# A markdown task line:  - [ ] text   or   - [x] text
_TASK_RE = re.compile(r"^(\s*[-*]\s*)\[([ xX])\]\s*(.+?)\s*$")

# Characters not allowed in note filenames
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Turn a title into a safe filename slug."""
    s = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-")) or "note"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Split a note into (frontmatter_dict, body). If there is no frontmatter,
    returns ({}, text). Malformed YAML degrades to ({}, original_text).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        return {}, text
    return meta, text[m.end():]


def serialize_frontmatter(meta: dict, body: str) -> str:
    """Inverse of parse_frontmatter. Omits the block entirely if meta is empty."""
    if not meta:
        return body
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.lstrip()}"


class Vault:
    """Filesystem access to an Obsidian vault. All paths resolve under vault_root."""

    def __init__(self, config) -> None:
        self.root       = Path(config.vault_root).resolve()
        self.jarvis_dir = self.root / config.jarvis_subdir
        self.daily_dir  = self.jarvis_dir / config.daily_dir
        self.memory_dir = self.jarvis_dir / config.memory_dir
        self.goals_path = self.jarvis_dir / config.goals_file

        for d in (self.jarvis_dir, self.daily_dir, self.memory_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── Path safety ───────────────────────────────────────────────────────────

    def _safe(self, path) -> Path:
        """Resolve a path and ensure it is inside the vault. Raises otherwise."""
        p = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise PermissionError(f"Path escapes the vault: {path}")
        return p

    # ── Read / write ──────────────────────────────────────────────────────────

    def read(self, path) -> str:
        p = self._safe(path)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8", errors="replace")

    def write(self, path, content: str) -> Path:
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def append(self, path, text: str) -> Path:
        p = self._safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text)
        return p

    # ── Daily notes ───────────────────────────────────────────────────────────

    def daily_path(self, when: Optional[_date] = None) -> Path:
        when = when or _date.today()
        return self.daily_dir / f"{when.isoformat()}.md"

    def ensure_daily(self, when: Optional[_date] = None) -> Path:
        """Create today's daily note with frontmatter if it doesn't exist yet."""
        p = self.daily_path(when)
        if not p.exists():
            day = (when or _date.today()).isoformat()
            header = serialize_frontmatter(
                {"date": day, "type": "daily", "tags": ["jarvis"]},
                f"# {day}\n\n",
            )
            p.write_text(header, encoding="utf-8")
        return p

    def append_exchange(self, user: str, assistant: str,
                        when: Optional[datetime] = None) -> Path:
        """Append one user↔JARVIS turn to today's daily note as a blockquote pair."""
        when = when or datetime.now()
        self.ensure_daily(when.date())
        stamp = when.strftime("%H:%M")
        block = (
            f"\n> **[{stamp}] Sir:** {user.strip()}\n"
            f"> **JARVIS:** {assistant.strip()}\n"
        )
        return self.append(self.daily_path(when.date()), block)

    def read_today(self, when: Optional[_date] = None) -> str:
        return self.read(self.daily_path(when))

    # ── Note authoring ────────────────────────────────────────────────────────

    def create_note(self, title: str, body: str, tags: Optional[list[str]] = None) -> Path:
        """Author a long-form memory note under memory/<slug>.md."""
        slug = slugify(title)
        meta = {
            "title": title,
            "created": datetime.now().isoformat(timespec="seconds"),
            "tags": ["jarvis"] + list(tags or []),
        }
        content = serialize_frontmatter(meta, f"# {title}\n\n{body.strip()}\n")
        return self.write(self.memory_dir / f"{slug}.md", content)

    # ── Tasks (goals.md) — used by Phase C GoalManager ────────────────────────

    def toggle_task(self, text_substring: str, done: bool = True) -> bool:
        """
        Mark the first matching '- [ ]' task in goals.md as done/undone.
        Returns True if a line was changed.
        """
        if not self.goals_path.exists():
            return False
        lines = self.goals_path.read_text(encoding="utf-8").splitlines()
        needle = text_substring.lower()
        changed = False
        for i, line in enumerate(lines):
            m = _TASK_RE.match(line)
            if m and needle in m.group(3).lower():
                box = "x" if done else " "
                lines[i] = f"{m.group(1)}[{box}] {m.group(3)}"
                changed = True
                break
        if changed:
            self.goals_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return changed

    # ── Indexing support ──────────────────────────────────────────────────────

    def iter_notes(self) -> Iterator[Path]:
        """Yield every markdown file in the vault (for the indexer)."""
        for p in self.root.rglob("*.md"):
            if p.is_file():
                yield p

    def note_text(self, path: Path) -> str:
        """Return a note's body without frontmatter, for embedding."""
        _, body = parse_frontmatter(self.read(path))
        return body.strip()
