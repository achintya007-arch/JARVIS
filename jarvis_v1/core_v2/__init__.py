"""
core_v2 — Event-driven orchestration layer for JARVIS V2.

The stable fallback alongside V3. Launch with `python main.py` (or
`run.ps1 -V2`).

Public exports:
    Brain     — V2 orchestrator (successor to the retired V1 assistant)
    EventBus  — In-process async pub/sub dispatcher
"""

from core_v2.event_bus import EventBus

__all__ = ["EventBus"]
