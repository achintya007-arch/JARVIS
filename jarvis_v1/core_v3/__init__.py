"""
core_v3 — Vault-centric JARVIS orchestrator.

V3 replaces V2's SQLite + ChromaDB memory layer with:
  - An Obsidian markdown vault (full read/write/link) as primary memory
  - A read-only FineWeb-derived RAG corpus for general world-knowledge

Perception, action, FastRouter, AgentExecutor, EventBus are ported from V2.
The Brain, memory layer, and goal storage are rewritten.
"""
