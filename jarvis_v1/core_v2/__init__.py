"""
core_v2 — Event-driven orchestration layer for JARVIS V2.

Drop-in alongside core/ — select via config.yaml:
    orchestrator: v2

Public exports:
    Brain     — V2 orchestrator (replaces legacy.assistant.Assistant)
    EventBus  — In-process async pub/sub dispatcher
"""

from core_v2.event_bus import EventBus

__all__ = ["EventBus"]
