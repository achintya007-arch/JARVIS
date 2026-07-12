"""
core_v2/event_bus.py — Minimal in-process async pub/sub.

Design principles:
  - publish() is NON-BLOCKING: each handler runs as an independent asyncio.Task.
    The publisher does not await handler completion, so one slow handler (e.g.
    TTS draining) never stalls another subscriber or the event loop.
  - publish_and_wait() awaits all handlers — use sparingly for rare sequencing
    needs (e.g. Brain waiting for the startup event to be fully handled).
  - Exceptions in handlers are caught per-task and logged; one failing handler
    never affects others.
  - No serialization, no queue, no middleware. Events are Python objects
    passed by reference — zero-copy, sub-millisecond dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Coroutine

log = logging.getLogger("jarvis.event_bus")

Handler = Callable[[Any], Coroutine[Any, Any, None]]


class EventBus:
    """
    Central in-process event dispatcher.

    Usage:
        bus = EventBus()
        bus.subscribe(UserInput, my_async_handler)
        bus.publish(UserInput(text="hello", source="keyboard"))
    """

    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)

    # ── Registration ──────────────────────────────────────────────────────────

    def subscribe(self, event_type: type, handler: Handler) -> None:
        """Register an async handler for an event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        """Remove a previously registered handler (no-op if not found)."""
        try:
            self._handlers[event_type].remove(handler)
        except (KeyError, ValueError):
            pass

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def publish(self, event: Any) -> None:
        """
        Fire-and-forget dispatch.

        Each handler is scheduled as an independent asyncio.Task.
        Returns immediately — callers do NOT await handler completion.
        Use this for the vast majority of event publications.
        """
        handlers = list(self._handlers.get(type(event), []))
        loop = asyncio.get_running_loop()

        for handler in handlers:
            async def runner(h=handler):
                try:
                    await h(event)
                except Exception as e:
                    log.error("Event handler task failed: %s", e, exc_info=e)

            loop.create_task(runner())

    async def publish_and_wait(self, event: Any) -> None:
        """
        Blocking dispatch — awaits all handlers before returning.

        Use sparingly: breaks the non-blocking contract and can introduce
        latency. Appropriate for lifecycle events where Brain must know
        that subscribers have finished before proceeding (e.g. startup).
        """
        handlers = self._handlers.get(type(event), [])
        if not handlers:
            return
        results = await asyncio.gather(
            *[handler(event) for handler in handlers],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                log.error("Handler error for %s: %s", type(event).__name__, result)


# ── Task error callback ───────────────────────────────────────────────────────

def _log_task_error(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc:
        log.error("Event handler task failed: %s", exc, exc_info=exc)
