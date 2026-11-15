"""
ConfirmationBroker — one owner for every pending confirmation (BBP milestone 3).

A gated tool (run_shell, write_file, close_app, close_window) calls ask(). The
broker publishes ConfirmRequested (→ BBP confirm.request on every Body), then
races every way you can answer:

  channels   coroutines the caller passes in: your voice in voice mode, the
             console in text mode. Each returns True / False, or None for
             "no answer from me" (silence), which leaves the others racing.
  answer()   a Body's dialog (BBP confirm.response, by="body").

The first answer wins; ConfirmResolved (→ confirm.resolved) closes every dialog.
Fail closed (spec + amendment 16):

  no answer within timeout_s           → denied, by="timeout"
  a channel raising, ask() cancelled   → denied, by="error"
  deny_all() (a HUD Body disconnected) → denied, by="error"
  an unknown or settled id in answer() → refused (False), nothing changes

The deadline starts when the request goes out and covers every channel, so the
dialog countdown and the spoken prompt share the same 15 s.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from action.confirmation import ConfirmRequest
from core_v3.events import ConfirmRequested, ConfirmResolved

log = logging.getLogger("jarvis.confirm")

# (request, seconds left) → True approve / False deny / None no answer
Channel = Callable[[ConfirmRequest, float], Awaitable["bool | None"]]
BY_VALUES = frozenset({"body", "voice", "text", "timeout", "error"})


@dataclass(frozen=True)
class Resolution:
    approved: bool
    by: str


@dataclass
class _Pending:
    request: ConfirmRequest
    deadline: float
    timeout_s: float
    future: asyncio.Future = field(repr=False)


class ConfirmationBroker:
    def __init__(self, bus=None, timeout_s: float = 15.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._timeout = float(timeout_s)
        self._clock = clock
        self._pending: dict[str, _Pending] = {}

    @property
    def is_pending(self) -> bool:
        return bool(self._pending)

    def pending(self) -> list[dict]:
        """For the BBP welcome snapshot: confirm.request payloads, timeout_s = time left."""
        now = self._clock()
        items = []
        for cid, p in self._pending.items():
            left = round(p.deadline - now, 2)
            if left <= 0:
                continue                         # about to resolve as a timeout
            item = {"confirm_id": cid, "prompt": p.request.prompt, "risk": p.request.risk,
                    "timeout_s": left}
            if p.request.detail:
                item["detail"] = p.request.detail
            items.append(item)
        return items

    async def ask(self, request: ConfirmRequest, channels: dict[str, Channel] | None = None,
                  timeout_s: float | None = None) -> Resolution:
        timeout = self._timeout if timeout_s is None else float(timeout_s)
        cid = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cid] = _Pending(request, self._clock() + timeout, timeout, future)
        log.info("confirmation %s: %s [%s] %s", cid[:8], request.tool, request.risk, request.detail)
        self._publish(ConfirmRequested(confirm_id=cid, prompt=request.prompt, risk=request.risk,
                                       timeout_s=timeout, detail=request.detail))
        tasks = [asyncio.create_task(self._run_channel(cid, by, channel, request, timeout),
                                     name=f"confirm-{by}")
                 for by, channel in (channels or {}).items()]
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            self._settle(cid, False, "timeout")
        except asyncio.CancelledError:
            self._settle(cid, False, "error")       # the turn was cancelled: deny
            raise
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._pending.pop(cid, None)
        return future.result()

    def answer(self, confirm_id: str, approved: bool, by: str = "body") -> bool:
        """Settle a pending confirmation. False if it isn't pending (unknown id,
        already answered, timed out): the caller refuses with E_NOT_FOUND."""
        return self._settle(confirm_id, approved is True, by)

    def deny_all(self, by: str = "error") -> int:
        """Deny everything pending (a HUD Body went away mid-confirmation)."""
        return sum(self._settle(cid, False, by) for cid in list(self._pending))

    # ── internals ─────────────────────────────────────────────────────────────
    async def _run_channel(self, cid: str, by: str, channel: Channel,
                           request: ConfirmRequest, timeout: float) -> None:
        try:
            answer = await channel(request, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("confirmation %s: %s channel failed (%s): denying", cid[:8], by, e)
            self._settle(cid, False, "error")
            return
        if answer is not None:
            self._settle(cid, answer is True, by)

    def _settle(self, cid: str, approved: bool, by: str) -> bool:
        pending = self._pending.get(cid)
        if pending is None or pending.future.done():
            return False
        if by not in BY_VALUES:
            by = "error"
        pending.future.set_result(Resolution(approved, by))
        log.info("confirmation %s: %s by %s", cid[:8], "approved" if approved else "denied", by)
        self._publish(ConfirmResolved(confirm_id=cid, approved=approved, by=by))
        return True

    def _publish(self, event) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(event)
        except Exception as e:                       # observers never break a confirmation
            log.debug("publish failed: %s", e)
