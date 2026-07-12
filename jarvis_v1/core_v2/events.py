"""
core_v2/events.py — backwards-compat shim.

The canonical event types now live in core_common.events (shared by V2 and V3
so V3 doesn't depend on V2). Re-exported here so existing
`from core_v2.events import ...` imports keep resolving to the SAME classes.
"""

from core_common.events import (  # noqa: F401
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
