"""
Milestone 4: clicking and typing into apps (BBP amendments 19–21), Brain side.

- ui_policy: the risky-word list shared with the Body (its examples), fail
  closed without it, and text that may not be typed
- describe(): what a click or typing confirmation says
- FastRouter: "click seven", "press the equals button", keys stay keys
- AgentExecutor: no approval for a plain click; high-risk approval for Send;
  set_element_text always asks; the Body finding a risky element makes it ask
  and retry; the confirm_id and the exact action reach the Body; declines,
  unsafe text and a missing Body
- over a real BBPServer: confirm.request carries the action, the desktop
  request carries the matching confirm_id, and both validate against the schemas

No desktop is touched: the Body is a fake.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from action import ui_policy
from action.agent_executor import AgentExecutor
from action.confirmation import describe
from action.desktop_errors import DesktopError
from action.fast_router import FastRouter
from core.config import AgentConfig

EXAMPLES = json.loads(ui_policy.POLICY_FILE.read_text(encoding="utf-8"))["examples"]


# ── Policy ────────────────────────────────────────────────────────────────────

class TestPolicy:
    @pytest.mark.parametrize("label", EXAMPLES["risky"])
    def test_shared_risky_examples(self, label):
        assert ui_policy.is_risky(label), label

    @pytest.mark.parametrize("label", EXAMPLES["safe"])
    def test_shared_safe_examples(self, label):
        assert not ui_policy.is_risky(label), label

    def test_any_of_several_texts(self):
        assert ui_policy.is_risky("→", "SendButton")
        assert not ui_policy.is_risky(None, "", "Seven")

    def test_accented_latin_is_not_mixed_script(self):
        assert not ui_policy.is_risky("Café")

    def test_missing_list_makes_everything_risky(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ui_policy, "POLICY_FILE", tmp_path / "nope.json")
        ui_policy._policy.cache_clear()
        try:
            assert ui_policy.is_risky("Seven")
        finally:
            monkeypatch.undo()
            ui_policy._policy.cache_clear()

    @pytest.mark.parametrize("text, word", [
        ("a‮b", "right-to-left"), ("a​b", "invisible"), ("a\x07b", "control"),
        ("a\rb", "control"), ("x" * 2001, "limit"),
    ])
    def test_unsafe_text(self, text, word):
        assert word in ui_policy.unsafe_text(text)

    def test_tabs_newlines_and_unicode_are_fine(self):
        assert ui_policy.unsafe_text("line 1\n\tline 2 — café ✓") is None


# ── describe ──────────────────────────────────────────────────────────────────

class TestDescribe:
    def test_click(self):
        r = describe("click_element", {"app": "Discord", "element": {"name": "Send", "control_type": "Button"}})
        assert r.prompt == "About to press 'Send' in Discord. Shall I proceed, Sir?"
        assert (r.risk, r.detail) == ("high", "Discord › Button 'Send'")

    def test_click_explains_why_the_body_asked(self):
        r = describe("click_element", {"app": "Discord", "element": {"name": "→"},
                                       "why": "pressing '→' needs your approval: you weren't asked"})
        assert r.detail.startswith("Discord › '→' (pressing") and r.risk == "high"

    def test_typing_names_the_field_and_size(self):
        r = describe("set_element_text", {"app": "Notepad", "element": {"name": "Search", "control_type": "Edit"},
                                          "text": "hello"})
        assert r.prompt == "About to type into 'Search' in Notepad. Shall I proceed, Sir?"
        assert (r.risk, r.detail) == ("low", "Notepad › Edit 'Search' ← 5 characters")

    def test_untrusted_names_are_cleaned(self):
        r = describe("click_element", {"app": "Discord", "element": {"name": "Se‮nd"}})
        assert "‮" not in r.prompt + r.detail


# ── FastRouter ────────────────────────────────────────────────────────────────

class TestRouter:
    @pytest.mark.parametrize("text, args", [
        ("click seven", {"name": "seven"}),
        ("press the equals button", {"name": "equals", "control_type": "Button"}),
        ("Hey Jarvis, click on the Bold tab", {"name": "bold", "control_type": "TabItem"}),
        ("click send in discord", {"app": "discord", "name": "send"}),
        ("tap OK.", {"name": "ok"}),
        ("click save in place", {"name": "save in place"}),
    ])
    def test_clicks(self, text, args):
        route = FastRouter().route(text)
        assert (route.tool, route.args) == ("click_element", args)

    @pytest.mark.parametrize("text", [
        "press enter", "press ctrl c", "press alt+tab", "click at 100, 200", "hit the up arrow",
        "click here", "press play", "press f5",
    ])
    def test_keys_and_coordinates_stay_elsewhere(self, text):
        route = FastRouter().route(text)
        assert route is None or getattr(route, "tool", None) != "click_element"

    def test_compound(self):
        routes = FastRouter().route("click one and then click plus")
        assert [r.args["name"] for r in routes] == ["one", "plus"]


# ── AgentExecutor ─────────────────────────────────────────────────────────────

class FakeRemote:
    """Stands in for RemoteDesktop; `answer(topic, payload)` returns a result or raises."""

    def __init__(self, answer=None, front="calculator", capable=True):
        self.calls = []
        self._answer = answer or (lambda topic, payload: _ok(topic, payload))
        self._front = front
        self._capable = capable

    def available(self, topic):
        return self._capable

    async def call(self, topic, payload):
        self.calls.append((topic, json.loads(json.dumps(payload))))
        if topic == "desktop.uia.read":
            return {"app": self._front, "title": "x", "root": {"control_type": "Window"},
                    "node_count": 1, "truncated": False}
        return self._answer(topic, payload)


def _ok(topic, payload):
    el = payload["element"]
    if topic == "desktop.uia.invoke":
        return {"app": payload["app"], "name": el.get("name", ""), "control_type": "Button",
                "automation_id": "", "pattern": "invoke"}
    return {"app": payload["app"], "name": el.get("name", ""), "control_type": "Edit",
            "automation_id": "", "length": len(payload["text"])}


class Asker:
    """The V3 confirmation callback: records requests, answers as told."""

    def __init__(self, approved=True):
        self.requests = []
        self.approved = approved

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace(approved=self.approved, by="body", confirm_id=f"c{len(self.requests)}")


def _executor(tmp_path, remote, asker=None):
    ex = AgentExecutor(AgentConfig(confirmation_required=True, sandbox_dir=str(tmp_path)),
                       confirm_request_callback=asker)
    ex.attach_remote_desktop(remote, fallback=False)
    return ex


async def _run(ex, tool, **args):
    [result] = await ex.execute([{"tool": tool, "args": args}])
    return result


class TestExecutor:
    async def test_plain_click_needs_no_approval(self, tmp_path):
        remote, asker = FakeRemote(), Asker()
        result = await _run(_executor(tmp_path, remote, asker), "click_element", app="calc", name="Seven")
        assert result == {"tool": "click_element", "status": "ok", "result": "Pressed 'Seven' in Calculator, Sir."}
        assert remote.calls == [("desktop.uia.invoke", {"app": "calculator", "element": {"name": "Seven"}})]
        assert asker.requests == []

    async def test_app_defaults_to_the_one_in_front(self, tmp_path):
        remote = FakeRemote(front="notepad")
        await _run(_executor(tmp_path, remote, Asker()), "click_element", name="Bold", control_type="Button")
        assert [c[0] for c in remote.calls] == ["desktop.uia.read", "desktop.uia.invoke"]
        assert remote.calls[1][1] == {"app": "notepad", "element": {"name": "Bold", "control_type": "Button"}}

    async def test_risky_click_asks_high_risk_and_sends_the_approval(self, tmp_path):
        remote, asker = FakeRemote(), Asker()
        result = await _run(_executor(tmp_path, remote, asker), "click_element", app="notepad", name="Delete all")
        assert result["status"] == "ok"
        [req] = asker.requests
        assert (req.tool, req.risk) == ("click_element", "high")
        assert req.action == {"topic": "desktop.uia.invoke", "app": "notepad", "element": {"name": "Delete all"}}
        assert remote.calls == [("desktop.uia.invoke",
                                 {"app": "notepad", "element": {"name": "Delete all"}, "confirm_id": "c1"})]

    async def test_declined_click_never_reaches_the_body(self, tmp_path):
        remote = FakeRemote()
        result = await _run(_executor(tmp_path, remote, Asker(approved=False)), "click_element",
                            app="notepad", name="Send")
        assert (result["status"], result["code"], result["reason"]) == ("denied", "E_DENIED", "you declined")
        assert remote.calls == []

    async def test_body_finding_a_risky_element_makes_it_ask_and_retry_once(self, tmp_path):
        def answer(topic, payload):
            if "confirm_id" not in payload:
                raise DesktopError("E_DENIED", "pressing 'SendButton' needs your approval: you weren't asked")
            return _ok(topic, payload)
        remote, asker = FakeRemote(answer), Asker()
        result = await _run(_executor(tmp_path, remote, asker), "click_element", app="notepad", name="→")
        assert result["status"] == "ok"
        assert [c[1].get("confirm_id") for c in remote.calls] == [None, "c1"]
        assert "SendButton" in asker.requests[0].detail and asker.requests[0].risk == "high"

    async def test_body_refusing_an_approved_request_is_not_retried(self, tmp_path):
        def answer(topic, payload):
            raise DesktopError("E_DENIED", "pressing 'Send' needs your approval: that approval has expired")
        remote, asker = FakeRemote(answer), Asker()
        result = await _run(_executor(tmp_path, remote, asker), "click_element", app="notepad", name="Send")
        assert (result["status"], len(remote.calls), len(asker.requests)) == ("denied", 1, 1)
        assert "expired" in result["reason"]

    async def test_typing_always_asks_with_the_exact_text(self, tmp_path):
        remote, asker = FakeRemote(), Asker()
        result = await _run(_executor(tmp_path, remote, asker), "set_element_text",
                            app="notepad", name="Search", control_type="Edit", text="hello\nworld")
        assert result["result"] == "Typed 11 characters into 'Search' in Notepad, Sir."
        [req] = asker.requests
        assert req.risk == "low" and req.action["text"] == "hello\nworld"
        assert remote.calls[0][1] == {"app": "notepad", "element": {"name": "Search", "control_type": "Edit"},
                                      "text": "hello\nworld", "confirm_id": "c1"}

    async def test_unsafe_text_is_refused_before_asking(self, tmp_path):
        remote, asker = FakeRemote(), Asker()
        result = await _run(_executor(tmp_path, remote, asker), "set_element_text",
                            app="notepad", name="Search", text="pay‮yap")
        assert result["status"] == "error" and "right-to-left" in result["error"]
        assert (asker.requests, remote.calls) == ([], [])

    async def test_without_the_v3_broker_risky_actions_are_denied(self, tmp_path):
        remote = FakeRemote()
        result = await _run(_executor(tmp_path, remote, None), "set_element_text",
                            app="notepad", name="Search", text="x")
        assert result["status"] == "denied" and remote.calls == []

    async def test_a_bare_true_has_no_confirm_id_so_it_is_denied(self, tmp_path):
        async def yes(request):
            return True
        remote = FakeRemote()
        result = await _run(_executor(tmp_path, remote, yes), "click_element", app="notepad", name="Send")
        assert result["status"] == "denied" and remote.calls == []

    async def test_needs_a_target_and_an_allowed_app(self, tmp_path):
        ex = _executor(tmp_path, FakeRemote(), Asker())
        assert (await _run(ex, "click_element", control_type="Button"))["status"] == "error"
        result = await _run(ex, "click_element", app="bankapp", name="OK")
        assert (result["status"], result["code"]) == ("denied", "E_NOT_ALLOWED")

    async def test_no_body_means_unavailable(self, tmp_path):
        result = await _run(_executor(tmp_path, FakeRemote(capable=False), Asker()),
                            "click_element", app="calc", name="Seven")
        assert result["status"] == "error" and "Body" in result["error"]

    async def test_screen_tools_off_blocks_them(self, tmp_path):
        ex = AgentExecutor(AgentConfig(screen_enabled=False, sandbox_dir=str(tmp_path)))
        assert (await _run(ex, "click_element", name="Seven"))["status"] == "denied"
        assert (await _run(ex, "set_element_text", name="Search", text="x"))["status"] == "denied"


# ── Over a real BBPServer ─────────────────────────────────────────────────────

async def test_over_bbp_the_body_gets_the_action_and_the_matching_confirm_id(tmp_path):
    pytest.importorskip("websockets")
    pytest.importorskip("jsonschema")
    from websockets.asyncio.client import connect

    from core.config import BBPConfig
    from core_v3.adapters import bbp_protocol as bbp
    from core_v3.adapters.bbp_commands import BBPCommands
    from core_v3.adapters.bbp_server import BBPServer
    from core_v3.adapters.remote_desktop import RemoteDesktop
    from core_v3.confirmations import ConfirmationBroker
    from core_v3.event_bus import EventBus

    bus = EventBus()
    broker = ConfirmationBroker(bus, timeout_s=5)
    cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "t"))
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

    async def ask(request):
        return await broker.ask(request)

    ex = AgentExecutor(AgentConfig(confirmation_required=True, sandbox_dir=str(tmp_path)),
                       confirm_request_callback=ask)
    ex.attach_remote_desktop(RemoteDesktop(server), fallback=False)

    seen = {}          # confirm_id → action, as a Body records them
    approved = set()
    try:
        async with connect(f"ws://127.0.0.1:{server.port}/bbp") as ws:
            await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
                "client": "pytest", "version": "0", "token": token, "capabilities": ["hud", "uia"]})))
            assert json.loads(await ws.recv())["kind"] == "welcome"

            async def body():
                async for raw in ws:
                    msg = json.loads(raw)
                    bbp.validate(msg)                       # every message matches the schemas
                    if msg["topic"] == "confirm.request":
                        seen[msg["payload"]["confirm_id"]] = msg["payload"].get("action")
                        await ws.send(bbp.dumps(bbp.envelope("command", "confirm.response", {
                            "confirm_id": msg["payload"]["confirm_id"], "approved": True})))
                    elif msg["topic"] == "confirm.resolved" and msg["payload"]["approved"]:
                        approved.add(msg["payload"]["confirm_id"])
                    elif msg["kind"] == "request" and msg["topic"] == "desktop.uia.set_text":
                        p = msg["payload"]
                        for _ in range(50):                 # the C# Body waits up to 2 s too
                            if p["confirm_id"] in approved:
                                break
                            await asyncio.sleep(0.02)
                        action = {k: p[k] for k in ("app", "element", "text")}
                        assert seen[p["confirm_id"]] == {"topic": "desktop.uia.set_text", **action}
                        result = {"app": p["app"], "name": "Search", "control_type": "Edit", "length": len(p["text"])}
                        reply = bbp.envelope("response", msg["topic"], {"ok": True, "result": result}, corr=msg["id"])
                        bbp.validate(reply)
                        await ws.send(bbp.dumps(reply))

            serving = asyncio.create_task(body())
            await asyncio.sleep(0.1)
            [result] = await asyncio.wait_for(ex.execute([{"tool": "set_element_text", "args": {
                "app": "notepad", "name": "Search", "control_type": "Edit", "text": "hello"}}]), 10)
            assert result["status"] == "ok", result
            assert len(seen) == 1 and approved == set(seen)
            serving.cancel()
    finally:
        await server.stop()


# ── Reliability (amendment 22) ───────────────────────────────────────────────

class TestReliability:
    def test_clicks_and_typing_get_time_for_the_body_to_wait(self):
        from core_v3.adapters.remote_desktop import ACTION_TIMEOUT_S, RemoteDesktop
        remote = RemoteDesktop(server=None, timeout_s=5.0)
        assert remote.timeout_for("desktop.uia.invoke") == ACTION_TIMEOUT_S >= 2 + 2 + 3 + 3
        assert remote.timeout_for("desktop.uia.set_text") == ACTION_TIMEOUT_S
        assert remote.timeout_for("desktop.uia.read") == 5.0
        assert RemoteDesktop(server=None, timeout_s=30.0).timeout_for("desktop.uia.invoke") == 30.0

    async def test_a_timeout_says_it_may_have_happened(self, tmp_path):
        def answer(topic, payload):
            raise DesktopError("E_TIMEOUT", "win-shell didn't answer desktop.uia.invoke within 15 s")
        result = await _run(_executor(tmp_path, FakeRemote(answer), Asker()), "click_element",
                            app="calculator", name="Seven")
        assert (result["status"], result["code"]) == ("error", "E_TIMEOUT")
        assert "can't tell whether Calculator did it" in result["error"]

    async def test_the_reply_names_what_the_body_pressed(self, tmp_path):
        def answer(topic, payload):
            return {**_ok(topic, payload), "name": "Seven"}         # "7" matched loosely
        result = await _run(_executor(tmp_path, FakeRemote(answer), Asker()), "click_element",
                            app="calculator", name="7")
        assert result["result"] == "Pressed 'Seven' in Calculator, Sir."

    @pytest.mark.parametrize("code", ["E_NOT_FOUND", "E_TIMEOUT", "E_BAD_PAYLOAD"])
    def test_findable_failures_are_said_plainly(self, code):
        from core_v3.conversation import _format_fast_result
        reply = _format_fast_result("click_element", {
            "status": "error", "code": code, "error": "no element 'nine' in Calculator; did you mean 'Nine'?"})
        assert reply == "I couldn't do that, Sir: no element 'nine' in Calculator; did you mean 'Nine'?"
        assert _format_fast_result("click_element", {"status": "error", "code": code, "error": "Calculator has no open window"})             == "I couldn't do that, Sir: Calculator has no open window."
