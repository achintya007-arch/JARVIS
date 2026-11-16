"""
Live activity for the HUD (BBP milestone 5, amendment 23).

- RunningTools: the tool calls in progress, for the welcome snapshot
  (`running_tools`), so a Body that reconnects mid-call still shows them.
- AgentStateThrottle: when to send `agent.state`. At once when the model, the
  fallback flag, the mood or the intent changes; for load (RAM / VRAM %), at
  most every LOAD_INTERVAL_S and only after a move of LOAD_STEP points or more.
"""

from __future__ import annotations

import time
from typing import Any

LOAD_STEP = 5.0                 # percentage points
LOAD_INTERVAL_S = 5.0
CHECK_INTERVAL_S = 1.0          # how often the server looks at the agent state
RUNNING_MAX = 32                # a stuck reporter can't grow the snapshot forever
RUNNING_STALE_S = 300.0         # forget a call that never finished after 5 min


class RunningTools:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._calls: dict[str, tuple[float, dict[str, Any]]] = {}

    def started(self, call_id: str, tool: str, summary: str) -> None:
        self._prune()
        if len(self._calls) >= RUNNING_MAX:
            oldest = min(self._calls, key=lambda k: self._calls[k][0])
            del self._calls[oldest]
        self._calls[call_id] = (self._clock(), {"call_id": call_id, "tool": tool, "summary": summary})

    def finished(self, call_id: str) -> None:
        self._calls.pop(call_id, None)

    def snapshot(self) -> list[dict[str, Any]]:
        """Oldest first, each with elapsed_s (how long it has been running)."""
        self._prune()
        now = self._clock()
        return [
            {**item, "elapsed_s": round(max(0.0, now - started), 1)}
            for started, item in sorted(self._calls.values(), key=lambda v: v[0])
        ]

    def _prune(self) -> None:
        now = self._clock()
        for call_id in [k for k, (t, _) in self._calls.items() if now - t > RUNNING_STALE_S]:
            del self._calls[call_id]


def _identity(state: dict[str, Any]) -> tuple:
    return (state.get("model"), bool(state.get("fallback")), state.get("mood"), state.get("intent"))


def _moved(before: Any, after: Any) -> bool:
    if before is None or after is None:
        return (before is None) != (after is None)       # a reading appeared or vanished
    try:
        return abs(float(after) - float(before)) >= LOAD_STEP
    except (TypeError, ValueError):
        return False


class AgentStateThrottle:
    """Decides whether the current agent state is worth an `agent.state` event.
    The first state seen is only remembered: the welcome snapshot carries it."""

    def __init__(self) -> None:
        self._last: dict[str, Any] | None = None
        self._last_load_at = float("-inf")

    def check(self, state: dict[str, Any], now: float) -> bool:
        if self._last is None:
            self._remember(state, now)
            return False
        if _identity(state) != _identity(self._last):
            self._remember(state, now)
            return True
        before = self._last.get("load") or {}
        after = state.get("load") or {}
        if now - self._last_load_at >= LOAD_INTERVAL_S and any(
            _moved(before.get(k), after.get(k)) for k in ("ram_pct", "vram_pct")
        ):
            self._remember(state, now)
            return True
        return False

    def _remember(self, state: dict[str, Any], now: float) -> None:
        self._last = state
        self._last_load_at = now
