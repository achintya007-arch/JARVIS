"""
action/obsidian_tools.py — vault CRUD exposed as agent tools.

Thin bridge between the AgentExecutor tool surface and the V3 memory layer
(memory/vault.py + memory/vault_memory.py). Keeps AgentExecutor free of vault
internals: it just calls these methods and speaks the returned string.

Only instantiated by the V3 Brain (which has a VaultMemory). In V2, AgentExecutor
holds no ObsidianTools, so vault tool calls report that memory isn't available.
All writes are confined to the vault root by memory/vault.py::Vault._safe.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("jarvis.obsidian")

_EXCHANGE_USER_RE = re.compile(r"^> \*\*\[[0-9:]+\] Sir:\*\* (.+)$", re.MULTILINE)


class ObsidianTools:
    def __init__(self, vault_memory) -> None:
        self._mem   = vault_memory
        self._vault = vault_memory.vault

    # ── Authoring ─────────────────────────────────────────────────────────────

    def create_note(self, title: str, body: str = "", tags: Optional[list] = None) -> str:
        title = (title or "").strip()
        if not title:
            return "I need a title for the note, Sir."
        self._vault.create_note(title, body or title, tags=tags)
        return f"Noted, Sir. I've filed a note titled '{title}'."

    def append_to_daily_note(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "There was nothing to log, Sir."
        # Log as a standalone journal line (distinct from conversation turns).
        self._vault.append(self._vault.daily_path(), f"\n- {text}\n")
        return "Logged to today's note, Sir."

    def link_notes(self, src: str, dst: str) -> str:
        src, dst = (src or "").strip(), (dst or "").strip()
        if not src or not dst:
            return "I need both a source and a target note, Sir."
        try:
            self._vault.append(src if src.endswith(".md") else f"{src}.md",
                              f"\n\nRelated: [[{dst}]]\n")
        except PermissionError:
            return "That note is outside the vault, Sir."
        return f"Linked '{src}' to '{dst}', Sir."

    def complete_task(self, text: str) -> str:
        text = (text or "").strip()
        if self._vault.toggle_task(text, done=True):
            return f"Marked '{text}' as complete, Sir."
        return "I couldn't find a matching task, Sir."

    # ── Retrieval ─────────────────────────────────────────────────────────────

    async def search_vault(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "What should I search your notes for, Sir?"
        hits = await self._mem.recall(query)
        if not hits:
            return f"I found nothing in your notes about {query}, Sir."
        top = hits[0].split("] ", 1)[-1]  # strip the [source] prefix
        return f"From your notes, Sir: {top[:280]}"

    def list_today(self, date: str = "") -> str:
        text = self._vault.read_today()
        if not text.strip():
            return "Nothing recorded yet today, Sir."
        users = _EXCHANGE_USER_RE.findall(text)
        if not users:
            return "Today's note is started, but no conversations are logged yet, Sir."
        recent = "; ".join(u.strip()[:60] for u in users[-3:])
        return f"Today we've spoken {len(users)} times, Sir. Most recently: {recent}."
