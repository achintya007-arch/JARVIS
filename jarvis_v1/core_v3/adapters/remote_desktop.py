"""
RemoteDesktop — the Brain's client for desktop.* requests to the C# Body.

AgentExecutor's desktop tools (open_app, focus_window, list_windows,
set_volume, read_screen_ui, click_element, set_element_text) ask `available(topic)`; when a connected Body
declared the matching capability they call `call(topic, payload)` instead of the
Python implementation. Errors come back as DesktopError with the Body's code
(E_NOT_ALLOWED, E_NOT_FOUND, E_TIMEOUT after 5 s, …). Clicking and typing
get longer (ACTION_TIMEOUT_S): the Body may wait for the app's window to
appear, for the approval to arrive, and for a slow button.
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
    "desktop.uia.invoke": "uia",
    "desktop.uia.set_text": "uia",
}

# The Body may spend up to ~2 s finding the element, ~2 s waiting for the
# approval and ~3 s pressing (amendment 22). A shorter wait would report
# failure for a press that then happens.
ACTION_TIMEOUT_S = 15.0
ACTIONS = ("desktop.uia.invoke", "desktop.uia.set_text")


class RemoteDesktop:
    def __init__(self, server, timeout_s: float = 5.0) -> None:
        self._server = server
        self._timeout = timeout_s

    def timeout_for(self, topic: str) -> float:
        return max(self._timeout, ACTION_TIMEOUT_S) if topic in ACTIONS else self._timeout

    def available(self, topic: str) -> bool:
        cap = CAPABILITY.get(topic)
        return cap is not None and self._server.body_with(cap) is not None

    async def call(self, topic: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._server.request(topic, payload, capability=CAPABILITY[topic],
                                              timeout=self.timeout_for(topic))
        except bbp.BBPError as e:
            raise DesktopError(e.code, e.message) from None
