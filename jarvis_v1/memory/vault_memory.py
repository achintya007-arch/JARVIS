"""
memory/vault_memory.py — the V3 memory facade.

Replaces V2's ContextManager. Same shape (initialize / close / build_messages /
recall / add_exchange / ready) so core_v3/adapters.py::VaultAdapter drops in
unchanged. Two tiers, mirroring the V2 design but vault-backed:

  Tier 1 — today's daily note (exact, ordered, human-readable)  → rolling window
  Tier 2 — VaultIndexer over all vault notes (semantic recall)  → memory block

build_messages() assembles, in order:
  1. a [VAULT MEMORY] system block from semantic recall (if any hits)
  2. the recent turns from today's daily note (rolling window)
  3. the current user message

add_exchange() appends the turn to today's daily note and lets the indexer pick
it up on its next refresh (fire-and-forget — never delays TTS).
"""

from __future__ import annotations

import logging
import re
from collections import deque
from typing import Deque, Optional

from memory.vault import Vault
from memory.vault_indexer import VaultIndexer
from memory.web_rag import WebRAG

log = logging.getLogger("jarvis.vault.memory")

# Conversation intents for which world-knowledge (web RAG) is worth consulting.
_FACTUAL_INTENTS = frozenset({"query"})

# Parse an exchange pair back out of a daily note to seed the rolling window.
_EXCHANGE_RE = re.compile(
    r"^> \*\*\[[0-9:]+\] Sir:\*\* (?P<user>.*)\n> \*\*JARVIS:\*\* (?P<assistant>.*)$",
    re.MULTILINE,
)


class VaultMemory:
    def __init__(self, config, ollama_url: str = "http://localhost:11434",
                web_rag_config=None) -> None:
        self._config = config
        self._vault  = Vault(config)
        self._index  = VaultIndexer(
            vault=self._vault,
            index_dir=config.index_dir,
            ollama_url=ollama_url,
        )
        # Web-knowledge RAG (Phase D) — disabled unless a config is passed and
        # the index has been built. Personal context stays on the vault.
        self._web_rag = WebRAG(web_rag_config, ollama_url) if web_rag_config else None
        # Rolling window of recent {role, content} messages (like V2 TranscriptStore).
        window = max(2, int(getattr(config, "rolling_window_turns", 30)))
        self._recent: Deque[dict] = deque(maxlen=window)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        await self._index.initialize()
        self._index.start_background()
        if self._web_rag is not None:
            await self._web_rag.initialize()
        self._seed_rolling_window()
        log.info(
            "VaultMemory ready | vault=%s | recall=%s | web=%s",
            self._vault.jarvis_dir,
            "on" if self._index.ready else "off",
            "on" if (self._web_rag and self._web_rag.ready) else "off",
        )

    async def close(self) -> None:
        await self._index.close()
        if self._web_rag is not None:
            await self._web_rag.close()

    @property
    def ready(self) -> bool:
        # Memory is usable even without embeddings — daily-note context still works.
        return True

    @property
    def recall_ready(self) -> bool:
        """True when semantic (cross-note) recall is available (embeddings up)."""
        return self._index.ready

    # ── Core API (mirrors ContextManager) ─────────────────────────────────────

    async def build_messages(self, user_text: str, intent: Optional[str] = None) -> list[dict]:
        messages: list[dict] = []

        # Tier 2a — semantic recall over the user's vault (personal context)
        memories = await self._index.query(user_text)
        if memories:
            block = (
                "[VAULT MEMORY — relevant notes and past context]\n"
                + "\n".join(f"  • {m}" for m in memories)
                + "\n[END MEMORY]"
            )
            messages.append({"role": "system", "content": block})
            log.debug("Injected %d vault memory fragment(s)", len(memories))

        # Tier 2b — web-knowledge RAG, only for factual questions (intent gate)
        if (
            self._web_rag is not None and self._web_rag.ready
            and (intent is None or intent in _FACTUAL_INTENTS)
        ):
            web = await self._web_rag.query(user_text)
            if web:
                block = (
                    "[WEB KNOWLEDGE — grounding facts from a curated corpus]\n"
                    + "\n".join(f"  • {w}" for w in web)
                    + "\n[END WEB KNOWLEDGE]"
                )
                messages.append({"role": "system", "content": block})
                log.debug("Injected %d web knowledge fragment(s)", len(web))

        # Tier 1 — recent turns from today's daily note
        messages.extend(self._recent)

        # Current query
        messages.append({"role": "user", "content": user_text})
        return messages

    async def recall(self, text: str) -> list[str]:
        return await self._index.query(text)

    async def add_exchange(self, user: str, assistant: str) -> None:
        # In-memory rolling window
        self._recent.append({"role": "user",      "content": user})
        self._recent.append({"role": "assistant", "content": assistant})
        # Persist to today's daily note (the indexer picks it up on next refresh)
        try:
            self._vault.append_exchange(user, assistant)
        except Exception as e:
            log.error("Vault daily-note append failed: %s", e)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _seed_rolling_window(self) -> None:
        """Load today's exchanges so a restart keeps same-day context."""
        text = self._vault.read_today()
        if not text:
            return
        pairs = _EXCHANGE_RE.findall(text)
        # Keep only the last window//2 exchanges
        for user, assistant in pairs[-(self._recent.maxlen // 2):]:
            self._recent.append({"role": "user",      "content": user.strip()})
            self._recent.append({"role": "assistant", "content": assistant.strip()})
        if pairs:
            log.info("Seeded rolling window with %d recent exchange(s)", len(pairs))

    # Convenience accessors for other layers (Phase C tools, Brain prompt context)
    @property
    def vault(self) -> Vault:
        return self._vault
