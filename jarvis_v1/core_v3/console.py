"""
ConsoleInput — the text-mode console, shared by the chat loop and confirmations.

In text mode a gated action used to listen to a microphone that wasn't running:
it froze for 15 s and could never be approved. Now the confirmation reads the
console instead, racing the Body's dialog. That needs one stdin reader shared
by both:

- A blocking input() can't be cancelled. If the dialog answers first, the read
  already started stays in flight, and its line goes to the next caller instead
  of being lost to an abandoned thread.
- Each line goes to exactly one caller, the newest one waiting. A confirmation
  asked while the chat loop is also waiting (a HUD-typed turn in text mode)
  gets your "yes"; the chat loop doesn't also take it as a message.
- End of input (EOF) reaches every caller, now and later.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable

Reader = Callable[[str], str]


class ConsoleInput:
    def __init__(self, read: Reader | None = None) -> None:
        self._read = read
        self._reading: asyncio.Future | None = None
        self._waiters: list[tuple[asyncio.Future, str]] = []    # (caller, its prompt), oldest first
        self._lines: deque[str] = deque()                       # typed while nobody was waiting
        self._error: BaseException | None = None                # EOF etc.: sticky

    async def readline(self, prompt: str) -> str:
        """The next line typed. Raises EOFError / KeyboardInterrupt like input()."""
        if self._lines:
            return self._lines.popleft()
        if self._error is not None:
            raise self._error
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append((waiter, prompt))
        if self._reading is None:
            self._start(prompt)
        else:
            print(prompt, end="", flush=True)   # the read in flight showed an older prompt
        try:
            return await waiter
        finally:
            self._waiters = [(w, p) for w, p in self._waiters if w is not waiter]

    def _start(self, prompt: str) -> None:
        read = self._read or input              # looked up late: tests patch builtins.input
        self._reading = asyncio.ensure_future(asyncio.to_thread(read, prompt))
        self._reading.add_done_callback(self._delivered)

    def _delivered(self, future: asyncio.Future) -> None:
        self._reading = None
        if future.cancelled():
            return
        error = future.exception()
        live = [(w, p) for w, p in self._waiters if not w.done()]
        if error is not None:
            self._error = error
            for waiter, _ in live:
                waiter.set_exception(error)
            return
        line = future.result()
        if live:
            live[-1][0].set_result(line)        # newest caller first
            live = live[:-1]
        else:
            self._lines.append(line)
        if live:
            self._start(live[-1][1])            # someone is still waiting: keep reading
