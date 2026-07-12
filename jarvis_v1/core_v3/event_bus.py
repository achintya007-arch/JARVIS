"""
core_v3/event_bus.py — Minimal in-process async pub/sub.

Ported verbatim from core_v2/event_bus.py (semantics unchanged).

Design principles:
  - publish() is NON-BLOCKING: each handler runs as an independent asyncio.Task.
  - publish_and_wait() awaits all handlers — use sparingly for rare sequencing.
  - Exceptions in handlers are caught per-task and logged.
  - No serialization, no queue, no middleware. Sub-millisecond dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

log = logging.getLogger("jarvis.event_bus")

Handler = Callable[[Any], Coroutine[Any, Any, None]]


class EventBus:
    """Central in-process event dispatcher."""

    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: Handler) -> None:
        try:
            self._handlers[event_type].remove(handler)
        except (KeyError, ValueError):
            pass

    def publish(self, event: Any) -> None:
        """Fire-and-forget dispatch — schedules each handler as its own task."""
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
        """Blocking dispatch — awaits all handlers before returning."""
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
