"""
core_v3/adapters — thin bridges between subsystems and the V3 EventBus.

resource.py      ResourceAdapter — resource pressure → model switching + alerts
bbp_server.py    BBPServer       — Brain–Body Protocol WebSocket server: forwards
                                   bus events to connected Bodies (C# shell) and
                                   turns their commands into Brain actions
bbp_protocol.py  envelope building + JSON Schema validation (protocol/ folder)
phonebox.py      PhoneBoxAdapter — mirrors JARVIS's state on a PhoneBox desk panel
                                   (local webhooks; optional)
"""

from core_v3.adapters.resource import ResourceAdapter

__all__ = ["ResourceAdapter"]
