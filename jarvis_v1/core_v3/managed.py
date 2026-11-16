"""
Running under the Body launcher (JARVIS_MANAGED=1).

The Body starts the Brain as a hidden child process and keeps its stdin. That
pipe is the control channel: a line "shutdown", or the pipe closing because the
Body exited or crashed, stops the Brain the same way Ctrl+C does (BBP closes
with 1000, the vault and TTS shut down). Nothing else reads stdin in this mode:
text mode has no console prompt (typed input comes from the HUD) and
confirmations are answered on the HUD or by voice, not the console.

The pipe is moved off stdin before it's read (take_control_pipe): fd 0 and
the Windows standard input handle point at NUL instead. On Windows a blocking
read on a pipe holds up every other call on it, and some native libraries
(scipy's Fortran runtime, imported while the Brain starts) query the standard
input handle when they load; with the read pending on stdin itself, that
import hangs forever. Child processes inherit NUL too, so nothing else can
read the launcher's lines.

The Body is a BBP client, so BBP is always on here, on the port the Body
connects to (JARVIS_BBP_PORT), whatever config.yaml says.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections.abc import Callable, Mapping
from typing import TextIO

log = logging.getLogger("jarvis.managed")

ENV_MANAGED = "JARVIS_MANAGED"
ENV_BBP_PORT = "JARVIS_BBP_PORT"
SHUTDOWN_LINE = "shutdown"


def is_managed(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(ENV_MANAGED, "") == "1"


def apply(config, environ: Mapping[str, str] = os.environ) -> None:
    """Adjust the loaded config for a launcher-managed Brain."""
    config.managed = True
    config.bbp.enabled = True
    port = environ.get(ENV_BBP_PORT, "")
    if port:
        try:
            value = int(port)
        except ValueError:
            log.warning("%s=%r isn't a port; keeping %d", ENV_BBP_PORT, port, config.bbp.port)
        else:
            if 0 < value < 65536:
                config.bbp.port = value
            else:
                log.warning("%s=%d is out of range; keeping %d", ENV_BBP_PORT, value, config.bbp.port)


def take_control_pipe() -> TextIO:
    """The launcher's pipe as a private stream; stdin becomes NUL."""
    private = os.dup(0)                        # same pipe, a descriptor only we hold
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)
    os.close(nul)
    if os.name == "nt":
        import ctypes
        import msvcrt

        std_input_handle = -10
        ctypes.windll.kernel32.SetStdHandle(std_input_handle, msvcrt.get_osfhandle(0))
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 - lives as long as the process
    return os.fdopen(private, "r", encoding="utf-8", errors="replace")


def watch_stdin(loop: asyncio.AbstractEventLoop, on_shutdown: Callable[[], None],
                stream: TextIO | None = None) -> threading.Thread:
    """Call `on_shutdown` (once, on the loop) when the launcher says so or goes away.

    Without `stream`, reads the launcher's pipe after moving it off stdin
    (see take_control_pipe)."""
    stream = stream if stream is not None else take_control_pipe()
    fired = threading.Event()

    def fire(why: str) -> None:
        if fired.is_set():
            return
        fired.set()
        log.info("Launcher %s: shutting down.", why)
        try:
            loop.call_soon_threadsafe(on_shutdown)
        except RuntimeError:
            pass                                    # loop already closed

    def run() -> None:
        try:
            for line in stream:
                if line.strip().lower() == SHUTDOWN_LINE:
                    fire("asked")
                    return
        except (OSError, ValueError):
            pass
        fire("went away (stdin closed)")

    thread = threading.Thread(target=run, name="launcher-stdin", daemon=True)
    thread.start()
    return thread
