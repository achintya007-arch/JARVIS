"""
memory/embeddings.py — shared Ollama embedding client.

Single source of truth for turning text into vectors via Ollama's
/api/embed (nomic-embed-text by default). Used by the vault indexer, the web
RAG index, and the offline FineWeb build script so the embedding logic lives in
exactly one place.

Graceful by design: embed() returns None on any failure so callers can degrade
instead of crashing.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("jarvis.embeddings")

_DEFAULT_TIMEOUT = 8.0


class OllamaEmbedder:
    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._url     = ollama_url.rstrip("/")
        self._model   = model
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._url, timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> bool:
        """Return True if the embed model responds (model pulled, Ollama up)."""
        return await self.embed("probe") is not None

    async def embed(self, text: str) -> Optional[list[float]]:
        if self._client is None:
            await self.start()
        try:
            resp = await self._client.post(
                "/api/embed", json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings")
            if embeddings:
                return embeddings[0]
            embedding = data.get("embedding")  # older Ollama shape
            return embedding or None
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                log.warning("Embed HTTP error: %s", e)
            return None
        except Exception as e:
            log.warning("Embed failed: %s", e)
            return None

    @property
    def model(self) -> str:
        return self._model
