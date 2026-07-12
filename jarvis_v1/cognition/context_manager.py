"""
ContextManager — persistent conversation memory for Jarvis.

Sprint B additions:
  - MemoryStore (ChromaDB + Ollama embeddings) wired into add_exchange() and build_messages()
  - Every exchange embedded and stored in vector DB after SQLite persist
  - build_messages() recalls semantically relevant past context and injects it
    as a labelled memory block before the rolling context window
  - Graceful degradation: if embed model isn't pulled, memory is silently disabled
    and conversation continues normally using SQLite history only

Sources integrated from claw-code:
  - TranscriptStore  → adapted for role/content dicts + compact/replay/flush
  - StoredSession    → adapted as SQLite rows
  - QueryEngineConfig.compact_after_turns → drives _COMPACT_AFTER
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiosqlite

from cognition.memory_store import MemoryStore

log = logging.getLogger("jarvis.context")

# ── Tuning ────────────────────────────────────────────────────────────────────
_COMPACT_AFTER  = 20
_KEEP_LAST      = 10
_CONTEXT_WINDOW = 12
_SCHEMA_VERSION = 1


# ── TranscriptStore (from claw-code/src/transcript.py) ───────────────────────

@dataclass
class TranscriptStore:
    entries: list[dict] = field(default_factory=list)
    flushed: bool = False

    def append(self, message: dict) -> None:
        self.entries.append(message)
        self.flushed = False

    def compact(self, keep_last: int = _KEEP_LAST) -> None:
        if len(self.entries) > keep_last:
            dropped = len(self.entries) - keep_last
            self.entries[:] = self.entries[-keep_last:]
            log.debug("Transcript compacted: dropped %d, kept %d", dropped, keep_last)

    def replay(self) -> tuple[dict, ...]:
        return tuple(self.entries)

    def flush(self) -> None:
        self.flushed = True

    def __len__(self) -> int:
        return len(self.entries)


# ── StoredSession (from claw-code/src/session_store.py) ──────────────────────

@dataclass(frozen=True)
class StoredSession:
    session_id: str
    created_at: float
    last_active: float
    message_count: int


# ── ContextManager ────────────────────────────────────────────────────────────

class ContextManager:
    """
    Two-tier memory:
      Tier 1 — SQLite (exact, ordered, full history)
      Tier 2 — ChromaDB via MemoryStore (semantic, cross-session, fuzzy recall)

    build_messages() flow:
      1. Semantic recall from ChromaDB for the user's query
      2. Inject recalled memories as a [JARVIS MEMORY] block if any found
      3. Append rolling context window from TranscriptStore
      4. Append current user message
    """

    def __init__(
        self,
        db_path: str = "data/conversations.db",
        vec_path: Optional[str] = "data/chroma",
        ollama_url: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
        compact_after: int = _COMPACT_AFTER,
        keep_last: int = _KEEP_LAST,
        context_window: int = _CONTEXT_WINDOW,
    ):
        self._db_path        = db_path
        self._compact_after  = compact_after
        self._keep_last      = keep_last
        self._context_window = context_window

        self._transcript = TranscriptStore()
        self._session_id: str = uuid4().hex
        self._db: Optional[aiosqlite.Connection] = None

        # Sprint B: semantic memory
        self._memory: Optional[MemoryStore] = (
            MemoryStore(
                vec_path=vec_path,
                ollama_url=ollama_url,
                embed_model=embed_model,
            )
            if vec_path else None
        )

    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "DB not initialized — call initialize() first"
        return self._db

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._create_schema()

        # Run session restore and memory init in parallel
        init_tasks: list = [self._restore_or_create_session()]
        if self._memory:
            init_tasks.append(self._memory.initialize())

        results = await asyncio.gather(*init_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.warning("Init error (non-fatal): %s", r)

    async def close(self) -> None:
        if self._memory:
            await self._memory.close()
        if self._db:
            self._transcript.flush()
            await self._db.close()
            self._db = None

    # ── Core API ──────────────────────────────────────────────────────────────

    async def build_messages(self, user_text: str) -> list[dict]:
        """
        Build the message list for the LLM.

        Sprint B: semantic recall prepended as a [JARVIS MEMORY] block
        when relevant past context exists. Jarvis can now recall things
        from days or weeks ago — genuinely.
        """
        messages: list[dict] = []

        # Tier 2: semantic recall
        if self._memory and self._memory.ready:
            memories = await self._memory.recall(user_text)
            if memories:
                memory_block = (
                    "[JARVIS MEMORY — relevant context from previous conversations]\n"
                    + "\n".join(f"  • {m}" for m in memories)
                    + "\n[END MEMORY]"
                )
                # Inject as a system message so the LLM treats it as background
                # context rather than part of the conversation history.
                # A fake user/assistant pair confused the model about who said what.
                messages.append({"role": "system", "content": memory_block})
                log.debug("Injected %d memory fragments", len(memories))

        # Tier 1: rolling in-memory window
        messages.extend(self._transcript.entries[-self._context_window:])

        # Current query
        messages.append({"role": "user", "content": user_text})
        return messages

    async def add_exchange(self, user: str, assistant: str) -> None:
        """
        Persist one exchange to both tiers.
        ChromaDB embedding is fire-and-forget so TTS isn't delayed.
        """
        now = time.time()
        exchange_id = f"{self._session_id}_{int(now * 1000)}"

        # Tier 1: in-memory + SQLite
        self._transcript.append({"role": "user",      "content": user})
        self._transcript.append({"role": "assistant",  "content": assistant})

        await self._persist_message("user",      user,      now)
        await self._persist_message("assistant", assistant, now + 0.001)
        await self._touch_session(now)

        # Tier 2: ChromaDB — non-blocking
        if self._memory and self._memory.ready:
            asyncio.create_task(
                self._memory.store(exchange_id, user, assistant, now)
            )

        # In-memory compaction
        if len(self._transcript) >= self._compact_after:
            self._transcript.compact(self._keep_last)
            self._transcript.flush()
    async def memory_store_recall(self, query: str, n: int = 3) -> list[str]:
        """
        Public shortcut for DecisionEngine to query semantic memory
        independently of build_messages().
 
        Returns a list of memory strings, or empty list if memory
        is unavailable or the query returns nothing relevant.
        """
        if not self._memory or not self._memory.ready:
            return []
        try:
            return await self._memory.recall(query, n=n)
        except Exception:
            return []

    # ── Session helpers ───────────────────────────────────────────────────────

    async def _restore_or_create_session(self) -> None:
        restored = await self._restore_last_session()
        if restored:
            log.info("Restored session %s (%d messages)", self._session_id, len(self._transcript))
        else:
            await self._create_session()
            log.info("New session: %s", self._session_id)

    async def _create_schema(self) -> None:
        await self._conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                created_at   REAL NOT NULL,
                last_active  REAL NOT NULL,
                schema_ver   INTEGER NOT NULL DEFAULT {_SCHEMA_VERSION}
            );
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                ts           REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, ts);
        """)
        await self._conn.commit()

    async def _create_session(self) -> None:
        now = time.time()
        await self._conn.execute(
            "INSERT INTO sessions(session_id, created_at, last_active) VALUES (?,?,?)",
            (self._session_id, now, now),
        )
        await self._conn.commit()

    async def _restore_last_session(self) -> bool:
        async with self._conn.execute(
            "SELECT session_id FROM sessions ORDER BY last_active DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return False

        self._session_id = row[0]

        async with self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (self._session_id, self._keep_last),
        ) as cursor:
            rows = await cursor.fetchall()

        for role, content in list(rows)[::-1]:
            self._transcript.append({"role": role, "content": content})

        self._transcript.flush()
        return True

    async def _persist_message(self, role: str, content: str, ts: float) -> None:
        if not self._db:
            return
        await self._conn.execute(
            "INSERT INTO messages(session_id, role, content, ts) VALUES (?,?,?,?)",
            (self._session_id, role, content, ts),
        )
        await self._conn.commit()

    async def _touch_session(self, ts: float) -> None:
        if not self._db:
            return
        await self._conn.execute(
            "UPDATE sessions SET last_active=? WHERE session_id=?",
            (ts, self._session_id),
        )
        await self._conn.commit()

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def memory_ready(self) -> bool:
        return bool(self._memory and self._memory.ready)

    async def message_count(self) -> int:
        if not self._db:
            return 0
        async with self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            (self._session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def full_history(self) -> list[dict]:
        if not self._db:
            return []
        async with self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY ts",
            (self._session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"role": r, "content": c} for r, c in rows]
    
    async def recall_for_thinking(self, query: str, n: int = 3) -> list[str]:
        if self._memory and self._memory.ready:
            try:
                return await self._memory.recall(query, n=n)
            except Exception:
                pass
        return []