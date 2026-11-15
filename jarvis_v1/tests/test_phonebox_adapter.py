"""
PhoneBoxAdapter: bus facts become PhoneBox webhook posts, without ever delaying
the bus, and an absent PhoneBox costs nothing. A fake poster stands in for HTTP;
one test runs against a real local HTTP server.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.config import Config, PhoneBoxConfig
from core_v3.adapters.phonebox import PhoneBoxAdapter, validate_url
from core_v3.event_bus import EventBus
from core_v3.events import (
    PRIORITY_CRITICAL,
    PRIORITY_IMPORTANT,
    PRIORITY_NORMAL,
    ActionSuggestion,
    ReplyChunk,
    ReplyDone,
    SpeakRequest,
    SystemEvent,
    TranscriptReady,
    VoiceStateChanged,
)


class FakePoster:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, method, url, body, timeout):
        if self.fail:
            raise ConnectionRefusedError("no PhoneBox")
        self.calls.append((url, json.loads(body)))
        return 200

    def pushed(self, key):
        return [b for u, b in self.calls if u.endswith("/api/push/" + key)]

    def notes(self):
        return [b for u, b in self.calls if u.endswith("/api/notify")]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


@pytest.fixture
async def rig():
    bus = EventBus()
    poster = FakePoster()
    cfg = PhoneBoxConfig(enabled=True, url="http://127.0.0.1:8780")
    adapter = PhoneBoxAdapter(cfg, bus, current_model=lambda: "qwen-test", poster=poster)
    await adapter.start()
    yield bus, poster, adapter, cfg
    await adapter.stop()


def test_config_defaults_off_and_not_on_the_bbp_port():
    cfg = Config()
    assert cfg.phonebox.enabled is False
    assert not cfg.phonebox.url.endswith(f":{cfg.bbp.port}")


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8780", True),
    ("http://localhost:8780/", True),
    ("http://192.168.1.5:8780", False),      # never off this machine
    ("https://127.0.0.1:8780", False),
    ("http://127.0.0.1:8780/api", False),
    ("http://evil.example", False),
])
def test_only_loopback_urls(url, ok):
    assert (validate_url(url) is not None) is ok


async def test_non_loopback_url_disables_without_subscribing():
    bus = EventBus()
    poster = FakePoster()
    adapter = PhoneBoxAdapter(PhoneBoxConfig(enabled=True, url="http://10.0.0.2:8780"), bus, poster=poster)
    await adapter.start()
    bus.publish(VoiceStateChanged(state="listening"))
    await settle()
    assert poster.calls == []
    await adapter.stop()


async def test_startup_and_voice_states(rig):
    bus, poster, *_ = rig
    bus.publish(SystemEvent(kind="startup"))
    await settle()
    assert poster.pushed("jarvis")[-1] == {"value": "Idle", "title": "JARVIS", "level": "ok", "sub": "qwen-test"}
    assert poster.pushed("jarvis_model")[-1]["value"] == "qwen-test"

    assert poster.pushed("jarvis_active")[-1]["value"] == "off"

    bus.publish(VoiceStateChanged(state="processing"))
    await settle()
    assert poster.pushed("jarvis")[-1]["value"] == "Thinking"
    assert poster.pushed("jarvis_active")[-1]["value"] == "on"

    bus.publish(VoiceStateChanged(state="speaking"))     # still active: no new jarvis_active post
    await settle()
    assert len(poster.pushed("jarvis_active")) == 2

    bus.publish(VoiceStateChanged(state="idle", muted=True))
    await settle()
    assert poster.pushed("jarvis")[-1]["value"] == "Idle (mic off)"
    assert poster.pushed("jarvis_active")[-1]["value"] == "off"
    assert poster.pushed("jarvis_muted")[-1]["value"] == "on"


async def test_reply_is_pushed_whole_when_done(rig):
    bus, poster, *_ = rig
    bus.publish(TranscriptReady(text="  what time   is it "))
    bus.publish(ReplyChunk(turn_id="t1", seq=0, text="It is ten."))
    bus.publish(ReplyChunk(turn_id="t1", seq=1, text="Anything else?"))
    await settle()
    assert poster.pushed("jarvis_heard")[-1]["value"] == "what time is it"
    assert poster.pushed("jarvis_reply") == []          # nothing until the reply is done
    bus.publish(ReplyDone(turn_id="t1"))
    await settle()
    reply = poster.pushed("jarvis_reply")[-1]
    assert reply["value"] == "It is ten. Anything else?"
    assert reply["items"] == ["It is ten.", "Anything else?"]     # one row per sentence on the phone


async def test_privacy_switches(rig):
    bus, poster, _, cfg = rig
    cfg.show_transcripts = False
    cfg.show_replies = False
    bus.publish(TranscriptReady(text="secret"))
    bus.publish(ReplyChunk(turn_id="t", seq=0, text="secret reply"))
    bus.publish(ReplyDone(turn_id="t"))
    await settle()
    assert poster.pushed("jarvis_heard") == [] and poster.pushed("jarvis_reply") == []


async def test_long_text_is_shortened(rig):
    bus, poster, *_ = rig
    bus.publish(TranscriptReady(text="word " * 500))
    await settle()
    value = poster.pushed("jarvis_heard")[-1]["value"]
    assert len(value) <= 400 and value.endswith("…")


async def test_reminders_and_alerts_notify_but_conversation_does_not(rig):
    bus, poster, _, cfg = rig
    bus.publish(SpeakRequest(text="Just chatting", priority=PRIORITY_NORMAL))
    bus.publish(SpeakRequest(text="Sir, a reminder", priority=PRIORITY_IMPORTANT))
    bus.publish(SpeakRequest(text="VRAM pressure", priority=PRIORITY_CRITICAL))
    bus.publish(ActionSuggestion(goal_id="g", speak_text="Sir, shall I open spotify?", action_text="open spotify"))
    await settle()
    texts = [n["text"] for n in poster.notes()]
    assert "Just chatting" not in texts
    assert {"Sir, a reminder", "VRAM pressure", "Sir, shall I open spotify?"} <= set(texts)
    assert next(n for n in poster.notes() if n["text"] == "VRAM pressure")["level"] == "warn"

    cfg.notify = False
    before = len(poster.notes())
    bus.publish(SpeakRequest(text="quiet", priority=PRIORITY_IMPORTANT))
    await settle()
    assert len(poster.notes()) == before


async def test_notification_can_switch_page(rig):
    bus, poster, _, cfg = rig
    cfg.page = "jarvis"
    bus.publish(SpeakRequest(text="reminder", priority=PRIORITY_IMPORTANT))
    await settle()
    assert poster.notes()[-1]["page"] == "jarvis"


async def test_unreachable_phonebox_keeps_newest_value_and_never_blocks():
    bus = EventBus()
    poster = FakePoster(fail=True)
    adapter = PhoneBoxAdapter(PhoneBoxConfig(enabled=True), bus, poster=poster)
    await adapter.start()
    for state in ("listening", "processing", "speaking"):
        bus.publish(VoiceStateChanged(state=state))
    await settle()                       # handlers returned at once despite the failures
    poster.fail = False
    assert await adapter.flush()
    assert [b["value"] for b in poster.pushed("jarvis")] == ["Speaking"]   # coalesced to the newest
    adapter._task.cancel()


async def test_stop_marks_offline(rig):
    bus, poster, adapter, _ = rig
    bus.publish(VoiceStateChanged(state="listening"))
    await settle()
    await adapter.stop()
    assert poster.pushed("jarvis")[-1]["value"] == "Offline"
    assert poster.pushed("jarvis_active")[-1]["value"] == "off"     # the rule lets go of the page


async def test_real_http_server():
    """End to end over HTTP against a stand-in for the PhoneBox agent."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers["Host"], json.loads(body)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        bus = EventBus()
        adapter = PhoneBoxAdapter(PhoneBoxConfig(enabled=True, url=f"http://127.0.0.1:{server.server_port}"), bus)
        await adapter.start()
        bus.publish(TranscriptReady(text="hello there"))
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
        path, host, body = received[0]
        assert path == "/api/push/jarvis_heard"
        assert host.startswith("127.0.0.1:")          # PhoneBox rejects other Host headers
        assert body == {"value": "hello there", "title": "Heard"}
        await adapter.stop()
    finally:
        server.shutdown()


# ── timers and goals ──────────────────────────────────────────────────────────

class Snap:
    """A snapshot callable whose list the test changes."""

    def __init__(self, items=None):
        self.items = list(items or [])

    def __call__(self):
        return list(self.items)


def timer(tid, label, remaining, duration=600.0):
    import time
    return {"id": tid, "label": label, "duration": duration, "remaining": remaining,
            "ends_at": time.monotonic() + remaining}


@pytest.fixture
async def watched():
    bus = EventBus()
    poster = FakePoster()
    timers, goals = Snap(), Snap()
    adapter = PhoneBoxAdapter(PhoneBoxConfig(enabled=True, url="http://127.0.0.1:8780"), bus,
                              poster=poster, timers=timers, goals=goals)
    await adapter.start()
    yield bus, poster, adapter, timers, goals
    await adapter.stop()


def test_fmt_remaining():
    from core_v3.adapters.phonebox import fmt_remaining
    assert fmt_remaining(600) == "10:00"
    assert fmt_remaining(599.2) == "10:00"           # rounded up: never shows 0:00 early
    assert fmt_remaining(65) == "1:05"
    assert fmt_remaining(3725) == "1:02:05"
    assert fmt_remaining(0) == "0:00"
    assert fmt_remaining(-3) == "0:00"


async def test_watcher_pushes_empty_timers_and_goals_on_start(watched):
    _, poster, *_ = watched
    await settle()
    assert poster.pushed("jarvis_timers")[-1]["value"] == "None"
    assert poster.pushed("jarvis_goals")[-1]["value"] == "None"


async def test_one_timer_counts_down_with_progress(watched):
    _, poster, adapter, timers, _ = watched
    timers.items = [timer(1, "pasta", 272, duration=600)]
    adapter.refresh()
    await settle()
    last = poster.pushed("jarvis_timers")[-1]
    assert last["value"] == "4:32" and last["sub"] == "Pasta" and last["level"] == "ok"
    assert last["progress"] == pytest.approx(272 / 600, abs=0.001)

    timers.items = [timer(1, "pasta", 8, duration=600)]       # last ten seconds: warning colour
    adapter.refresh()
    await settle()
    assert poster.pushed("jarvis_timers")[-1]["level"] == "warn"


async def test_unchanged_values_are_not_reposted(watched):
    _, poster, adapter, timers, goals = watched
    goals.items = [{"name": "study physics", "priority": 8}]
    timers.items = [timer(1, "tea", 100)]
    adapter.refresh()
    await settle()
    counts = len(poster.pushed("jarvis_goals")), len(poster.pushed("jarvis_timers"))
    timers.items = [timer(1, "tea", 100)]
    adapter.refresh()
    adapter.refresh()
    await settle()
    assert (len(poster.pushed("jarvis_goals")), len(poster.pushed("jarvis_timers"))) == counts


async def test_several_timers_become_rows_soonest_first(watched):
    _, poster, adapter, timers, _ = watched
    timers.items = [timer(1, "tea", 30), timer(2, "Timer", 1500)]
    adapter.refresh()
    await settle()
    last = poster.pushed("jarvis_timers")[-1]
    assert last["title"] == "Timers (2)"
    assert [(i["label"], i["value"]) for i in last["items"]] == [("Tea", "0:30"), ("Timer", "25:00")]


async def test_finished_timer_notifies_but_cancelled_one_does_not(watched):
    _, poster, adapter, timers, _ = watched
    timers.items = [timer(1, "pasta", 0.5), timer(2, "laundry", 900)]
    adapter.refresh()
    timers.items = []                          # pasta went off; laundry was cancelled early
    adapter.refresh()
    await settle()
    assert [n["text"] for n in poster.notes()] == ["Pasta timer is done"]
    assert poster.notes()[0]["level"] == "warn"
    assert poster.pushed("jarvis_timers")[-1]["value"] == "None"


async def test_goals_list_highest_priority_first(watched):
    _, poster, adapter, _, goals = watched
    goals.items = [{"name": "drink water", "priority": 5}, {"name": "physics exam", "priority": 8}]
    adapter.refresh()
    await settle()
    last = poster.pushed("jarvis_goals")[-1]
    assert last["title"] == "Goals (2)"
    assert last["items"] == [
        {"label": "physics exam", "value": "P8", "level": "warn"},
        {"label": "drink water", "value": "P5", "level": "info"},
    ]


async def test_reply_done_refreshes_at_once(watched):
    bus, poster, _, timers, _ = watched
    await settle()
    timers.items = [timer(1, "eggs", 300, duration=300)]
    bus.publish(ReplyDone(turn_id="t1"))       # "Timer 'eggs' set for 5 minutes."
    await settle()
    assert poster.pushed("jarvis_timers")[-1]["value"] == "5:00"


async def test_broken_snapshot_never_stops_the_watcher(watched):
    bus, poster, adapter, timers, _ = watched

    def boom():
        raise RuntimeError("state is mid-update")

    adapter._timers = boom
    bus.publish(ReplyDone(turn_id="t1"))
    await settle()
    adapter._timers = timers
    timers.items = [timer(1, "tea", 60, duration=60)]
    bus.publish(ReplyDone(turn_id="t2"))
    await settle()
    assert poster.pushed("jarvis_timers")[-1]["value"] == "1:00"


async def test_stop_clears_timers_but_keeps_goals():
    bus = EventBus()
    poster = FakePoster()
    timers, goals = Snap([timer(1, "tea", 60)]), Snap([{"name": "x", "priority": 6}])
    adapter = PhoneBoxAdapter(PhoneBoxConfig(enabled=True, url="http://127.0.0.1:8780"), bus,
                              poster=poster, timers=timers, goals=goals)
    await adapter.start()
    await settle()
    goal_posts = len(poster.pushed("jarvis_goals"))
    await adapter.stop()
    assert poster.pushed("jarvis_timers")[-1]["value"] == "None"
    assert len(poster.pushed("jarvis_goals")) == goal_posts


async def test_executor_lists_running_timers():
    from action.agent_executor import AgentExecutor
    from core.config import AgentConfig

    ex = AgentExecutor(AgentConfig(confirmation_required=False))
    assert ex.active_timers() == []
    await ex._dispatch("set_timer", {"duration_seconds": 600, "label": "pasta"})
    await ex._set_timer(0, "tea")                  # (the tool turns 0 into 60)
    await settle()                                 # tea went off at once
    running = ex.active_timers()
    assert [t["label"] for t in running] == ["pasta"]
    assert running[0]["duration"] == 600 and 599 < running[0]["remaining"] <= 600
    await ex.shutdown()
    assert ex.active_timers() == []
