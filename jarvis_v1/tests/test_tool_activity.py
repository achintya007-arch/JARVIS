"""
BBP milestone 5 (amendment 23): live activity on the HUD.

- action/tool_activity.summarize: templated lines, never a tool's contents.
- AgentExecutor reports every call: started (before any confirmation), then
  finished with ok / denied / error / cancelled.
- RunningTools and AgentStateThrottle (core_v3/adapters/bbp_activity.py).
- Schemas for tool.started, tool.finished, agent.state and welcome.running_tools.
- Over a real BBPServer: the events reach a Body, a reconnecting Body sees what
  is still running, and agent.state follows a model switch.
- Brain._agent_status: the fallback flag.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from action import tool_activity
from action.agent_executor import AgentExecutor
from action.tool_activity import SUMMARY_MAX, summarize
from core.config import AgentConfig
from core_v3.adapters.bbp_activity import AgentStateThrottle, RunningTools

# ── Summaries ────────────────────────────────────────────────────────────────


class TestSummaries:
    @pytest.mark.parametrize("tool,args,line", [
        ("open_app", {"app": "calculator"}, "Opening Calculator"),
        ("open_app", {"name": "vs code"}, "Opening VS Code"),
        ("open_app", {"name": "definitely-not-an-app"}, "Opening an app"),
        ("focus_window", {"title": "chrome"}, "Switching to Chrome"),
        ("click_element", {"name": "Seven", "app": "calculator"}, "Pressing 'Seven' in Calculator"),
        ("click_element", {"automation_id": "num7Button"}, "Pressing 'num7Button'"),
        ("set_element_text", {"name": "Text editor", "app": "notepad", "text": "secret"},
         "Typing into 'Text editor' in Notepad"),
        ("write_file", {"path": "C:\\Users\\me\\data\\notes.txt", "content": "x"}, "Writing notes.txt"),
        ("read_file", {"path": "/home/me/.ssh/id_rsa"}, "Reading id_rsa"),
        ("run_shell", {"command": "git push --force"}, "Running a shell command"),
        ("search", {"query": "my medical results"}, "Searching the web"),
        ("set_clipboard", {"text": "hunter2"}, "Setting the clipboard"),
        ("type_text", {"text": "hunter2"}, "Typing"),
        ("set_volume", {"level": 140}, "Setting the volume to 100%"),
        ("set_volume", {"delta": -10}, "Changing the volume"),
        ("set_timer", {"duration_seconds": 300}, "Setting a 5 min timer"),
        ("set_timer", {"duration_seconds": 45}, "Setting a 45 s timer"),
        ("weather", {"city": "Bangalore"}, "Checking the weather in Bangalore"),
        ("read_screen_ui", {}, "Reading the screen"),
        ("mystery_tool", {}, "Using mystery_tool"),
    ])
    def test_lines(self, tool, args, line):
        assert summarize(tool, args) == line

    def test_window_titles_are_not_repeated(self):
        # A title can name a document; only allowlisted app names are shown.
        assert summarize("focus_window", {"title": "Salary review 2026.xlsx"}) == "Switching to a window"
        assert summarize("close_window", {"title": "Diary - Notepad"}) == "Closing a window"

    @pytest.mark.parametrize("tool,args,secret", [
        ("run_shell", {"command": "del /s C:\\important"}, "important"),
        ("search", {"query": "divorce lawyer"}, "lawyer"),
        ("set_element_text", {"name": "Message", "text": "I quit"}, "quit"),
        ("paste_text", {"text": "password123"}, "password"),
        ("create_note", {"title": "Therapy notes", "body": "..."}, "Therapy"),
        ("search_vault", {"query": "bank pin"}, "pin"),
        ("open_url", {"url": "https://example.com/private?token=abc"}, "token"),
    ])
    def test_contents_never_appear(self, tool, args, secret):
        assert secret.lower() not in summarize(tool, args).lower()

    def test_cleaned_and_capped(self):
        line = summarize("click_element", {"name": "Se\u202even\u200b\n\tbutton", "app": "notepad"})
        assert line == "Pressing 'Se ven button' in Notepad"
        long = summarize("click_element", {"name": "x" * 500, "app": "x" * 500})
        assert len(long) <= SUMMARY_MAX and long.endswith("…")
        assert summarize("", {}) == "Using a tool"
        assert summarize("open_app", None) == "Opening an app"


# ── The executor's reports ───────────────────────────────────────────────────


def _recording(ex):
    events = []
    ex.set_activity_callbacks(
        lambda call_id, tool, summary: events.append(("started", call_id, tool, summary)),
        lambda call_id, tool, summary, status: events.append(("finished", call_id, tool, summary, status)),
    )
    return events


class TestExecutorReports:
    async def test_ok_call_starts_then_finishes(self, tmp_path):
        ex = AgentExecutor(AgentConfig(confirmation_required=False, sandbox_dir=str(tmp_path)))
        events = _recording(ex)
        [res] = await ex.execute([{"tool": "get_time", "args": {}}])
        assert res["status"] == "ok"
        (s, cid, tool, summary), (f, cid2, _, _, status) = events
        assert (s, f, tool, summary, status) == ("started", "finished", "get_time", "Checking the time", "ok")
        assert cid == cid2 and len(cid) == 16

    async def test_each_call_gets_its_own_id(self, tmp_path):
        ex = AgentExecutor(AgentConfig(confirmation_required=False, sandbox_dir=str(tmp_path)))
        events = _recording(ex)
        await ex.execute([{"tool": "get_time"}, {"tool": "get_time"}])
        assert [e[0] for e in events] == ["started", "finished", "started", "finished"]
        assert events[0][1] != events[2][1]

    async def test_started_comes_before_the_confirmation(self, tmp_path):
        order = []

        async def ask(request):
            order.append("asked")
            return False
        ex = AgentExecutor(AgentConfig(confirmation_required=True, sandbox_dir=str(tmp_path)),
                           confirm_request_callback=ask)
        ex.set_activity_callbacks(lambda *a: order.append("started"),
                                  lambda *a: order.append(f"finished:{a[3]}"))
        [res] = await ex.execute([{"tool": "write_file", "args": {"path": str(tmp_path / "a.txt"), "content": "x"}}])
        assert res["status"] == "denied"
        assert order == ["started", "asked", "finished:denied"]

    async def test_blocked_and_failed_calls(self, tmp_path):
        ex = AgentExecutor(AgentConfig(confirmation_required=False, sandbox_dir=str(tmp_path),
                                       shell_enabled=False))
        events = _recording(ex)
        await ex.execute([{"tool": "run_shell", "args": {"command": "dir"}},
                          {"tool": "no_such_tool", "args": {}}])
        assert [e[4] for e in events if e[0] == "finished"] == ["denied", "error"]

    async def test_a_cancelled_turn_still_finishes_the_call(self, tmp_path):
        ex = AgentExecutor(AgentConfig(confirmation_required=False, sandbox_dir=str(tmp_path)))
        events = _recording(ex)
        started = asyncio.Event()

        async def slow(tool, args):
            started.set()
            await asyncio.sleep(30)
        ex._dispatch = slow
        task = asyncio.create_task(ex.execute([{"tool": "search", "args": {"query": "q"}}]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events[-1][0] == "finished" and events[-1][4] == "cancelled"

    async def test_a_broken_display_never_breaks_the_tool(self, tmp_path):
        ex = AgentExecutor(AgentConfig(confirmation_required=False, sandbox_dir=str(tmp_path)))

        def boom(*_):
            raise RuntimeError("HUD exploded")
        ex.set_activity_callbacks(boom, boom)
        [res] = await ex.execute([{"tool": "get_time"}])
        assert res["status"] == "ok"


# ── Running tools and the agent.state throttle ───────────────────────────────


class TestRunningTools:
    def test_started_finished_snapshot(self):
        now = [100.0]
        running = RunningTools(clock=lambda: now[0])
        running.started("a", "open_app", "Opening Calculator")
        now[0] += 1.5
        running.started("b", "search", "Searching the web")
        now[0] += 1.0
        snap = running.snapshot()
        assert [(t["call_id"], t["elapsed_s"]) for t in snap] == [("a", 2.5), ("b", 1.0)]
        running.finished("a")
        running.finished("zzz")                       # unknown: fine
        assert [t["call_id"] for t in running.snapshot()] == ["b"]

    def test_stale_and_capped(self):
        now = [0.0]
        running = RunningTools(clock=lambda: now[0])
        running.started("old", "x", "X")
        now[0] += 301
        assert running.snapshot() == []
        for i in range(40):
            running.started(str(i), "x", "X")
        assert len(running.snapshot()) == 32


def _state(model="qwen", fallback=False, mood="neutral", intent="chat", ram=40.0, vram=50.0):
    return {"mood": mood, "intent": intent, "model": model, "fallback": fallback,
            "load": {"ram_pct": ram, "vram_pct": vram}}


class TestAgentStateThrottle:
    def test_first_state_is_only_remembered(self):
        t = AgentStateThrottle()
        assert not t.check(_state(), 0.0)              # the welcome snapshot has it

    @pytest.mark.parametrize("change", [
        {"model": "qwen-small", "fallback": True}, {"mood": "frustrated"}, {"intent": "task"},
    ])
    def test_identity_changes_go_at_once(self, change):
        t = AgentStateThrottle()
        t.check(_state(), 0.0)
        assert t.check(_state(**change), 0.1)
        assert not t.check(_state(**change), 0.2)      # same again: nothing

    def test_load_needs_five_points_and_five_seconds(self):
        t = AgentStateThrottle()
        t.check(_state(ram=40.0), 0.0)
        assert not t.check(_state(ram=44.9), 10.0)     # moved too little
        assert not t.check(_state(ram=50.0), 3.0)      # too soon
        assert t.check(_state(ram=50.0), 6.0)
        assert not t.check(_state(ram=56.0), 8.0)      # 2 s after the last send
        assert t.check(_state(ram=56.0), 11.5)
        assert t.check(_state(vram=None, ram=56.0), 17.0)    # VRAM reading vanished


# ── Schemas ──────────────────────────────────────────────────────────────────

bbp = pytest.importorskip("core_v3.adapters.bbp_protocol")
pytest.importorskip("jsonschema")


def _code(message):
    try:
        bbp.validate(message)
    except bbp.BBPError as e:
        return e.code
    return None


class TestSchemas:
    @pytest.mark.parametrize("topic,payload", [
        ("tool.started", {"call_id": "c1", "tool": "open_app", "summary": "Opening Calculator"}),
        ("tool.finished", {"call_id": "c1", "ok": True, "summary": "Opening Calculator", "status": "ok"}),
        ("tool.finished", {"call_id": "c1", "ok": False, "summary": "Writing a.txt", "status": "denied"}),
        ("tool.finished", {"call_id": "c1", "ok": False, "summary": "Searching the web"}),   # status optional
        ("agent.state", _state()),
        ("agent.state", {**_state(), "load": {"ram_pct": None, "vram_pct": None}}),
        ("agent.state", {k: v for k, v in _state().items() if k != "fallback"}),            # older Brain
    ])
    def test_valid(self, topic, payload):
        assert _code(bbp.envelope("event", topic, payload)) is None

    @pytest.mark.parametrize("topic,payload", [
        ("tool.started", {"call_id": "c1", "tool": "x", "summary": "x" * 81}),
        ("tool.started", {"call_id": "", "tool": "x", "summary": "x"}),
        ("tool.finished", {"call_id": "c1", "ok": "yes", "summary": "x"}),
        ("tool.finished", {"call_id": "c1", "ok": False, "summary": "x", "status": "exploded"}),
        ("agent.state", {**_state(), "load": {"ram_pct": 140, "vram_pct": None}}),
        ("agent.state", {"mood": "x", "intent": "y", "model": "z"}),
    ])
    def test_invalid(self, topic, payload):
        assert _code(bbp.envelope("event", topic, payload)) == "E_BAD_PAYLOAD"

    def test_welcome_with_running_tools(self):
        snapshot = {
            "voice": {"state": "processing", "muted": False}, "agent": _state(),
            "active_goals": [], "pending_confirmations": [],
            "running_tools": [{"call_id": "c1", "tool": "search", "summary": "Searching the web",
                               "elapsed_s": 3.2}],
        }
        assert _code(bbp.envelope("welcome", "welcome",
                                  {"session": "s", "brain_version": "3.0", "snapshot": snapshot})) is None


# ── Over a real BBPServer ────────────────────────────────────────────────────

websockets = pytest.importorskip("websockets")


class TestOverTheServer:
    @pytest.fixture
    async def server(self, tmp_path, monkeypatch):
        from websockets.asyncio.client import connect

        from core.config import BBPConfig
        from core_v3.adapters import bbp_server
        from core_v3.event_bus import EventBus
        monkeypatch.setattr(bbp_server, "CHECK_INTERVAL_S", 0.05)
        cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "bbp_token"))
        bus = EventBus()
        agent = {"state": _state()}
        snapshot = {"voice": {"state": "idle", "muted": False}, "agent": _state(),
                    "active_goals": [], "pending_confirmations": []}
        srv = bbp_server.BBPServer(cfg, bus, lambda: snapshot, agent_fn=lambda: agent["state"])
        assert await srv.start()
        token = (tmp_path / "bbp_token").read_text(encoding="utf-8").strip()

        async def body():
            ws = await connect(f"ws://127.0.0.1:{srv.port}{cfg.path}")
            await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
                "client": "pytest", "version": "0", "token": token, "capabilities": ["hud"]})))
            welcome = await self._recv(ws)
            return ws, welcome

        yield SimpleNamespace(server=srv, bus=bus, body=body, agent=agent)
        await srv.stop()

    @staticmethod
    async def _recv(ws, timeout=3.0):
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        bbp.validate(message)
        return message

    async def _until_clients(self, srv, n):
        for _ in range(200):
            if len(srv.clients) == n:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("clients")

    async def test_tool_events_and_running_tools(self, server):
        from core_v3.events import ToolFinished, ToolStarted
        ws, _ = await server.body()
        async with ws:
            await self._until_clients(server.server, 1)
            server.bus.publish(ToolStarted(call_id="c1", tool="search", summary="Searching the web"))
            started = await self._recv(ws)
            assert (started["topic"], started["payload"]) == (
                "tool.started", {"call_id": "c1", "tool": "search", "summary": "Searching the web"})

            # A Body that connects now sees it still running.
            late, welcome = await server.body()
            async with late:
                [running] = welcome["payload"]["snapshot"]["running_tools"]
                assert running["call_id"] == "c1" and running["summary"] == "Searching the web"

            server.bus.publish(ToolFinished(call_id="c1", tool="search", summary="Searching the web",
                                            status="denied"))
            finished = await self._recv(ws)
            assert finished["payload"] == {"call_id": "c1", "ok": False, "summary": "Searching the web",
                                           "status": "denied"}
            again, welcome = await server.body()
            async with again:
                assert welcome["payload"]["snapshot"]["running_tools"] == []

    async def test_agent_state_follows_a_model_switch(self, server):
        ws, _ = await server.body()
        async with ws:
            await self._until_clients(server.server, 1)
            await asyncio.sleep(0.15)                       # the first state is only remembered
            server.agent["state"] = _state(model="qwen-small", fallback=True)
            message = await self._recv(ws)
            assert message["topic"] == "agent.state"
            assert message["payload"]["model"] == "qwen-small" and message["payload"]["fallback"] is True


# ── The Brain's agent status ─────────────────────────────────────────────────


class TestAgentStatus:
    def _brain(self, current, model="qwen-7b", fallback_model="qwen-3b"):
        from core_v3.brain import Brain
        brain = Brain.__new__(Brain)
        brain.config = SimpleNamespace(llm=SimpleNamespace(model=model, fallback_model=fallback_model))
        brain._agent_state = SimpleNamespace(snapshot=lambda: SimpleNamespace(
            user_state="focused", conversation_intent="task"))
        brain._resource_monitor = SimpleNamespace(snapshot=lambda: {"ram_pct": 61.234, "vram_pct": None})
        brain._llm = SimpleNamespace(current_model=current)
        return brain

    def test_normal_model(self):
        status = self._brain("qwen-7b")._agent_status()
        assert status == {"mood": "focused", "intent": "task", "model": "qwen-7b", "fallback": False,
                          "load": {"ram_pct": 61.2, "vram_pct": None}}
        assert _code(bbp.envelope("event", "agent.state", status)) is None

    def test_fallback_model(self):
        assert self._brain("qwen-3b")._agent_status()["fallback"] is True

    def test_no_fallback_configured(self):
        assert self._brain("qwen-7b", fallback_model="")._agent_status()["fallback"] is False


def test_statuses_are_the_schema_enum():
    from core_v3.adapters.bbp_protocol import protocol_dir
    schema = json.loads((protocol_dir() / "topics" / "tool.finished.schema.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]["status"]["enum"]) == {
        tool_activity.STATUS_OK, tool_activity.STATUS_DENIED,
        tool_activity.STATUS_ERROR, tool_activity.STATUS_CANCELLED}
