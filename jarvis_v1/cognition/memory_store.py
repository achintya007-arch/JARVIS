"""
MemoryStore — long-term semantic memory for Jarvis.

Architecture:
  - Ollama /api/embed generates embeddings (no model download, reuses existing Ollama)
  - ChromaDB stores embedding vectors + exchange text, persisted to disk
  - On every add_exchange(): embed and store the full user↔assistant pair
  - On every build_messages(): semantic search recalls relevant past context
  - Recalled memories injected into LLM context as a labelled memory block

Design choices:
  - Embeddings are provided manually to ChromaDB — no DefaultEmbeddingFunction,
    no ONNX download, no internet required. Fully local.
  - Async everywhere: Ollama embed call is httpx, ChromaDB calls run in to_thread()
  - Graceful degradation: if Ollama embed fails, the exchange is skipped silently
    and recall returns nothing — normal conversation continues unaffected.
  - Cosine distance threshold filters out weak matches so Jarvis doesn't inject
    irrelevant noise into context.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

import chromadb
import httpx

log = logging.getLogger("jarvis.memory")

# ── Tuning ────────────────────────────────────────────────────────────────────
_RECALL_N            = 3      # how many past exchanges to recall per query
_DISTANCE_THRESHOLD  = 0.7    # cosine distance cap (range 0–2); 0.7 rejects
                               # loosely-related noise, keeps genuinely close matches
_COLLECTION_NAME     = "jarvis_memory"
_EMBED_TIMEOUT       = 8.0    # seconds before giving up on Ollama embed


class MemoryStore:
    """
    Semantic long-term memory using ChromaDB + Ollama embeddings.

    Usage (called by ContextManager):
        store = MemoryStore(vec_path="data/chroma", ollama_url="http://localhost:11434", embed_model="qwen2.5:7b-instruct-q4_K_M")
        await store.initialize()
        await store.store("id", "user text", "assistant text", ts)
        memories = await store.recall("what did we talk about?")
    """

    def __init__(
        self,
        vec_path: str,
        ollama_url: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
    ):
        self._vec_path    = vec_path
        self._ollama_url  = ollama_url.rstrip("/")
        self._embed_model = embed_model
        self._client: httpx.AsyncClient | None = None
        self._chroma: chromadb.PersistentClient | None = None
        self._collection  = None
        self._ready       = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        Path(self._vec_path).mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(base_url=self._ollama_url, timeout=_EMBED_TIMEOUT)

        # ChromaDB is sync — open in thread
        self._chroma = await asyncio.to_thread(
            chromadb.PersistentClient, path=self._vec_path
        )
        self._collection = await asyncio.to_thread(
            self._chroma.get_or_create_collection,
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        # Probe Ollama embed — if the model isn't pulled, warn and degrade
        probe = await self._embed("Jarvis online.")
        if probe is None:
            log.warning(
                "Embed model '%s' not available. "
                "Run: ollama pull %s\n"
                "Semantic memory will be disabled until the model is available.",
                self._embed_model, self._embed_model,
            )
            self._ready = False
        else:
            count = await asyncio.to_thread(self._collection.count)
            log.info(
                "MemoryStore ready | model=%s | stored exchanges=%d",
                self._embed_model, count,
            )
            self._ready = True

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ── Core API ──────────────────────────────────────────────────────────────

    async def store(
        self,
        exchange_id: str,
        user: str,
        assistant: str,
        ts: float,
    ) -> None:
        """
        Embed and store one user↔assistant exchange.
        The document stored is the full pair — gives richer semantic signal
        than embedding user-only or assistant-only.
        """
        if not self._ready:
            return

        document = f"User: {user}\nJarvis: {assistant}"
        embedding = await self._embed(document)
        if embedding is None:
            return

        doc_id = exchange_id or _stable_id(user, ts)

        await asyncio.to_thread(
            self._collection.upsert,
            ids=[doc_id],
            embeddings=[embedding],
            documents=[document],
            metadatas=[{"ts": ts, "user": user[:200]}],
        )
        log.debug("Memory stored: %s chars → id=%s", len(document), doc_id)

    async def recall(self, query: str, n: int = _RECALL_N) -> list[str]:
        """
        Semantic search over stored exchanges.
        Returns a list of human-readable strings suitable for LLM injection.
        Empty list if nothing relevant is found or memory is not ready.
        """
        if not self._ready:
            return []

        count = await asyncio.to_thread(self._collection.count)
        if count == 0:
            return []

        query_embedding = await self._embed(query)
        if query_embedding is None:
            return []

        actual_n = min(n, count)
        results = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[query_embedding],
            n_results=actual_n,
            include=["documents", "distances", "metadatas"],
        )

        memories: list[str] = []
        docs      = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metas     = results.get("metadatas", [[]])[0]

        for doc, dist, meta in zip(docs, distances, metas, strict=False):
            if dist > _DISTANCE_THRESHOLD:
                log.debug("Memory skipped (dist=%.3f > threshold)", dist)
                continue
            # Format as a concise spoken-memory snippet
            ts_label = _ts_label(meta.get("ts", 0))
            memories.append(f"[{ts_label}] {doc}")

        log.debug("Memory recall: %d/%d matches below threshold", len(memories), actual_n)
        return memories

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float] | None:
        """
        Call Ollama /api/embed and return the embedding vector.
        Returns None on any failure so callers can degrade gracefully.
        """
        if not self._client:
            return None
        try:
            resp = await self._client.post(
                "/api/embed",
                json={"model": self._embed_model, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()
            # /api/embed returns {"embeddings": [[...]]}
            embeddings = data.get("embeddings")
            if embeddings and len(embeddings) > 0:
                return embeddings[0]
            # Older Ollama fallback: /api/embeddings returns {"embedding": [...]}
            embedding = data.get("embedding")
            if embedding:
                return embedding
            log.warning("Unexpected embed response shape: %s", list(data.keys()))
            return None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Model not pulled — caller will disable memory
                log.debug("Embed model not found: %s", self._embed_model)
            else:
                log.warning("Embed HTTP error: %s", e)
            return None
        except Exception as e:
            log.warning("Embed failed: %s", e)
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stable_id(user: str, ts: float) -> str:
    """Deterministic ID from content + timestamp."""
    raw = f"{user[:100]}{ts}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _ts_label(ts: float) -> str:
    """Human-readable relative time label for a stored memory."""
    if ts == 0:
        return "earlier"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
