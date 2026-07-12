"""
core_v2/event_bus.py — backwards-compat shim.

The EventBus now lives in core_common.event_bus (shared). Re-exported here so
existing `from core_v2.event_bus import EventBus` imports keep working.
"""

from core_common.event_bus import EventBus, Handler  # noqa: F401
