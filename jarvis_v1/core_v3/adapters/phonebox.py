"""
PhoneBoxAdapter — shows what JARVIS is doing on a PhoneBox desk panel.

PhoneBox (https://github.com/ersinkoc/phonebox) is a separate program on this PC
that turns a phone into a desk panel. Its agent accepts webhooks on localhost;
this adapter subscribes to V3 EventBus facts and pushes them there:

  VoiceStateChanged            → push  jarvis        (Listening / Thinking / Speaking / Idle)
                               → push  jarvis_active (on during a turn; for a PhoneBox rule
                                                      that shows the JARVIS page while it lasts)
                               → push  jarvis_muted  (on / off; drives a Mute toggle key)
  TranscriptReady              → push  jarvis_heard  (what JARVIS heard)
  ReplyChunk + ReplyDone       → push  jarvis_reply  (the whole reply once it is done;
                                                      one list item per sentence)
  ActionSuggestion,
  SpeakRequest (important+)    → notify              (goal reminders, resource alerts)
  SystemEvent startup/shutdown → push  jarvis = Idle / Offline, plus the model in use
  timers() snapshot            → push  jarvis_timers (live countdown: every second while a
                                                      timer runs) + notify when one is done
  goals() snapshot             → push  jarvis_goals  (active goals, highest priority first)

Timers and goals have no bus events, so a watcher task reads two snapshot callables
(the executor's active_timers() and AgentState's goals) every second while a timer
runs and every few seconds otherwise, and after each reply; it pushes only changes.

Like BBPServer it is optional and fail-safe by construction: off unless
`phonebox: {enabled: true}`; bus handlers only record the latest value and wake a
single sender task, so a slow or absent PhoneBox never delays the Brain; posting
failures are logged once and retried with the newest values only.

Local only: the URL must be a loopback address. Transcripts and replies never
leave this PC through this adapter (PhoneBox serves them to the paired phone).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any

from core_v3.event_bus import EventBus
from core_v3.events import (
    PRIORITY_IMPORTANT,
    ActionSuggestion,
    ReplyChunk,
    ReplyDone,
    SpeakRequest,
    SystemEvent,
    TranscriptReady,
    VoiceStateChanged,
)

log = logging.getLogger("jarvis.phonebox")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_TEXT = 400                  # characters shown per value; a phone tile is small
MAX_NOTIFICATIONS = 10          # queued while PhoneBox is unreachable; oldest dropped
RETRY_AFTER_S = 10.0            # after a failed post, wait this long before trying again
TIMER_TICK_S = 1.0              # watcher period while a timer runs (the countdown)
IDLE_TICK_S = 5.0               # watcher period otherwise (new goals, new timers)
MAX_ITEMS = 12                  # rows shown for goals / several timers
HIGH_PRIORITY = 8               # goals at or above this show in the warning colour

# VoiceState values (core_v3/voice/state.py) → what the phone shows.
STATE_LABELS = {
    "idle": ("Idle", "ok"),
    "listening": ("Listening", "warn"),
    "processing": ("Thinking", "warn"),
    "speaking": ("Speaking", "ok"),
    "interrupted": ("Interrupted", "warn"),
}

Poster = Callable[[str, str, bytes, float], int]
Snapshot = Callable[[], list[dict]]


def fmt_remaining(seconds: float) -> str:
    """4:05, 1:02:05: whole seconds rounded up, so a new 10-minute timer shows 10:00."""
    total = max(0, math.ceil(seconds - 1e-6))
    hours, rest = divmod(total, 3600)
    mins, secs = divmod(rest, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def _timer_name(label: Any) -> str:
    label = " ".join(str(label).split()) or "Timer"
    return label[:1].upper() + label[1:]


def _short(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def validate_url(url: str) -> str | None:
    """The base URL without a trailing slash, or None when it is not a loopback http URL."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "http" or (parts.hostname or "") not in LOOPBACK_HOSTS:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        return None
    return url.rstrip("/")


def _urllib_post(method: str, url: str, body: bytes, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (loopback only)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class PhoneBoxAdapter:
    """EventBus facts → PhoneBox pushed values and notifications."""

    def __init__(
        self,
        config: Any,                                  # core.config.PhoneBoxConfig
        bus: EventBus,
        current_model: Callable[[], str] | None = None,
        poster: Poster | None = None,
        timers: Snapshot | None = None,
        goals: Snapshot | None = None,
    ) -> None:
        self._cfg = config
        self._bus = bus
        self._model = current_model or (lambda: "")
        self._timers = timers
        self._goals = goals
        self._post = poster or _urllib_post
        self._base = validate_url(config.url)
        self._pending: dict[str, dict] = {}           # key → latest value (coalesced)
        self._notes: deque[dict] = deque(maxlen=MAX_NOTIFICATIONS)
        self._replies: dict[str, list[str]] = {}      # turn_id → sentences so far
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._poke = asyncio.Event()                  # refresh timers/goals now
        self._last: dict[str, dict] = {}              # key → last payload queued
        self._running: dict[Any, dict] = {}           # timer id → last snapshot of it
        self._down_since: float | None = None
        self._active = False
        self._muted = False

        if self._base is None:
            log.warning("PhoneBox disabled: url %r must be http://127.0.0.1:<port>", config.url)
            return
        bus.subscribe(VoiceStateChanged, self._on_voice)
        bus.subscribe(TranscriptReady, self._on_transcript)
        bus.subscribe(ReplyChunk, self._on_chunk)
        bus.subscribe(ReplyDone, self._on_done)
        bus.subscribe(ActionSuggestion, self._on_suggestion)
        bus.subscribe(SpeakRequest, self._on_speak)
        bus.subscribe(SystemEvent, self._on_system)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._base is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._sender(), name="phonebox-sender")
        if self._timers is not None or self._goals is not None:
            self._watch_task = asyncio.create_task(self._watch(), name="phonebox-watch")
        log.info("PhoneBox panel: %s", self._base)

    async def stop(self) -> None:
        """Mark JARVIS offline on the panel (best effort), then stop the sender."""
        if self._task is None:
            return
        for task in (self._task, self._watch_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = self._watch_task = None
        self._set("jarvis", "Offline", level="error", sub="JARVIS is not running")
        self._set("jarvis_active", "off", title="JARVIS active")
        if self._timers is not None:     # timers die with JARVIS; goals are kept
            self._set("jarvis_timers", "None", title="Timers", sub="JARVIS is not running")
        try:
            await asyncio.wait_for(self.flush(), timeout=self._cfg.timeout_s + 0.5)
        except (asyncio.TimeoutError, OSError):
            pass

    # ── bus handlers: record, then wake the sender. Never post from here. ────
    async def _on_voice(self, event: VoiceStateChanged) -> None:
        label, level = STATE_LABELS.get(event.state, (event.state.title(), "ok"))
        if event.muted:
            label, level = f"{label} (mic off)", "warn"
        self._set("jarvis", label, level=level, sub=self._model())
        active = event.state != "idle"
        if active != self._active:
            self._active = active
            self._set("jarvis_active", "on" if active else "off", title="JARVIS active")
        if event.muted != self._muted:
            self._muted = event.muted
            self._set("jarvis_muted", "on" if event.muted else "off", title="Mic muted")

    async def _on_transcript(self, event: TranscriptReady) -> None:
        if self._cfg.show_transcripts and event.text.strip():
            self._set("jarvis_heard", _short(event.text), title="Heard")

    async def _on_chunk(self, event: ReplyChunk) -> None:
        if self._cfg.show_replies:
            parts = self._replies.setdefault(event.turn_id, [])
            if len(parts) < 50:
                parts.append(event.text)

    async def _on_done(self, event: ReplyDone) -> None:
        self._poke.set()        # the turn may have set a timer or added / finished a goal
        parts = self._replies.pop(event.turn_id, [])
        if self._cfg.show_replies and parts:
            # One item per sentence: PhoneBox shows items as rows in a small font,
            # which fits a reply far better than one big single-line value.
            self._set("jarvis_reply", _short(" ".join(parts)), title="JARVIS said",
                      sub="interrupted" if event.interrupted else "",
                      items=[_short(p, 200) for p in parts if p.strip()][:12])

    async def _on_suggestion(self, event: ActionSuggestion) -> None:
        self._notify(event.speak_text, level="info")

    async def _on_speak(self, event: SpeakRequest) -> None:
        # Goal reminders (important) and resource alerts (critical). Normal-priority
        # speech is the conversation itself, which jarvis_reply already shows.
        if event.priority >= PRIORITY_IMPORTANT:
            self._notify(event.text, level="warn" if event.priority > PRIORITY_IMPORTANT else "info")
            # A resource alert follows a model switch: refresh the model shown.
            self._set("jarvis_model", self._model() or "—", title="Model")

    async def _on_system(self, event: SystemEvent) -> None:
        if event.kind == "startup":
            self._set("jarvis", "Idle", level="ok", sub=self._model())
            self._set("jarvis_model", self._model() or "—", title="Model")
            self._set("jarvis_active", "off", title="JARVIS active")
            self._set("jarvis_muted", "off", title="Mic muted")

    # ── timers and goals (polled snapshots) ───────────────────────────────────
    async def _watch(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception as e:                       # a display must never break JARVIS
                log.debug("PhoneBox refresh failed: %s", e)
            period = TIMER_TICK_S if self._running else IDLE_TICK_S
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=period)
            except asyncio.TimeoutError:
                pass
            self._poke.clear()

    def refresh(self) -> None:
        """Read the timer and goal snapshots; queue what changed."""
        if self._timers is not None:
            self._refresh_timers(list(self._timers()))
        if self._goals is not None:
            self._refresh_goals(list(self._goals()))

    def _refresh_timers(self, timers: list[dict]) -> None:
        now = time.monotonic()
        current = {t.get("id", i): t for i, t in enumerate(timers)}
        for tid, old in self._running.items():
            # Gone at (or after) its end time → it went off. Gone early → cancelled.
            if tid not in current and now >= float(old.get("ends_at", 0)) - 1.5:
                name = _timer_name(old.get("label", ""))
                self._notify("Timer is done" if name.lower() == "timer" else f"{name} timer is done",
                             level="warn")
        self._running = current

        def left(t: dict) -> float:
            return float(t.get("remaining", 0))

        if not timers:
            self._set_changed("jarvis_timers", "None", title="Timers",
                              sub="Say: set a timer for 10 minutes")
        elif len(timers) == 1:
            t = timers[0]
            duration = float(t.get("duration") or 0)
            self._set_changed("jarvis_timers", fmt_remaining(left(t)), title="Timer",
                              sub=_timer_name(t.get("label", "")),
                              level="warn" if left(t) <= 10 else "ok",
                              progress=round(left(t) / duration, 3) if duration > 0 else None)
        else:
            items = [{"label": _timer_name(t.get("label", "")), "value": fmt_remaining(left(t)),
                      "level": "warn" if left(t) <= 10 else "ok"}
                     for t in timers[:MAX_ITEMS]]
            self._set_changed("jarvis_timers", items[0]["value"], title=f"Timers ({len(timers)})",
                              items=items)

    def _refresh_goals(self, goals: list[dict]) -> None:
        goals = sorted(goals, key=lambda g: -int(g.get("priority", 0)))
        if not goals:
            self._set_changed("jarvis_goals", "None", title="Goals", sub="Say: remind me to …")
            return
        items = [{"label": _short(str(g.get("name", "")), 80),
                  "value": f"P{int(g.get('priority', 0))}",
                  "level": "warn" if int(g.get("priority", 0)) >= HIGH_PRIORITY else "info"}
                 for g in goals[:MAX_ITEMS]]
        self._set_changed("jarvis_goals", str(len(goals)), title=f"Goals ({len(goals)})", items=items)

    # ── queueing ──────────────────────────────────────────────────────────────
    @staticmethod
    def _payload(value: str, *, level: str = "", title: str = "JARVIS", sub: str = "",
                 items: list | None = None, progress: float | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"value": value, "title": title}
        if level:
            payload["level"] = level
        if sub:
            payload["sub"] = _short(sub, 80)
        if items:
            payload["items"] = items
        if progress is not None:
            payload["progress"] = progress
        return payload

    def _set(self, key: str, value: str, **kw: Any) -> None:
        payload = self._payload(value, **kw)
        self._last[key] = payload
        self._pending[key] = payload
        self._wake.set()

    def _set_changed(self, key: str, value: str, **kw: Any) -> None:
        """_set, unless this is exactly what was last queued for the key."""
        if self._last.get(key) != self._payload(value, **kw):
            self._set(key, value, **kw)

    def _notify(self, text: str, *, level: str) -> None:
        if not self._cfg.notify or not text.strip():
            return
        note = {"title": "JARVIS", "text": _short(text), "level": level}
        if self._cfg.page:
            note["page"] = self._cfg.page
        self._notes.append(note)
        self._wake.set()

    # ── sending ───────────────────────────────────────────────────────────────
    async def _sender(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not await self.flush():
                await asyncio.sleep(RETRY_AFTER_S)
                self._wake.set()        # try again with whatever is newest by then

    async def flush(self) -> bool:
        """Post everything queued. False if PhoneBox could not be reached (values are kept)."""
        while self._pending or self._notes:
            if self._pending:
                key = next(iter(self._pending))
                payload = self._pending.pop(key)
                path = "/api/push/" + urllib.parse.quote(key, safe="")
            else:
                payload = self._notes.popleft()
                path = "/api/notify"
            ok = await self._send(path, payload)
            if not ok:
                if path == "/api/notify":
                    self._notes.appendleft(payload)
                else:
                    self._pending.setdefault(key, payload)   # unless a newer value arrived
                return False
        return True

    async def _send(self, path: str, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status = await asyncio.to_thread(self._post, "POST", self._base + path, body, self._cfg.timeout_s)
        except (OSError, ValueError) as e:
            if self._down_since is None:
                self._down_since = time.monotonic()
                log.info("PhoneBox not reachable at %s (%s); will keep trying quietly", self._base, e)
            return False
        if self._down_since is not None:
            log.info("PhoneBox reachable again")
            self._down_since = None
        if status >= 400:
            # A rejected value (e.g. too many keys) is dropped, not retried forever.
            log.warning("PhoneBox refused %s: HTTP %s", path, status)
        return True
