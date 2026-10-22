"""
RemoteDesktop — the Brain's client for desktop.* requests to the C# Body.

AgentExecutor's desktop tools (open_app, focus_window, list_windows,
set_volume, read_screen_ui) ask `available(topic)`; when a connected Body
declared the matching capability they call `call(topic, payload)` instead of the
Python implementation. Errors come back as DesktopError with the Body's code
(E_NOT_ALLOWED, E_NOT_FOUND, E_TIMEOUT after 5 s, …).
"""

from __future__ import annotations

from typing import Any

from action.desktop_errors import DesktopError
from core_v3.adapters import bbp_protocol as bbp

# Which hello capability a Body must declare for each request topic (amendment 15).
CAPABILITY = {
    "desktop.app.launch": "apps",
    "desktop.window.focus": "apps",
    "desktop.window.list": "apps",
    "desktop.audio.set_volume": "audio",
    "desktop.uia.read": "uia",
}


class RemoteDesktop:
    def __init__(self, server, timeout_s: float = 5.0) -> None:
        self._server = server
        self._timeout = timeout_s

    def available(self, topic: str) -> bool:
        cap = CAPABILITY.get(topic)
        return cap is not None and self._server.body_with(cap) is not None

    async def call(self, topic: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._server.request(topic, payload, capability=CAPABILITY[topic],
                                              timeout=self._timeout)
        except bbp.BBPError as e:
            raise DesktopError(e.code, e.message) from None
