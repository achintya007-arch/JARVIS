"""
BBP milestone 2: what the Brain does with Body commands (input.text,
voice.control, ui.hotkey), unit-tested with a fake orchestrator, then end to end
through the real server: a typed input from a Body comes back as reply.* events,
a refused command comes back as an error response, and a long-running command
never blocks the Body's heartbeat.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("websockets")
pytest.importorskip("jsonschema")

from websockets.asyncio.client import connect

from core.config import BBPConfig
from core_v3.adapters import bbp_protocol as bbp
from core_v3.adapters.bbp_commands import BBPCommands
from core_v3.adapters.bbp_server import BBPServer
from core_v3.event_bus import EventBus
from core_v3.events import ReplyChunk, ReplyDone
from core_v3.voice.state import VoiceState, VoiceStateManager

BODY = SimpleNamespace(client="pytest")


class FakeOrch:
    def __init__(self):
        self.muted = False
        self.state = VoiceStateManager()
        self.wake_result = "wake"
        self.text_ok = True
        self.fail_unmute = False
        self.calls = []

    def request_wake(self):
        self.calls.append("wake")
        return self.wake_result

    def request_text(self, text, *, speak=False):
        self.calls.append(("text", text, speak))
        return self.text_ok

    def cancel_turn(self):
        self.calls.append("cancel")
        return True

    async def set_muted(self, muted):
        if not muted and self.fail_unmute:
            raise OSError("no input device")
        self.muted = muted
        self.calls.append(("muted", muted))


class FakeConv:
    """Echoes, and publishes reply.* like ConversationEngine does."""
    def __init__(self, bus=None, delay=0.0):
        self.bus, self.delay, self.texts = bus, delay, []

    async def respond(self, text, followup=False):
        self.texts.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = f"You said {text}, Sir."
        if self.bus:
            self.bus.publish(ReplyChunk(turn_id="t1", seq=0, text=reply))
        yield reply
        if self.bus:
            self.bus.publish(ReplyDone(turn_id="t1"))


def _commands(*, voice=True, speak=False, orch=None, conv=None, lock=None):
    orch = orch or FakeOrch()
    conv = conv or FakeConv()
    cmds = BBPCommands(orchestrator=orch, conversation=conv,
                       config=BBPConfig(enabled=True, speak_text_input=speak),
                       voice_active=lambda: voice, turn_lock=lock or asyncio.Lock())
    return cmds, orch, conv


def _cmd(topic, payload):
    return bbp.envelope("command", topic, payload)


async def _refused(coro):
    with pytest.raises(bbp.BBPError) as e:
        await coro
    return e.value.code


# ── input.text ────────────────────────────────────────────────────────────────

class TestInputText:
    async def test_voice_mode_goes_through_the_voice_loop_unspoken(self):
        cmds, orch, conv = _commands()
        await cmds.input_text(BODY, _cmd("input.text", {"text": "  open chrome "}))
        assert orch.calls == [("text", "open chrome", False)]
        assert conv.texts == []                     # the orchestrator runs it, not us

    async def test_speak_text_input_is_passed_through(self):
        cmds, orch, _ = _commands(speak=True)
        await cmds.input_text(BODY, _cmd("input.text", {"text": "hi"}))
        assert orch.calls == [("text", "hi", True)]

    async def test_voice_busy_is_E_BUSY(self):
        cmds, orch, _ = _commands()
        orch.text_ok = False
        orch.state.transition(VoiceState.LISTENING)
        code = await _refused(cmds.input_text(BODY, _cmd("input.text", {"text": "hi"})))
        assert code == "E_BUSY"

    async def test_text_mode_answers_directly(self, capsys):
        cmds, orch, conv = _commands(voice=False)
        await cmds.input_text(BODY, _cmd("input.text", {"text": "hello"}))
        assert conv.texts == ["hello"] and orch.calls == []
        out = capsys.readouterr().out
        assert "[HUD] You: hello" in out and "JARVIS: You said hello, Sir." in out

    async def test_text_mode_busy_while_another_turn_runs(self):
        lock = asyncio.Lock()
        cmds, _, conv = _commands(voice=False, lock=lock)
        async with lock:                            # e.g. a stdin turn in progress
            code = await _refused(cmds.input_text(BODY, _cmd("input.text", {"text": "hi"})))
        assert code == "E_BUSY" and conv.texts == []


# ── voice.control ─────────────────────────────────────────────────────────────

class TestVoiceControl:
    @pytest.mark.parametrize("action,expected", [
        ("wake", "wake"), ("mute", ("muted", True)), ("cancel", "cancel"),
    ])
    async def test_actions_route_to_the_orchestrator(self, action, expected):
        cmds, orch, _ = _commands()
        await cmds.voice_control(BODY, _cmd("voice.control", {"action": action}))
        assert orch.calls == [expected]

    async def test_unmute(self):
        cmds, orch, _ = _commands()
        orch.muted = True
        await cmds.voice_control(BODY, _cmd("voice.control", {"action": "unmute"}))
        assert orch.calls == [("muted", False)] and not orch.muted

    async def test_unmute_without_a_mic_is_E_NOT_FOUND(self):
        cmds, orch, _ = _commands()
        orch.muted, orch.fail_unmute = True, True
        code = await _refused(cmds.voice_control(BODY, _cmd("voice.control", {"action": "unmute"})))
        assert code == "E_NOT_FOUND" and orch.muted

    async def test_wake_while_muted_is_E_NOT_ALLOWED(self):
        cmds, orch, _ = _commands()
        orch.muted = True
        code = await _refused(cmds.voice_control(BODY, _cmd("voice.control", {"action": "wake"})))
        assert code == "E_NOT_ALLOWED" and "wake" not in orch.calls

    async def test_wake_while_thinking_is_E_BUSY(self):
        cmds, orch, _ = _commands()
        orch.wake_result = "busy"
        code = await _refused(cmds.voice_control(BODY, _cmd("voice.control", {"action": "wake"})))
        assert code == "E_BUSY"

    @pytest.mark.parametrize("action", ["wake", "mute", "unmute", "cancel"])
    async def test_text_mode_is_E_NOT_ALLOWED(self, action):
        cmds, orch, _ = _commands(voice=False)
        code = await _refused(cmds.voice_control(BODY, _cmd("voice.control", {"action": action})))
        assert code == "E_NOT_ALLOWED" and orch.calls == []


# ── ui.hotkey ─────────────────────────────────────────────────────────────────

class TestHotkey:
    async def test_push_to_talk_wakes(self):
        cmds, orch, _ = _commands()
        await cmds.ui_hotkey(BODY, _cmd("ui.hotkey", {"name": "push_to_talk"}))
        assert orch.calls == ["wake"]

    async def test_push_to_talk_while_muted_is_E_NOT_ALLOWED(self):
        cmds, orch, _ = _commands()
        orch.muted = True
        code = await _refused(cmds.ui_hotkey(BODY, _cmd("ui.hotkey", {"name": "push_to_talk"})))
        assert code == "E_NOT_ALLOWED"

    async def test_unknown_hotkey_is_E_NOT_FOUND(self):
        cmds, orch, _ = _commands()
        code = await _refused(cmds.ui_hotkey(BODY, _cmd("ui.hotkey", {"name": "launch_rockets"})))
        assert code == "E_NOT_FOUND" and orch.calls == []

    async def test_text_mode_is_E_NOT_ALLOWED(self):
        cmds, _, _ = _commands(voice=False)
        code = await _refused(cmds.ui_hotkey(BODY, _cmd("ui.hotkey", {"name": "push_to_talk"})))
        assert code == "E_NOT_ALLOWED"


# ── End to end through the real server ────────────────────────────────────────

@pytest.fixture
async def live(tmp_path):
    cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "t"), heartbeat_timeout_s=1.0)
    bus = EventBus()
    server = BBPServer(cfg, bus, lambda: {
        "voice": {"state": "idle", "muted": False},
        "agent": {"mood": "x", "intent": "x", "model": "m", "load": {"ram_pct": None, "vram_pct": None}},
        "active_goals": [], "pending_confirmations": [],
    })
    assert await server.start()
    token = (tmp_path / "t").read_text().strip()

    async def body():
        ws = await connect(f"ws://127.0.0.1:{server.port}/bbp")
        await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
            "client": "pytest", "version": "0", "token": token, "capabilities": ["hud"]})))
        assert json.loads(await ws.recv())["kind"] == "welcome"
        return ws

    yield SimpleNamespace(server=server, bus=bus, body=body)
    await server.stop()


async def _recv(ws, timeout=3.0):
    message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    bbp.validate(message)
    return message


class TestEndToEnd:
    async def test_typed_input_streams_the_reply_back(self, live):
        BBPCommands(orchestrator=FakeOrch(), conversation=FakeConv(live.bus),
                    config=BBPConfig(), voice_active=lambda: False,
                    turn_lock=asyncio.Lock()).register(live.server)
        async with await live.body() as ws:
            await ws.send(bbp.dumps(_cmd("input.text", {"text": "hello"})))
            chunk, done = await _recv(ws), await _recv(ws)
            assert chunk["topic"] == "reply.chunk"
            assert chunk["payload"]["text"] == "You said hello, Sir."
            assert done["topic"] == "reply.done"

    async def test_refused_command_gets_an_error_response(self, live):
        BBPCommands(orchestrator=FakeOrch(), conversation=FakeConv(),
                    config=BBPConfig(), voice_active=lambda: False,
                    turn_lock=asyncio.Lock()).register(live.server)
        async with await live.body() as ws:
            cmd = _cmd("voice.control", {"action": "mute"})
            await ws.send(bbp.dumps(cmd))
            resp = await _recv(ws)
            assert resp["kind"] == "response" and resp["corr"] == cmd["id"]
            assert resp["payload"]["error"]["code"] == "E_NOT_ALLOWED"

    async def test_slow_command_does_not_block_the_heartbeat(self, live):
        # A 2.5 s reply with a 1 s idle timeout: pings must still be answered.
        BBPCommands(orchestrator=FakeOrch(), conversation=FakeConv(live.bus, delay=2.5),
                    config=BBPConfig(), voice_active=lambda: False,
                    turn_lock=asyncio.Lock()).register(live.server)
        async with await live.body() as ws:
            await ws.send(bbp.dumps(_cmd("input.text", {"text": "slow"})))
            for _ in range(4):
                await asyncio.sleep(0.5)
                await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))
                assert (await _recv(ws))["kind"] == "pong"
            assert (await _recv(ws))["topic"] == "reply.chunk"
            assert len(live.server.clients) == 1
