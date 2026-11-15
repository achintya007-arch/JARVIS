"""
Milestone 3: confirmations (ConfirmationBroker + BBP confirm.*) and notify.

- describe(): what a gated action asks, with untrusted detail made safe
- the broker race: Body vs. voice vs. console vs. timeout, fail closed
- ConsoleInput: one stdin shared by the chat loop and a confirmation
- AgentExecutor: the request callback, the timer hook
- a real BBPServer + websocket Bodies: confirm.request / confirm.response /
  confirm.resolved, E_NOT_FOUND, E_BUSY for input.text, the welcome snapshot,
  a HUD Body disconnecting, and notify
- text mode: a gated action no longer freezes; typed yes or the HUD approves

No audio, no Ollama, nothing beyond 127.0.0.1.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from action.agent_executor import AgentExecutor
from action.confirmation import ConfirmRequest, clean, describe
from core.config import AgentConfig
from core_v3.confirmations import ConfirmationBroker
from core_v3.console import ConsoleInput
from core_v3.event_bus import EventBus
from core_v3.events import ConfirmRequested, ConfirmResolved

TIMEOUT = 5.0
REQ = ConfirmRequest(tool="run_shell", prompt="About to run the command git status. Shall I proceed, Sir?",
                     detail="git status", risk="high")


class _Bus(EventBus):
    """EventBus that also records what was published."""
    def __init__(self):
        super().__init__()
        self.events = []

    def publish(self, event):
        self.events.append(event)
        super().publish(event)

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


async def _until(predicate, timeout=TIMEOUT):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _never():
    async def channel(request, timeout):
        await asyncio.Event().wait()
    return channel


def _says(answer, after=0.0):
    async def channel(request, timeout):
        await asyncio.sleep(after)
        return answer
    return channel


# ── What a gated action asks ──────────────────────────────────────────────────

class TestDescribe:
    def test_run_shell_is_high_risk_and_names_the_command(self):
        r = describe("run_shell", {"command": "git status"})
        assert r.risk == "high" and r.detail == "git status"
        assert r.prompt == "About to run the command git status. Shall I proceed, Sir?"

    def test_write_file_is_low_risk_with_path_and_size(self):
        r = describe("write_file", {"path": "notes.txt", "content": "hello"})
        assert r.risk == "low" and r.detail == "notes.txt (5 characters)"
        assert "write to the file notes.txt" in r.prompt

    def test_write_file_prompt_names_the_file_detail_has_the_path(self):
        path = r"C:\CSE_Projects\JARVIS\jarvis_v1\data\workspace\a_rather_long_file_name.txt"
        r = describe("write_file", {"path": path, "content": "hi"})
        assert r.prompt == "About to write to the file a_rather_long_file_name.txt. Shall I proceed, Sir?"
        assert r.detail == f"{path} (2 characters)"
        assert "write to the file b.md." in describe("write_file", {"path": "notes/b.md", "content": ""}).prompt

    def test_append_is_named(self):
        assert "append to the file" in describe("write_file", {"path": "a", "content": "", "mode": "a"}).prompt

    def test_close_app_high_close_window_low(self):
        assert describe("close_app", {"name": "Spotify"}).risk == "high"
        r = describe("close_window", {"title": "Untitled - Notepad"})
        assert r.risk == "low" and r.detail == "Untitled - Notepad"

    def test_bidi_and_control_characters_are_removed(self):
        # U+202E would make "rm -rf ~ #txt.exe" display reversed in the dialog.
        r = describe("run_shell", {"command": "echo hi‮\x1b[31m\r\nrm -rf ~⁦"})
        assert "‮" not in r.detail and "\x1b" not in r.detail and "\n" not in r.detail
        assert "⁦" not in r.detail and r.detail.startswith("echo hi")
        assert "rm -rf ~" in r.detail                 # nothing hidden: it is shown, not dropped

    def test_detail_is_capped_and_the_spoken_part_shorter(self):
        r = describe("run_shell", {"command": "x" * 1000})
        assert len(r.detail) == 200 and r.detail.endswith("…")
        assert len(r.prompt) < 120

    def test_clean_collapses_whitespace(self):
        assert clean("  a \t b  ") == "a b"


# ── The broker race ───────────────────────────────────────────────────────────

class TestBroker:
    async def test_body_answer_wins_and_resolves_once(self):
        bus = _Bus()
        broker = ConfirmationBroker(bus, timeout_s=5)
        ask = asyncio.create_task(broker.ask(REQ, {"voice": _never()}))
        await _until(lambda: broker.is_pending)
        cid = bus.of(ConfirmRequested)[0].confirm_id
        assert broker.answer(cid, True, "body")
        assert not broker.answer(cid, False, "body")          # the first answer won
        result = await asyncio.wait_for(ask, TIMEOUT)
        assert (result.approved, result.by) == (True, "body")
        resolved = bus.of(ConfirmResolved)
        assert len(resolved) == 1 and resolved[0].by == "body" and resolved[0].approved
        assert not broker.is_pending

    async def test_request_event_carries_detail_and_risk(self):
        bus = _Bus()
        await ConfirmationBroker(bus, timeout_s=0.05).ask(REQ)
        ev = bus.of(ConfirmRequested)[0]
        assert (ev.risk, ev.detail, ev.timeout_s) == ("high", "git status", 0.05)

    async def test_voice_yes_wins(self):
        result = await ConfirmationBroker(timeout_s=5).ask(REQ, {"voice": _says(True)})
        assert (result.approved, result.by) == (True, "voice")

    async def test_anything_but_yes_denies(self):
        result = await ConfirmationBroker(timeout_s=5).ask(REQ, {"text": _says(False)})
        assert (result.approved, result.by) == (False, "text")

    async def test_no_answer_is_a_timeout_deny(self):
        bus = _Bus()
        result = await asyncio.wait_for(
            ConfirmationBroker(bus, timeout_s=0.1).ask(REQ, {"voice": _never()}), TIMEOUT)
        assert (result.approved, result.by) == (False, "timeout")
        assert bus.of(ConfirmResolved)[0].by == "timeout"

    async def test_silence_leaves_the_body_racing(self):
        broker = ConfirmationBroker(timeout_s=5)
        ask = asyncio.create_task(broker.ask(REQ, {"voice": _says(None)}))
        await asyncio.sleep(0.05)                             # voice heard nothing...
        assert broker.is_pending                              # ...still waiting
        assert broker.answer(broker.pending()[0]["confirm_id"], True, "body")
        assert (await ask).by == "body"

    async def test_a_failing_channel_denies(self):
        async def broken(request, timeout):
            raise RuntimeError("stt exploded")
        result = await ConfirmationBroker(timeout_s=5).ask(REQ, {"voice": broken})
        assert (result.approved, result.by) == (False, "error")

    async def test_cancelled_turn_denies_and_tells_the_bodies(self):
        bus = _Bus()
        broker = ConfirmationBroker(bus, timeout_s=5)
        ask = asyncio.create_task(broker.ask(REQ, {"voice": _never()}))
        await _until(lambda: broker.is_pending)
        ask.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ask
        resolved = bus.of(ConfirmResolved)
        assert len(resolved) == 1 and (resolved[0].approved, resolved[0].by) == (False, "error")
        assert not broker.is_pending

    async def test_losing_channels_are_cancelled(self):
        cancelled = asyncio.Event()

        async def voice(request, timeout):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        broker = ConfirmationBroker(timeout_s=5)
        ask = asyncio.create_task(broker.ask(REQ, {"voice": voice}))
        await _until(lambda: broker.is_pending)
        broker.answer(broker.pending()[0]["confirm_id"], False, "body")
        await ask
        assert cancelled.is_set()

    async def test_unknown_id_is_refused(self):
        assert not ConfirmationBroker().answer("nope", True, "body")

    async def test_pending_snapshot_has_time_left(self):
        now = [100.0]
        broker = ConfirmationBroker(timeout_s=15, clock=lambda: now[0])
        ask = asyncio.create_task(broker.ask(REQ))
        await _until(lambda: broker.is_pending)
        now[0] = 104.0
        [item] = broker.pending()
        assert item["timeout_s"] == 11.0 and item["detail"] == "git status" and item["risk"] == "high"
        broker.deny_all()
        assert (await ask).by == "error"

    async def test_deny_all(self):
        broker = ConfirmationBroker(timeout_s=5)
        ask = asyncio.create_task(broker.ask(REQ))
        await _until(lambda: broker.is_pending)
        assert broker.deny_all("error") == 1
        result = await ask
        assert (result.approved, result.by) == (False, "error")


# ── One console for the chat loop and confirmations ───────────────────────────

class _ScriptedStdin:
    """input() stand-in: each call blocks until the test types a line."""
    def __init__(self):
        import queue
        self.lines = queue.Queue()
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        line = self.lines.get(timeout=TIMEOUT)
        if line is EOFError:
            raise EOFError
        return line

    def type(self, line):
        self.lines.put(line)


class TestConsoleInput:
    async def test_newest_caller_gets_the_line(self):
        stdin = _ScriptedStdin()
        console = ConsoleInput(stdin)
        chat = asyncio.create_task(console.readline("You: "))
        await _until(lambda: stdin.prompts)
        confirm = asyncio.create_task(console.readline("Approve? "))
        await asyncio.sleep(0.05)
        stdin.type("yes")
        assert await asyncio.wait_for(confirm, TIMEOUT) == "yes"
        assert not chat.done()                         # the chat loop didn't also get "yes"
        stdin.type("hello")
        assert await asyncio.wait_for(chat, TIMEOUT) == "hello"

    async def test_a_cancelled_read_hands_its_line_on(self):
        stdin = _ScriptedStdin()
        console = ConsoleInput(stdin)
        confirm = asyncio.create_task(console.readline("Approve? "))
        await _until(lambda: stdin.prompts)
        confirm.cancel()                               # the HUD answered first
        stdin.type("what time is it")
        assert await asyncio.wait_for(console.readline("You: "), TIMEOUT) == "what time is it"

    async def test_eof_reaches_everyone(self):
        stdin = _ScriptedStdin()
        console = ConsoleInput(stdin)
        first = asyncio.create_task(console.readline("You: "))
        await _until(lambda: stdin.prompts)
        stdin.type(EOFError)
        with pytest.raises(EOFError):
            await asyncio.wait_for(first, TIMEOUT)
        with pytest.raises(EOFError):
            await console.readline("You: ")


# ── AgentExecutor hooks ───────────────────────────────────────────────────────

class TestExecutorHooks:
    async def test_request_callback_gets_the_whole_request(self, tmp_path):
        seen = []

        async def ask(request):
            seen.append(request)
            return False
        ex = AgentExecutor(AgentConfig(confirmation_required=True, sandbox_dir=str(tmp_path)),
                           confirm_request_callback=ask)
        target = str(tmp_path / "a.txt")
        [res] = await ex.execute([{"tool": "write_file", "args": {"path": target, "content": "hi"}}])
        assert res["status"] == "denied" and not (tmp_path / "a.txt").exists()
        assert seen[0].tool == "write_file" and seen[0].detail.endswith("a.txt (2 characters)")

    async def test_request_callback_is_preferred(self, tmp_path):
        async def yes_prompt(prompt):
            raise AssertionError("the plain callback must not be used")

        async def yes(request):
            return True
        ex = AgentExecutor(AgentConfig(confirmation_required=True, sandbox_dir=str(tmp_path)),
                           confirm_callback=yes_prompt, confirm_request_callback=yes)
        target = str(tmp_path / "a.txt")
        [res] = await ex.execute([{"tool": "write_file", "args": {"path": target, "content": "hi"}}])
        assert res["status"] == "ok" and (tmp_path / "a.txt").read_text() == "hi"

    async def test_truthy_non_bool_does_not_approve(self, tmp_path):
        async def sloppy(request):
            return "no"                                  # truthy, but not an approval
        ex = AgentExecutor(AgentConfig(confirmation_required=True, sandbox_dir=str(tmp_path)),
                           confirm_request_callback=sloppy)
        target = str(tmp_path / "a.txt")
        [res] = await ex.execute([{"tool": "write_file", "args": {"path": target, "content": "x"}}])
        assert res["status"] == "denied"

    async def test_timer_done_hook(self, tmp_path):
        done = []
        ex = AgentExecutor(AgentConfig(confirmation_required=False, sandbox_dir=str(tmp_path)),
                           timer_done_callback=done.append)
        await ex.execute([{"tool": "set_timer", "args": {"duration_seconds": 1, "label": "tea"}}])
        await _until(lambda: done)
        assert done == ["tea"]
        await ex.shutdown()


# ── Over BBP, with real websocket Bodies ──────────────────────────────────────

websockets = pytest.importorskip("websockets")
pytest.importorskip("jsonschema")

from websockets.asyncio.client import connect  # noqa: E402

from core.config import BBPConfig  # noqa: E402
from core_v3.adapters import bbp_protocol as bbp  # noqa: E402
from core_v3.adapters.bbp_commands import BBPCommands  # noqa: E402
from core_v3.adapters.bbp_server import BBPServer  # noqa: E402
from core_v3.events import (  # noqa: E402
    PRIORITY_IMPORTANT,
    PRIORITY_NORMAL,
    ActionSuggestion,
    Notice,
    SpeakRequest,
    TimerFinished,
)


@pytest.fixture
async def live(tmp_path):
    bus = _Bus()
    broker = ConfirmationBroker(bus, timeout_s=5)
    cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "t"), heartbeat_timeout_s=5.0)
    snapshot = lambda: {  # noqa: E731
        "voice": {"state": "idle", "muted": False},
        "agent": {"mood": "x", "intent": "x", "model": "m", "load": {"ram_pct": None, "vram_pct": None}},
        "active_goals": [], "pending_confirmations": broker.pending(),
    }
    server = BBPServer(cfg, bus, snapshot)
    BBPCommands(orchestrator=None, conversation=None, config=cfg, voice_active=lambda: False,
                turn_lock=asyncio.Lock(), confirmations=broker).register(server)
    assert await server.start()
    token = (tmp_path / "t").read_text().strip()

    async def body(capabilities=("hud",)):
        ws = await connect(f"ws://127.0.0.1:{server.port}/bbp")
        await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
            "client": "pytest", "version": "0", "token": token, "capabilities": list(capabilities)})))
        welcome = json.loads(await ws.recv())
        assert welcome["kind"] == "welcome"
        return ws, welcome

    yield SimpleNamespace(bus=bus, broker=broker, server=server, body=body)
    await server.stop()


async def _recv(ws, topic=None):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
        bbp.validate(message)
        if topic is None or message["topic"] == topic:
            return message


async def _command(ws, topic, payload):
    cmd = bbp.envelope("command", topic, payload)
    await ws.send(bbp.dumps(cmd))
    return cmd["id"]


async def _no_response(ws, wait=0.3):
    try:
        message = json.loads(await asyncio.wait_for(ws.recv(), wait))
    except asyncio.TimeoutError:
        return
    assert message["kind"] != "response", message


class TestOverBBP:
    async def test_dialog_round_trip(self, live):
        ws, _ = await live.body()
        async with ws:
            ask = asyncio.create_task(live.broker.ask(REQ, {"voice": _never()}))
            request = (await _recv(ws, "confirm.request"))["payload"]
            assert (request["risk"], request["detail"], request["timeout_s"]) == ("high", "git status", 5)
            await _command(ws, "confirm.response", {"confirm_id": request["confirm_id"], "approved": True})
            resolved = (await _recv(ws, "confirm.resolved"))["payload"]
            assert resolved == {"confirm_id": request["confirm_id"], "approved": True, "by": "body"}
            result = await asyncio.wait_for(ask, TIMEOUT)
            assert (result.approved, result.by) == (True, "body")

    async def test_every_body_sees_who_answered(self, live):
        ws1, _ = await live.body()
        ws2, _ = await live.body()
        async with ws1, ws2:
            ask = asyncio.create_task(live.broker.ask(REQ))
            cid = (await _recv(ws1, "confirm.request"))["payload"]["confirm_id"]
            assert (await _recv(ws2, "confirm.request"))["payload"]["confirm_id"] == cid
            await _command(ws2, "confirm.response", {"confirm_id": cid, "approved": False})
            for ws in (ws1, ws2):
                assert (await _recv(ws, "confirm.resolved"))["payload"]["by"] == "body"
            assert not (await ask).approved

    async def test_late_and_unknown_answers_get_not_found(self, live):
        ws, _ = await live.body()
        async with ws:
            ask = asyncio.create_task(live.broker.ask(REQ))
            cid = (await _recv(ws, "confirm.request"))["payload"]["confirm_id"]
            await _command(ws, "confirm.response", {"confirm_id": cid, "approved": False})
            await ask
            for bad in (cid, "never-existed"):
                cmd_id = await _command(ws, "confirm.response", {"confirm_id": bad, "approved": True})
                err = await _recv(ws, "error")
                assert err["corr"] == cmd_id and err["payload"]["error"]["code"] == "E_NOT_FOUND"

    async def test_typed_input_cannot_answer(self, live):
        ws, _ = await live.body()
        async with ws:
            ask = asyncio.create_task(live.broker.ask(REQ))
            await _recv(ws, "confirm.request")
            cmd_id = await _command(ws, "input.text", {"text": "yes"})
            err = await _recv(ws, "error")
            assert err["corr"] == cmd_id and err["payload"]["error"]["code"] == "E_BUSY"
            assert live.broker.is_pending                       # "yes" approved nothing
            live.broker.deny_all()
            await ask

    async def test_a_body_joining_mid_confirmation_gets_it_in_the_snapshot(self, live):
        ask = asyncio.create_task(live.broker.ask(REQ))
        await _until(lambda: live.broker.is_pending)
        ws, welcome = await live.body()
        async with ws:
            [pending] = welcome["payload"]["snapshot"]["pending_confirmations"]
            assert pending["detail"] == "git status" and 0 < pending["timeout_s"] <= 5
            await _command(ws, "confirm.response", {"confirm_id": pending["confirm_id"], "approved": True})
            assert (await asyncio.wait_for(ask, TIMEOUT)).by == "body"

    async def test_hud_disconnect_denies(self, live):
        ws, _ = await live.body(("hud",))
        ask = asyncio.create_task(live.broker.ask(REQ))
        await _recv(ws, "confirm.request")
        await ws.close()
        result = await asyncio.wait_for(ask, TIMEOUT)
        assert (result.approved, result.by) == (False, "error")

    async def test_a_phone_button_client_leaving_does_not_deny(self, live):
        # tools/jarvis_send connects with no capabilities and leaves at once.
        ask = asyncio.create_task(live.broker.ask(REQ))
        await _until(lambda: live.broker.is_pending)
        ws, _ = await live.body(())
        await ws.close()
        await asyncio.sleep(0.2)
        assert live.broker.is_pending
        live.broker.deny_all()
        await ask

    async def test_notify_action_is_accepted_quietly(self, live):
        ws, _ = await live.body()
        async with ws:
            await _command(ws, "notify.action", {"notify_id": "n1", "action": "snooze"})
            await _no_response(ws)


class TestNotify:
    @pytest.mark.parametrize("event,level,title", [
        (Notice(level="alert", title="Resource pressure", body="Sir, VRAM pressure."), "alert", "Resource pressure"),
        (TimerFinished(label="tea"), "warn", "Timer"),
        (SpeakRequest(text="Sir, a reminder — you have an active goal: water.", priority=PRIORITY_IMPORTANT),
         "warn", "Reminder"),
        (ActionSuggestion(goal_id="g", speak_text="Sir, shall I open spotify?", action_text="open spotify"),
         "info", "Suggestion"),
    ])
    async def test_sources_become_notify(self, live, event, level, title):
        ws, _ = await live.body()
        async with ws:
            live.bus.publish(event)
            note = (await _recv(ws, "notify"))["payload"]
            assert (note["level"], note["title"], note["actions"]) == (level, title, [])
            assert note["body"]

    async def test_timer_body(self, live):
        ws, _ = await live.body()
        async with ws:
            live.bus.publish(TimerFinished(label="tea"))
            assert (await _recv(ws, "notify"))["payload"]["body"] == "Your tea timer is done."

    async def test_ordinary_speech_is_not_a_notification(self, live):
        ws, _ = await live.body()
        async with ws:
            live.bus.publish(SpeakRequest(text="Opening Chrome, Sir.", priority=PRIORITY_NORMAL))
            await asyncio.sleep(0.2)
            live.bus.publish(Notice(level="info", title="Resources", body="Pressure cleared, Sir."))
            first = await _recv(ws)                            # the first thing it got
            assert first["topic"] == "notify" and first["payload"]["title"] == "Resources"

    async def test_long_text_is_cut_to_the_schema(self, live):
        ws, _ = await live.body()
        async with ws:
            live.bus.publish(Notice(level="info", title="t" * 500, body="b" * 5000))
            note = (await _recv(ws, "notify"))["payload"]
            assert len(note["title"]) == 120 and len(note["body"]) == 1000


# ── Text mode: the freeze is gone ─────────────────────────────────────────────

class _GatedConversation:
    """respond() runs one gated action through the Brain, like a real turn."""
    def __init__(self, brain):
        self.brain = brain

    async def respond(self, text, followup=False):
        if text == "run it":
            ok = await self.brain._confirm_request(describe("run_shell", {"command": "git status"}))
            yield "Done, Sir." if ok else "Understood, I won't."
        else:
            yield f"You said {text}."


def _text_brain(broker, stdin):
    from core_v3.brain import Brain
    brain = Brain.__new__(Brain)
    brain.config = SimpleNamespace(mode="text")
    brain._stopped = False
    brain._turn_lock = asyncio.Lock()
    brain._console = ConsoleInput(stdin)
    brain._confirmations = broker
    brain._orchestrator = None
    brain._conversation = _GatedConversation(brain)
    return brain


class TestTextMode:
    async def test_no_answer_is_denied_within_the_timeout(self, capsys):
        stdin = _ScriptedStdin()
        broker = ConfirmationBroker(timeout_s=0.3)
        brain = _text_brain(broker, stdin)
        stdin.type("run it")
        loop = asyncio.create_task(brain._text_loop())
        await _until(lambda: "Understood, I won't." in capsys.readouterr().out or loop.done(), timeout=3)
        stdin.type(EOFError)
        await asyncio.wait_for(loop, TIMEOUT)

    async def test_typed_yes_approves(self, capsys):
        stdin = _ScriptedStdin()
        brain = _text_brain(ConfirmationBroker(timeout_s=5), stdin)
        loop = asyncio.create_task(brain._text_loop())
        stdin.type("run it")
        await _until(lambda: any(p.startswith("Approve?") for p in stdin.prompts))
        stdin.type("yes")
        stdin.type("thanks")
        stdin.type(EOFError)
        await asyncio.wait_for(loop, TIMEOUT)
        out = capsys.readouterr().out
        assert "> git status" in out and "Done, Sir." in out
        assert "You said thanks." in out and "You said yes." not in out   # "yes" was the answer only

    async def test_the_hud_can_answer_in_text_mode(self, capsys):
        stdin = _ScriptedStdin()
        bus = _Bus()
        broker = ConfirmationBroker(bus, timeout_s=5)
        brain = _text_brain(broker, stdin)
        loop = asyncio.create_task(brain._text_loop())
        stdin.type("run it")
        await _until(lambda: broker.is_pending)
        assert broker.answer(bus.of(ConfirmRequested)[0].confirm_id, True, "body")
        await _until(lambda: "Done, Sir." in capsys.readouterr().out)
        stdin.type("next")                      # the console read started by the prompt...
        await _until(lambda: "You said next." in capsys.readouterr().out)    # ...feeds the chat
        stdin.type(EOFError)
        await asyncio.wait_for(loop, TIMEOUT)
