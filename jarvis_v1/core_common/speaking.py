"""core_common/speaking.py — the shared "is JARVIS talking" flag."""

from __future__ import annotations


class SpeakingGuard:
    """
    Shared mutable "is JARVIS currently talking" flag.

    Brain's STT-suppression check and Brain-initiated speech (ack phrases, tool
    responses, LLM streaming) both go through Brain's own self._speaking.
    But SpeakRequest events published straight onto the bus at CRITICAL/
    IMPORTANT priority (resource alerts, goal reminders) are spoken directly by
    ActionAdapter, bypassing Brain entirely — so without a SHARED flag, STT
    keeps listening while JARVIS talks over those, and can hear (and act on)
    its own voice bleeding through the speakers. Brain and ActionAdapter both
    hold a reference to the same instance so either can set/read it.
    """

    def __init__(self) -> None:
        self.value: bool = False
