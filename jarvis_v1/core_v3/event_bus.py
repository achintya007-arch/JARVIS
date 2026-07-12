"""
core_v3/event_bus.py — re-export of the shared EventBus.

The EventBus lives in core_common.event_bus (one implementation, shared by V2
and V3). Re-exported here so `from core_v3.event_bus import EventBus` resolves.
"""

from core_common.event_bus import EventBus, Handler  # noqa: F401
