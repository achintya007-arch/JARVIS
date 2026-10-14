"""
core_v3/events.py — event types for the V3 bus.

The core event classes are shared with V2 (re-exported here, not redefined) so
that modules which import events from core_v2 — notably cognition/goal_manager.py
— register handlers under the *same* classes the V3 bus dispatches. Redefining
them here would give distinct types and silently break cross-module subscriptions.

V3 adds one event of its own: VaultEvent.

(The shared event classes live in core_common/events.py; V2 and V3 both
re-export them from there.)
"""

from __future__ import annotations

from dataclasses import dataclass

from core_common.events import (  # noqa: F401  (re-exported)
    PRIORITY_CRITICAL,
    PRIORITY_IMPORTANT,
    PRIORITY_NORMAL,
    ActionSuggestion,
    DecisionReady,
    ResourceAlert,
    SpeakRequest,
    StateUpdated,
    StreamComplete,
    SystemEvent,
    ThoughtGenerated,
    ToolRequest,
    ToolResult,
    UserInput,
)

# ── V3-only: vault events ─────────────────────────────────────────────────────

@dataclass
class VaultEvent:
    """
    Published whenever JARVIS reads or writes a vault file.
    The vault indexer may subscribe to queue affected paths for re-embedding.
    """
    kind:     str        # "read" | "write" | "create" | "delete"
    path:     str        # absolute path inside the vault
    priority: int = PRIORITY_NORMAL
