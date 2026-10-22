"""
Errors shared by every desktop-control path (Python tools, the C# Body over
BBP). Each carries a BBP error code so a refusal reads the same whichever path
ran: E_NOT_ALLOWED becomes a "denied" tool result, anything else an "error".
"""

from __future__ import annotations


class DesktopError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DesktopUnavailable(DesktopError):
    """No Body can do it and the Python fallback is off (or doesn't exist)."""

    def __init__(self, message: str) -> None:
        super().__init__("E_NOT_FOUND", message)
