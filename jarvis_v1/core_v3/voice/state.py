"""
VoiceState + VoiceStateManager — the explicit state machine.

States:
  IDLE        only the wake-word detector is active
  LISTENING   capturing the user's utterance (endpointer decides when done)
  PROCESSING  transcribing + running cognition
  SPEAKING    playing TTS, with barge-in monitoring active
  INTERRUPTED transient: user barged in; cancel + return to LISTENING

The manager holds the current state and logs every transition. Actual task
cancellation on transition is the orchestrator's job — this keeps the state
representation tiny and independently testable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum

log = logging.getLogger("jarvis.voice.state")


class VoiceState(Enum):
    IDLE        = "IDLE"
    LISTENING   = "LISTENING"
    PROCESSING  = "PROCESSING"
    SPEAKING    = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"


# Allowed transitions — anything else is a bug and is logged as a warning.
_ALLOWED: dict[VoiceState, set[VoiceState]] = {
    VoiceState.IDLE:        {VoiceState.LISTENING},
    VoiceState.LISTENING:   {VoiceState.PROCESSING, VoiceState.IDLE},
    VoiceState.PROCESSING:  {VoiceState.SPEAKING, VoiceState.IDLE, VoiceState.INTERRUPTED},
    VoiceState.SPEAKING:    {VoiceState.INTERRUPTED, VoiceState.IDLE},
    VoiceState.INTERRUPTED: {VoiceState.LISTENING, VoiceState.IDLE},
}


class VoiceStateManager:
    def __init__(self, on_change: Callable[[VoiceState, VoiceState], None] | None = None) -> None:
        self._state = VoiceState.IDLE
        self._on_change = on_change

    @property
    def state(self) -> VoiceState:
        return self._state

    def is_(self, *states: VoiceState) -> bool:
        return self._state in states

    def transition(self, to: VoiceState) -> VoiceState:
        old = self._state
        if to != old and to not in _ALLOWED.get(old, set()):
            log.warning("Unexpected transition %s → %s", old.value, to.value)
        log.info("state: %s → %s", old.value, to.value)
        self._state = to
        if self._on_change is not None:
            self._on_change(old, to)
        return to
