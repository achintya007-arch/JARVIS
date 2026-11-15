"""
BBPServer — the Brain side of the Brain–Body Protocol (docs/protocol/BBP_v0.1.md).

A thin adapter, like ResourceAdapter: it subscribes to V3 EventBus events and
forwards them to every connected Body as BBP events, and turns Body commands
into Brain actions (milestone 2+). It owns no cognition.

  Body → hello{token}      Brain → welcome{snapshot}     (bad token: close 4401)
  Body → ping (every 5 s)  Brain → pong                   (silent > timeout: close 4408)
  Brain → voice.state / voice.transcript / reply.chunk / reply.done  (events)
  Brain → confirm.request / confirm.resolved / notify                 (events)
  Body → input.text / voice.control / ui.hotkey / confirm.response / notify.action
         (commands; see bbp_commands.py)
  Brain → desktop.* request → Body → response (corr = request id; 5 s → E_TIMEOUT)

Fail-safe by construction: the server is optional (bbp.enabled), a failed
start only logs, every Body has its own bounded send queue (a stalled Body is
disconnected, never waited on), and bus handlers can't raise into the Brain.
Voice and text mode behave identically with zero, one, or many Bodies.
"""

from __future__ import annotations

import asyncio
import getpass
import hmac
import itertools
import json
import logging
import os
import secrets
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from core_v3.adapters import bbp_protocol as bbp
from core_v3.events import (
    PRIORITY_IMPORTANT,
    ActionSuggestion,
    ConfirmRequested,
    ConfirmResolved,
    Notice,
    ReplyChunk,
    ReplyDone,
    SpeakRequest,
    TimerFinished,
    TranscriptReady,
    VoiceStateChanged,
)

log = logging.getLogger("jarvis.bbp")

BRAIN_VERSION = "3.0"
CLOSE_NORMAL = 1000               # spec: Brain shutting down
CLOSE_AUTH = 4401                 # spec: bad or missing token
CLOSE_HEARTBEAT = 4408            # spec: idle timeout (nothing received for heartbeat_timeout_s)
_SEND_QUEUE_MAX = 512             # per-Body backlog before we drop the Body
_CONNECTION_SEQ = itertools.count(1)
_JARVIS_DIR = Path(__file__).resolve().parents[2]      # …/jarvis_v1

CommandHandler = Callable[["BodyConnection", dict[str, Any]], Awaitable[None]]
DisconnectHandler = Callable[["BodyConnection"], None]
_NOTIFY_TITLE_MAX = 120
_NOTIFY_BODY_MAX = 1000


# ── Auth token ────────────────────────────────────────────────────────────────

def resolve_token_path(token_path: str) -> Path:
    p = Path(token_path)
    return p if p.is_absolute() else _JARVIS_DIR / p


def load_or_create_token(path: Path) -> str:
    """Return the shared token, creating it (readable only by the current user)
    on first run. The Body reads the same file."""
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    if os.name == "nt":
        path.write_text(token, encoding="utf-8")
        _restrict_to_current_user_windows(path)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.chmod(path, 0o600)
    log.info("BBP: created auth token at %s", path)
    return token


def _restrict_to_current_user_windows(path: Path) -> None:
    """Drop inherited ACEs and grant full control to the current user only."""
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{user}" if domain else user
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("BBP: could not restrict token file permissions: %s", result.stderr.strip())


# ── One connected Body ────────────────────────────────────────────────────────

class BodyConnection:
    def __init__(self, ws: ServerConnection, hello: dict[str, Any]) -> None:
        self.ws = ws
        self.session = uuid.uuid4().hex
        self.client = str(hello.get("client", "?"))
        self.version = str(hello.get("version", "?"))
        self.capabilities = frozenset(hello.get("capabilities", []))
        # Strictly increasing (time.monotonic() ties within one ~15 ms Windows tick).
        self.connected_seq = next(_CONNECTION_SEQ)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SEND_QUEUE_MAX)

    def send(self, message: dict[str, Any]) -> None:
        """Queue a message; never blocks. A Body too slow to drain its queue is
        disconnected (it reconnects and gets a fresh snapshot)."""
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("BBP: %s send queue overflow — dropping connection", self.client)
            asyncio.get_running_loop().create_task(self.ws.close(1008, "send queue overflow"))

    async def writer(self) -> None:
        try:
            while True:
                message = await self._queue.get()
                await self.ws.send(bbp.dumps(message))
        except ConnectionClosed:
            pass


# ── Server ────────────────────────────────────────────────────────────────────

class BBPServer:
    def __init__(self, config, bus, snapshot_fn: Callable[[], dict[str, Any]]) -> None:
        self._cfg = config
        self._bus = bus
        self._snapshot_fn = snapshot_fn
        self._server: Server | None = None
        self._token = ""
        self._clients: set[BodyConnection] = set()
        self._commands: dict[str, CommandHandler] = {}
        self._disconnect_handlers: list[DisconnectHandler] = []
        self._tasks: set[asyncio.Task] = set()      # in-flight command handlers
        # Brain → Body requests awaiting a response: request id → (Body, future)
        self._pending: dict[str, tuple[BodyConnection, asyncio.Future]] = {}
        self._subscriptions = [
            (VoiceStateChanged, self._on_voice_state),
            (TranscriptReady, self._on_transcript),
            (ReplyChunk, self._on_reply_chunk),
            (ReplyDone, self._on_reply_done),
            (ConfirmRequested, self._on_confirm_requested),
            (ConfirmResolved, self._on_confirm_resolved),
            # notify (amendment 18): the visual copy of what JARVIS also says.
            (Notice, self._on_notice),
            (TimerFinished, self._on_timer_finished),
            (ActionSuggestion, self._on_suggestion),
            (SpeakRequest, self._on_speak_request),
        ]

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> bool:
        """Bind and start serving. Returns False (and logs) on failure — the
        Brain keeps running without a BBP link."""
        try:
            self._token = load_or_create_token(resolve_token_path(self._cfg.token_path))
            bbp.validate(bbp.envelope("pong", "pong", {}))      # schemas present + sane
            self._server = await serve(
                self._handle,
                self._cfg.host,
                self._cfg.port,
                process_request=self._check_path,
                ping_interval=None,          # the protocol's own ping/pong is the heartbeat
            )
        except Exception as e:
            log.error("BBP server failed to start (%s) — voice/text mode unaffected", e)
            self._server = None
            return False
        for event_type, handler in self._subscriptions:
            self._bus.subscribe(event_type, handler)
        log.info("BBP server listening on ws://%s:%d%s", self._cfg.host, self.port, self._cfg.path)
        return True

    async def stop(self) -> None:
        for event_type, handler in self._subscriptions:
            self._bus.unsubscribe(event_type, handler)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is None:
            return
        # Spec close-code table: 1000 = Brain shutting down (Bodies reconnect
        # with normal backoff). server.close() alone would send 1001.
        await asyncio.gather(
            *(c.ws.close(CLOSE_NORMAL, "brain shutting down") for c in list(self._clients)),
            return_exceptions=True,
        )
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("BBP server did not close cleanly within 3 s")
        self._server = None
        log.info("BBP server stopped")

    @property
    def port(self) -> int:
        if self._server is None:
            return 0
        return self._server.sockets[0].getsockname()[1]

    @property
    def clients(self) -> frozenset[BodyConnection]:
        return frozenset(self._clients)

    def register_command(self, topic: str, handler: CommandHandler) -> None:
        self._commands[topic] = handler

    def on_disconnect(self, handler: DisconnectHandler) -> None:
        """Called (synchronously) after a Body leaves, e.g. to deny a confirmation."""
        self._disconnect_handlers.append(handler)

    # ── Brain → Body requests ─────────────────────────────────────────────────
    def body_with(self, capability: str) -> BodyConnection | None:
        """The most recently connected Body that declared `capability`."""
        bodies = [c for c in self._clients if capability in c.capabilities]
        return max(bodies, key=lambda c: c.connected_seq) if bodies else None

    async def request(self, topic: str, payload: dict[str, Any], *, capability: str,
                      timeout: float) -> dict[str, Any]:
        """Send a request to a capable Body and return its `result`.

        Raises BBPError: the Body's own error (E_NOT_ALLOWED, E_NOT_FOUND, …),
        E_TIMEOUT after `timeout` seconds, E_INTERNAL if the Body drops first,
        E_NOT_FOUND if no connected Body has the capability."""
        message = bbp.envelope("request", topic, payload)
        bbp.validate(message)                         # a malformed request is a Brain bug: fail first
        body = self.body_with(capability)
        if body is None:
            raise bbp.BBPError("E_NOT_FOUND", f"no connected Body has the {capability!r} capability")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = (body, future)
        try:
            body.send(message)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise bbp.BBPError("E_TIMEOUT", f"{body.client} didn't answer {topic} within {timeout:g} s") from None
        finally:
            self._pending.pop(message["id"], None)

    def _on_response(self, client: BodyConnection, message: dict[str, Any]) -> None:
        entry = self._pending.get(message.get("corr") or "")
        if entry is None:
            log.info("BBP: late or unknown response %s from %s (dropped)", message["corr"], client.client)
            return
        body, future = entry
        if body is not client or future.done():
            log.warning("BBP: response %s from the wrong Body (dropped)", message["corr"])
            return
        try:
            bbp.validate_payload(message)
        except bbp.BBPError as e:
            future.set_exception(bbp.BBPError("E_BAD_PAYLOAD", f"invalid response from {client.client}: {e.message}"))
            return
        if message["topic"] == "error":
            err = message["payload"]["error"]
            future.set_exception(bbp.BBPError(err["code"], err["message"]))
        else:
            future.set_result(message["payload"]["result"])

    def _fail_pending(self, client: BodyConnection) -> None:
        for body, future in list(self._pending.values()):
            if body is client and not future.done():
                future.set_exception(bbp.BBPError("E_INTERNAL", f"{client.client} disconnected before answering"))

    # ── Outgoing ──────────────────────────────────────────────────────────────
    def broadcast_event(self, topic: str, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        message = bbp.envelope("event", topic, payload)
        for client in list(self._clients):
            client.send(message)

    async def _on_voice_state(self, ev: VoiceStateChanged) -> None:
        self.broadcast_event("voice.state", {"state": ev.state, "muted": ev.muted})

    async def _on_transcript(self, ev: TranscriptReady) -> None:
        self.broadcast_event("voice.transcript",
                             {"text": ev.text, "final": ev.final, "confidence": ev.confidence})

    async def _on_reply_chunk(self, ev: ReplyChunk) -> None:
        self.broadcast_event("reply.chunk", {"turn_id": ev.turn_id, "seq": ev.seq, "text": ev.text})

    async def _on_reply_done(self, ev: ReplyDone) -> None:
        self.broadcast_event("reply.done", {"turn_id": ev.turn_id, "interrupted": ev.interrupted})

    async def _on_confirm_requested(self, ev: ConfirmRequested) -> None:
        payload = {"confirm_id": ev.confirm_id, "prompt": ev.prompt, "risk": ev.risk,
                   "timeout_s": ev.timeout_s}
        if ev.detail:
            payload["detail"] = ev.detail
        if ev.action:
            payload["action"] = ev.action
        self.broadcast_event("confirm.request", payload)

    async def _on_confirm_resolved(self, ev: ConfirmResolved) -> None:
        self.broadcast_event("confirm.resolved",
                             {"confirm_id": ev.confirm_id, "approved": ev.approved, "by": ev.by})

    def notify(self, level: str, title: str, body: str = "") -> None:
        """Broadcast a notify event (no buttons in v0.1)."""
        self.broadcast_event("notify", {
            "notify_id": uuid.uuid4().hex, "level": level,
            "title": _cut(title, _NOTIFY_TITLE_MAX) or "JARVIS", "body": _cut(body, _NOTIFY_BODY_MAX),
            "actions": [],
        })

    async def _on_notice(self, ev: Notice) -> None:
        self.notify(ev.level, ev.title, ev.body)

    async def _on_timer_finished(self, ev: TimerFinished) -> None:
        label = " ".join(str(ev.label).split()) or "Timer"
        self.notify("warn", "Timer", f"Your {label} timer is done."
                    if label.lower() != "timer" else "Your timer is done.")

    async def _on_suggestion(self, ev: ActionSuggestion) -> None:
        self.notify("info", "Suggestion", ev.speak_text)

    async def _on_speak_request(self, ev: SpeakRequest) -> None:
        # Goal reminders are exactly PRIORITY_IMPORTANT. Resource alerts are
        # critical and arrive as a Notice instead; normal speech is the reply.
        if ev.priority == PRIORITY_IMPORTANT:
            self.notify("warn", "Reminder", ev.text)

    # ── Connection handling ───────────────────────────────────────────────────
    def _check_path(self, connection: ServerConnection, request):
        if request.path != self._cfg.path:
            return connection.respond(HTTPStatus.NOT_FOUND, "BBP endpoint is " + self._cfg.path + "\n")
        return None

    def _snapshot(self) -> dict[str, Any]:
        try:
            return self._snapshot_fn()
        except Exception as e:
            log.error("BBP snapshot failed (%s) — sending an empty one", e)
            return {
                "voice": {"state": "idle", "muted": False},
                "agent": {"mood": "unknown", "intent": "unknown", "model": "unknown",
                          "load": {"ram_pct": None, "vram_pct": None}},
                "active_goals": [],
                "pending_confirmations": [],
            }

    async def _handle(self, ws: ServerConnection) -> None:
        hello = await self._authenticate(ws)
        if hello is None:
            return
        client = BodyConnection(ws, hello["payload"])
        # Queue welcome BEFORE joining the broadcast set: no event can overtake
        # the snapshot, and none published after the snapshot is lost.
        client.send(bbp.envelope("welcome", "welcome", {
            "session": client.session,
            "brain_version": BRAIN_VERSION,
            "snapshot": self._snapshot(),
        }))
        self._clients.add(client)
        writer = asyncio.create_task(client.writer())
        log.info("BBP: %s %s connected (caps=%s, %d connected)",
                 client.client, client.version, sorted(client.capabilities), len(self._clients))
        try:
            await self._read_loop(client)
        finally:
            self._clients.discard(client)
            self._fail_pending(client)
            writer.cancel()
            for handler in self._disconnect_handlers:
                try:
                    handler(client)
                except Exception as e:
                    log.error("BBP: disconnect handler failed: %s", e)
            log.info("BBP: %s disconnected (%d connected)", client.client, len(self._clients))

    async def _authenticate(self, ws: ServerConnection) -> dict[str, Any] | None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._cfg.heartbeat_timeout_s)
            message = bbp.parse(raw)
            if message["kind"] != "hello":
                raise bbp.BBPError("E_AUTH", "first message must be hello")
            bbp.validate_payload(message)
        except (asyncio.TimeoutError, bbp.BBPError) as e:
            reason = getattr(e, "message", "no hello received")
            log.warning("BBP: rejected connection (%s)", reason)
            await ws.close(CLOSE_AUTH, "E_AUTH")
            return None
        except ConnectionClosed:
            return None
        offered = str(message["payload"]["token"]).encode()
        if not hmac.compare_digest(offered, self._token.encode()):
            log.warning("BBP: rejected connection (bad token)")
            await ws.close(CLOSE_AUTH, "E_AUTH")
            return None
        return message

    async def _read_loop(self, client: BodyConnection) -> None:
        timeout = self._cfg.heartbeat_timeout_s
        while True:
            try:
                raw = await asyncio.wait_for(client.ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("BBP: %s silent for %.0f s — closing", client.client, timeout)
                await client.ws.close(CLOSE_HEARTBEAT, "idle timeout")
                return
            except ConnectionClosed:
                return
            await self._on_message(client, raw)

    async def _on_message(self, client: BodyConnection, raw: str | bytes) -> None:
        try:
            message = bbp.parse(raw)
        except bbp.BBPError as e:
            command_id = _extract_id(raw)
            if command_id:
                client.send(bbp.error_response(command_id, e.code, e.message))
            log.warning("BBP: bad message from %s: %s", client.client, e.message)
            return

        kind = message["kind"]
        if kind == "ping":
            client.send(bbp.envelope("pong", "pong", {}, corr=message["id"]))
            return
        if kind == "response":
            self._on_response(client, message)
            return
        if kind != "command":
            return      # events, stray session messages: dropped (spec: unknown events dropped)

        handler = self._commands.get(message["topic"])
        if handler is None:
            client.send(bbp.error_response(message["id"], "E_UNKNOWN_TOPIC",
                                           f"no handler for command {message['topic']!r}"))
            return
        try:
            bbp.validate_payload(message)
        except bbp.BBPError as e:
            client.send(bbp.error_response(message["id"], e.code, e.message))
            return
        # Run as a task: a long command (input.text streams a whole reply) must
        # never stall this Body's reader, or its pings would go unread → 4408.
        task = asyncio.get_running_loop().create_task(self._run_command(client, handler, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_command(self, client: BodyConnection, handler: CommandHandler,
                           message: dict[str, Any]) -> None:
        """A handler refuses a command by raising BBPError (E_BUSY, E_NOT_ALLOWED,
        …); the Body gets an error response. Accepted commands get no response."""
        try:
            await handler(client, message)
        except bbp.BBPError as e:
            log.info("BBP: %s refused: %s", message["topic"], e)
            client.send(bbp.error_response(message["id"], e.code, e.message))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("BBP: command %s failed: %s", message["topic"], e, exc_info=True)


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _extract_id(raw: str | bytes) -> str | None:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    msg_id = value.get("id") if isinstance(value, dict) else None
    return msg_id if isinstance(msg_id, str) and msg_id else None
