"""
memory — V3 vault-centric memory layer.

Modules (filled in across V3 phases):
  vault.py         (Phase B) — Obsidian read/write primitives
  vault_indexer.py (Phase B) — Chroma index over vault notes
  vault_memory.py  (Phase B) — Facade: build_messages, add_exchange, recall
  web_rag.py       (Phase D) — Read-only FineWeb RAG client

Phase A: package exists so imports don't fail; modules are added as phases land.
"""
