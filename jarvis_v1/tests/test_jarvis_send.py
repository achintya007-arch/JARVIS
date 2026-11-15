"""
tools/jarvis_send: the one-shot BBP client behind PhoneBox shortcut keys. Runs
against a real BBPServer on an ephemeral port with stand-in command handlers.
"""

import asyncio

import pytest

pytest.importorskip("websockets")
pytest.importorskip("jsonschema")

from core.config import BBPConfig
from core_v3.adapters.bbp_protocol import BBPError
from core_v3.adapters.bbp_server import BBPServer
from core_v3.event_bus import EventBus
from core_v3.events import ReplyChunk, ReplyDone
from tools import jarvis_send


@pytest.fixture
async def server(tmp_path):
    cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "bbp_token"))
    bus = EventBus()
    srv = BBPServer(cfg, bus, lambda: {"voice": {"state": "idle", "muted": False}, "agent": {},
                                      "active_goals": [], "pending_confirmations": []})
    received = []

    async def input_text(client, message):
        received.append(("text", message["payload"]["text"]))
        if message["payload"]["text"] == "busy":
            raise BBPError("E_BUSY", "JARVIS is speaking; try again in a moment")
        if message["payload"]["text"] == "talk":
            bus.publish(ReplyChunk(turn_id="t", seq=0, text="Hello, Sir."))
            await asyncio.sleep(0.05)
            bus.publish(ReplyDone(turn_id="t"))

    async def voice_control(client, message):
        received.append(("voice", message["payload"]["action"]))

    srv.register_command("input.text", input_text)
    srv.register_command("voice.control", voice_control)
    assert await srv.start()
    token = (tmp_path / "bbp_token").read_text(encoding="utf-8").strip()
    yield f"ws://127.0.0.1:{srv.port}{cfg.path}", token, received
    await srv.stop()


async def test_text_is_delivered(server):
    url, token, received = server
    cmd = jarvis_send.build_command("what's the time", None)
    assert await jarvis_send.send(url, token, cmd, reply=False, reply_timeout=5) == 0
    assert ("text", "what's the time") in received


async def test_voice_control_is_delivered(server):
    url, token, received = server
    assert await jarvis_send.send(url, token, jarvis_send.build_command(None, "mute"), reply=False, reply_timeout=5) == 0
    assert ("voice", "mute") in received


async def test_refusal_is_reported(server, capsys):
    url, token, _ = server
    assert await jarvis_send.send(url, token, jarvis_send.build_command("busy", None), reply=False, reply_timeout=5) == 1
    assert "E_BUSY" in capsys.readouterr().err


async def test_reply_is_printed(server, capsys):
    url, token, _ = server
    assert await jarvis_send.send(url, token, jarvis_send.build_command("talk", None), reply=True, reply_timeout=5) == 0
    assert "Hello, Sir." in capsys.readouterr().out


async def test_bad_token_is_not_success(server):
    url, _, received = server
    assert await jarvis_send.send(url, "wrong", jarvis_send.build_command("hi", None), reply=False, reply_timeout=5) == 2
    assert received == []


def test_needs_text_or_voice():
    with pytest.raises(SystemExit):
        jarvis_send.main([])
    with pytest.raises(SystemExit):
        jarvis_send.main(["hi", "--voice", "mute"])


@pytest.mark.parametrize("url", [
    "ws://192.168.1.5:8765/bbp", "ws://example.com/bbp", "wss://127.0.0.1.example.com/bbp",
    "http://127.0.0.1:8765/bbp",
])
def test_token_never_goes_off_this_pc(url, monkeypatch):
    # Refused before the token is even read.
    monkeypatch.setattr(jarvis_send, "resolve_token_path", lambda p: pytest.fail("token read"))
    with pytest.raises(SystemExit):
        jarvis_send.main(["hi", "--url", url])


@pytest.mark.parametrize("url", ["ws://127.0.0.1:8765/bbp", "ws://localhost:8765/bbp", "ws://[::1]:8765/bbp"])
def test_loopback_urls_are_accepted(url):
    assert jarvis_send.is_loopback_ws(url)
