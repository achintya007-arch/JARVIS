"""
core_v3/events.py — event types for the V3 bus.

The core event classes are shared with V2 (re-exported here, not redefined) so
that modules which import events from core_v2 — notably cognition/goal_manager.py
— register handlers under the *same* classes the V3 bus dispatches. Redefining
them here would give distinct types and silently break cross-module subscriptions.

V3 adds its own events below: VaultEvent, plus the voice/conversation events the
Brain–Body Protocol server (core_v3/adapters/bbp_server.py) forwards to Bodies.
Since the voice rebuild, the voice layer and ConversationEngine talk through
direct calls; they now ALSO publish these facts on the bus so observers (the BBP
server today) can subscribe instead of reaching into those classes.

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


# ── V3-only: voice + conversation observability (Brain–Body Protocol) ─────────
# Facts, published after the fact. Nothing in the Brain depends on anyone
# subscribing to them, so voice and text mode behave identically whether or not
# a BBP server or Body is present.

@dataclass
class VoiceStateChanged:
    """The voice state machine moved (IDLE/LISTENING/PROCESSING/SPEAKING/
    INTERRUPTED), or the microphone was muted/unmuted. -> BBP voice.state"""
    state:    str        # lowercase VoiceState value, e.g. "listening"
    muted:    bool = False
    priority: int = PRIORITY_NORMAL


@dataclass
class TranscriptReady:
    """A captured utterance was transcribed. -> BBP voice.transcript"""
    text:       str
    final:      bool = True              # Whisper does not stream partials
    confidence: float | None = None      # 0-1, from Whisper avg log-prob
    priority:   int = PRIORITY_NORMAL


@dataclass
class ReplyChunk:
    """One sentence of a reply, as ConversationEngine yields it. -> BBP reply.chunk"""
    turn_id:  str
    seq:      int
    text:     str
    priority: int = PRIORITY_NORMAL


@dataclass
class ReplyDone:
    """A reply finished (or was cut off by barge-in/cancel). -> BBP reply.done"""
    turn_id:     str
    interrupted: bool = False
    priority:    int = PRIORITY_NORMAL
