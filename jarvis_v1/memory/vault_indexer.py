"""
memory/vault_indexer.py — semantic index over the Obsidian vault.

Embeds vault notes into a Chroma collection ("vault_index") using Ollama's
nomic-embed-text, then answers semantic queries. Adapted from
cognition/memory_store.py — same embedding + Chroma upsert/query approach; the
only real difference is the document source (note files, chunked) rather than
conversation pairs.

Design:
  - Ollama /api/embed via httpx (no model download; reuses the running Ollama)
  - Chroma PersistentClient on disk; cosine distance
  - refresh() re-embeds only notes whose mtime changed this process lifetime
  - Graceful degradation: if the embed model isn't available, ready=False and
    query() returns [] so conversation continues on daily-note context alone
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from memory.embeddings import OllamaEmbedder
from memory.vault import Vault

log = logging.getLogger("jarvis.vault.index")

_COLLECTION      = "vault_index"
_DISTANCE_MAX    = 0.7      # cosine distance cap — reject loosely-related noise
_EMBED_TIMEOUT   = 8.0
_CHUNK_CHARS     = 1500     # target chunk size for long notes
_REFRESH_SECS    = 30.0


def _chunk(text: str, size: int = _CHUNK_CHARS) -> list[str]:
    """Split a note into ~size-char chunks on paragraph boundaries."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
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


class VaultIndexer:
    def __init__(
        self,
        vault: Vault,
        index_dir: str,
        ollama_url: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
    ) -> None:
        self._vault       = vault
        self._index_dir   = index_dir
        self._embedder    = OllamaEmbedder(ollama_url, embed_model)

        self._chroma = None
        self._collection = None
        self._ready = False
        # path -> mtime of the last time we embedded it (this process)
        self._seen: dict[str, float] = {}
        self._refresh_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        import chromadb  # lazy — keeps the module importable without chromadb installed

        Path(self._index_dir).mkdir(parents=True, exist_ok=True)
        await self._embedder.start()

        self._chroma = await asyncio.to_thread(chromadb.PersistentClient, path=self._index_dir)
        self._collection = await asyncio.to_thread(
            self._chroma.get_or_create_collection,
            name=_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

        if not await self._embedder.probe():
            log.warning(
                "Embed model %r unavailable — vault semantic recall disabled. "
                "Run: ollama pull %s", self._embedder.model, self._embedder.model,
            )
            self._ready = False
            return

        self._ready = True
        await self.refresh()
        log.info("VaultIndexer ready | model=%s", self._embedder.model)

    def start_background(self) -> None:
        """Kick off a periodic refresh loop. Call after initialize()."""
        if self._ready and self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def close(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        await self._embedder.close()

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Indexing ──────────────────────────────────────────────────────────────

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REFRESH_SECS)
                await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Vault refresh error: %s", e)

    async def refresh(self) -> int:
        """Embed notes whose mtime changed since we last saw them. Returns count."""
        if not self._ready:
            return 0
        embedded = 0
        for note in self._vault.iter_notes():
            try:
                mtime = note.stat().st_mtime
            except OSError:
                continue
            key = str(note)
            if self._seen.get(key) == mtime:
                continue

            body = self._vault.note_text(note)
            if not body:
                self._seen[key] = mtime
                continue

            rel = self._rel(note)
            for i, chunk in enumerate(_chunk(body)):
                emb = await self._embedder.embed(chunk)
                if emb is None:
                    continue
                await asyncio.to_thread(
                    self._collection.upsert,
                    ids=[f"{rel}#{i}"],
                    embeddings=[emb],
                    documents=[chunk],
                    metadatas=[{"source": rel, "chunk": i}],
                )
                embedded += 1
            self._seen[key] = mtime

        if embedded:
            log.debug("Vault index refreshed: %d chunk(s) embedded", embedded)
        return embedded

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query(self, text: str, n: int = 3) -> list[str]:
        """Return up to n relevant note snippets, formatted for LLM injection."""
        if not self._ready:
            return []
        count = await asyncio.to_thread(self._collection.count)
        if count == 0:
            return []

        emb = await self._embedder.embed(text)
        if emb is None:
            return []

        res = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[emb],
            n_results=min(n, count),
            include=["documents", "distances", "metadatas"],
        )
        docs   = res.get("documents", [[]])[0]
        dists  = res.get("distances", [[]])[0]
        metas  = res.get("metadatas", [[]])[0]

        out: list[str] = []
        for doc, dist, meta in zip(docs, dists, metas, strict=False):
            if dist > _DISTANCE_MAX:
                continue
            source = (meta or {}).get("source", "note")
            snippet = doc.strip().replace("\n", " ")
            out.append(f"[{source}] {snippet[:400]}")
        return out

    # ── Internal ──────────────────────────────────────────────────────────────

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._vault.root)).replace("\\", "/")
        except ValueError:
            return path.name
