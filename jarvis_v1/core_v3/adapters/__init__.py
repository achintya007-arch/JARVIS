"""
core_v3/adapters — thin bridges between subsystems and the V3 EventBus.

resource.py      ResourceAdapter — resource pressure → model switching + alerts
bbp_server.py    BBPServer       — Brain–Body Protocol WebSocket server: forwards
                                   bus events to connected Bodies (C# shell) and
                                   turns their commands into Brain actions
bbp_protocol.py  envelope building + JSON Schema validation (protocol/ folder)
"""

from core_v3.adapters.resource import ResourceAdapter

__all__ = ["ResourceAdapter"]
