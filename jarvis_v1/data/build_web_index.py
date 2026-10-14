"""
data/build_web_index.py — build the web-knowledge Chroma index from FineWeb.

Offline, run once (or monthly to refresh):

    ollama pull nomic-embed-text          # if not already present
    pip install -e ".[data]"              # for the `datasets` package
    python -m data.build_web_index --gb 1.0

Streams FineWeb-Edu, filters through data/blocklists, embeds each accepted
chunk with nomic-embed-text (via the shared OllamaEmbedder), and writes to the
Chroma collection "web_index" at WebRagConfig.index_dir.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys

from core.config import Config
from data.fineweb_pipeline import iter_chunks
from memory.embeddings import OllamaEmbedder

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("jarvis.build_web_index")

_COLLECTION = "web_index"
_BATCH = 64


def _chunk_id(text: str, url: str) -> str:
    return hashlib.sha1(f"{url}{text[:200]}".encode()).hexdigest()[:16]


async def build(target_gb: float, cfg: Config) -> int:
    import chromadb

    embedder = OllamaEmbedder(cfg.llm.base_url, cfg.web_rag.embed_model)
    await embedder.start()
    if not await embedder.probe():
        log.error("Embed model %r unavailable. Run: ollama pull %s",
                  cfg.web_rag.embed_model, cfg.web_rag.embed_model)
        await embedder.close()
        return 0

    chroma = chromadb.PersistentClient(path=cfg.web_rag.index_dir)
    coll = chroma.get_or_create_collection(_COLLECTION, metadata={"hnsw:space": "cosine"})

    ids: list[str] = []
    embs: list[list[float]] = []
    docs: list[str] = []
    metas: list[dict] = []
    total = 0

    def flush():
        nonlocal ids, embs, docs, metas
        if ids:
            coll.upsert(ids=ids, embeddings=embs, documents=docs, metadatas=metas)
            ids, embs, docs, metas = [], [], [], []

    try:
        for chunk, url in iter_chunks(target_gb):
            emb = await embedder.embed(chunk)
            if emb is None:
                continue
            ids.append(_chunk_id(chunk, url))
            embs.append(emb)
            docs.append(chunk)
            metas.append({"source": url or "web"})
            total += 1
            if len(ids) >= _BATCH:
                await asyncio.to_thread(flush)
                if total % (_BATCH * 20) == 0:
                    log.info("Embedded %d chunks...", total)
        await asyncio.to_thread(flush)
    finally:
        await embedder.close()

    log.info("Done. %d chunks embedded into %s (%s).",
             total, cfg.web_rag.index_dir, _COLLECTION)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the FineWeb web-knowledge index.")
    ap.add_argument("--gb", type=float, default=1.0, help="Approx GB of FineWeb text to ingest")
    args = ap.parse_args()

    cfg = Config()
    count = asyncio.run(build(args.gb, cfg))
    if count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
