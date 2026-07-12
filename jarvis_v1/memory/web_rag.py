"""
memory/web_rag.py — read-only retrieval over the FineWeb web-knowledge index.

Queries the Chroma "web_index" collection built offline by
data/build_web_index.py. Read-only: it never writes. Disabled unless
WebRagConfig.enabled is True AND the index exists AND embeddings are available,
so it is a no-op until the user has actually built the corpus.

VaultMemory calls this only for factual/world-knowledge queries (intent gate),
keeping personal questions on the vault and web questions on the corpus.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from memory.embeddings import OllamaEmbedder

log = logging.getLogger("jarvis.web_rag")

_COLLECTION = "web_index"


class WebRAG:
    def __init__(self, config, ollama_url: str = "http://localhost:11434") -> None:
        self._cfg      = config
        self._embedder = OllamaEmbedder(ollama_url, getattr(config, "embed_model", "nomic-embed-text"))
        self._collection = None
        self._ready = False

    async def initialize(self) -> None:
        if not getattr(self._cfg, "enabled", False):
            log.info("Web RAG disabled in config — skipping.")
            return
        index_dir = Path(self._cfg.index_dir)
        if not index_dir.exists():
            log.warning("Web RAG index not found at %s — run data.build_web_index. Disabled.", index_dir)
            return

        try:
            import chromadb
            chroma = await asyncio.to_thread(chromadb.PersistentClient, path=str(index_dir))
            self._collection = await asyncio.to_thread(chroma.get_collection, _COLLECTION)
        except Exception as e:
            log.warning("Web RAG index could not be opened (%s). Disabled.", e)
            return

        await self._embedder.start()
        if not await self._embedder.probe():
            log.warning("Embed model unavailable — Web RAG disabled.")
            await self._embedder.close()
            return

        self._ready = True
        count = await asyncio.to_thread(self._collection.count)
        log.info("Web RAG ready | %d chunks indexed", count)

    async def close(self) -> None:
        await self._embedder.close()

    @property
    def ready(self) -> bool:
        return self._ready

    async def query(self, text: str, n: Optional[int] = None) -> list[str]:
        """Return up to n web-knowledge snippets above the similarity floor."""
        if not self._ready:
            return []
        n = n or getattr(self._cfg, "top_k", 2)
        emb = await self._embedder.embed(text)
        if emb is None:
            return []

        count = await asyncio.to_thread(self._collection.count)
        if count == 0:
            return []

        res = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[emb],
            n_results=min(n, count),
            include=["documents", "distances", "metadatas"],
        )
        docs  = res.get("documents", [[]])[0]
        dists = res.get("distances", [[]])[0]
        metas = res.get("metadatas", [[]])[0]

        min_score = getattr(self._cfg, "min_score", 0.55)
        out: list[str] = []
        for doc, dist, meta in zip(docs, dists, metas):
            # Chroma cosine distance ≈ 1 - cosine_similarity for unit vectors.
            if (1.0 - dist) < min_score:
                continue
            source = (meta or {}).get("source", "web")
            snippet = doc.strip().replace("\n", " ")
            out.append(f"[{source}] {snippet[:400]}")
        return out
