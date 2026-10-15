"""
Typed input while Ollama is down: the BBP turn lock (milestone 2) and the
failed-turn fix (PR #2) together.

Typed input — from the Body (input.text) or from stdin in text mode — must come
back with an error reply, never crash the Brain, never hang, and never leave the
turn lock held. Real ConversationEngine with an unreachable LLM, real BBPServer
and a real websocket Body; no network beyond 127.0.0.1, no audio.
"""

import asyncio
import json

import httpx
import pytest

pytest.importorskip("websockets")
pytest.importorskip("jsonschema")

from test_turn_failure import _DownLLM, _engine, _text_brain
from test_voice_body_controls import Harness
from websockets.asyncio.client import connect

from core.config import BBPConfig
from core_v3.adapters import bbp_protocol as bbp
from core_v3.adapters.bbp_commands import BBPCommands
from core_v3.adapters.bbp_server import BBPServer
from core_v3.conversation import LLM_UNREACHABLE_REPLY, TURN_FAILED_REPLY
from core_v3.event_bus import EventBus
from core_v3.events import ReplyChunk, ReplyDone
from core_v3.voice.state import VoiceState

TIMEOUT = 5.0
SNAPSHOT = {
    "voice": {"state": "idle", "muted": False},
    "agent": {"mood": "x", "intent": "x", "model": "m", "load": {"ram_pct": None, "vram_pct": None}},
    "active_goals": [], "pending_confirmations": [],
}


class _SlowDownLLM:
    """Ollama down, but the connect attempt takes a moment (holds the turn)."""
    async def sentence_stream(self, messages, tools=None, system_prompt=None, last_message_out=None):
        await asyncio.sleep(0.4)
        raise httpx.ConnectError("All connection attempts failed")
        yield  # pragma: no cover


class _BrokenConversation:
    async def respond(self, text, followup=False):
        raise RuntimeError("vault exploded")
        yield  # pragma: no cover


@pytest.fixture
async def live(tmp_path):
    """Text-mode Brain wiring: BBPServer + BBPCommands sharing one turn lock."""
    bus = EventBus()
    cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "t"), heartbeat_timeout_s=2.0)
    server = BBPServer(cfg, bus, lambda: SNAPSHOT)
    lock = asyncio.Lock()
    assert await server.start()
    token = (tmp_path / "t").read_text().strip()

    def wire(conversation):
        BBPCommands(orchestrator=None, conversation=conversation, config=cfg,
                    voice_active=lambda: False, turn_lock=lock).register(server)

    async def body():
        ws = await connect(f"ws://127.0.0.1:{server.port}/bbp")
        await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
            "client": "pytest", "version": "0", "token": token, "capabilities": ["hud"]})))
        assert json.loads(await ws.recv())["kind"] == "welcome"
        return ws

    class Live:
        pass
    live = Live()
    live.bus, live.server, live.lock, live.wire, live.body = bus, server, lock, wire, body
    yield live
    await server.stop()


async def _recv(ws):
    message = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
    bbp.validate(message)
    return message


async def _send_text(ws, text):
    cmd = bbp.envelope("command", "input.text", {"text": text})
    await ws.send(bbp.dumps(cmd))
    return cmd["id"]


async def _ping(ws):
    await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))
    assert (await _recv(ws))["kind"] == "pong"


# ── Body input.text, text mode ────────────────────────────────────────────────

class TestBodyTypedInputTextMode:
    async def test_ollama_down_replies_with_the_error_and_keeps_going(self, live, capsys):
        live.wire(_engine(_DownLLM(), bus=live.bus))
        async with await live.body() as ws:
            for question in ("what is the capital of France", "and of Spain"):
                await _send_text(ws, question)
                chunk, done = await _recv(ws), await _recv(ws)
                assert chunk["topic"] == "reply.chunk"
                assert chunk["payload"]["text"] == LLM_UNREACHABLE_REPLY
                assert done["topic"] == "reply.done" and done["payload"]["interrupted"] is False
            await _ping(ws)                                  # Body still connected
        assert not live.lock.locked()                        # lock released after each turn
        assert capsys.readouterr().out.count(f"JARVIS: {LLM_UNREACHABLE_REPLY}") == 2

    async def test_other_failures_apologise_without_crashing(self, live, capsys):
        live.wire(_BrokenConversation())
        async with await live.body() as ws:
            await _send_text(ws, "hello")
            await asyncio.sleep(0.2)
            await _ping(ws)
        assert not live.lock.locked()
        assert f"JARVIS: {TURN_FAILED_REPLY}" in capsys.readouterr().out


# ── stdin and Body together: one lock, Ollama down ────────────────────────────

class TestStdinAndBodyShareTheLock:
    async def test_body_is_busy_during_a_failing_stdin_turn_then_served(self, live, monkeypatch, capsys):
        conversation = _engine(_SlowDownLLM(), bus=live.bus)
        live.wire(conversation)
        brain = _text_brain(conversation)
        brain._turn_lock = live.lock                          # the Brain's single lock

        typed_in = asyncio.Event()

        def scripted_input(prompt=""):
            if not typed_in.is_set():
                typed_in.set()
                return "what is the capital of France"
            raise EOFError

        monkeypatch.setattr("builtins.input", scripted_input)
        async with await live.body() as ws:
            stdin_loop = asyncio.create_task(brain._text_loop())
            await asyncio.wait_for(typed_in.wait(), TIMEOUT)
            await asyncio.wait_for(_until(lambda: live.lock.locked()), TIMEOUT)

            # Every reply the Body sees (the stdin turn's arrive after the refusal).
            chunks, dones, errors = [], [], []

            async def read_until(n_dones):
                while len(dones) < n_dones:
                    m = await _recv(ws)
                    {"reply.chunk": chunks, "reply.done": dones, "error": errors}.get(m["topic"], []).append(m)

            # While the stdin turn holds the lock, the Body's input is refused…
            cmd_id = await _send_text(ws, "are you there")
            await read_until(1)                               # …until the stdin turn ends.
            assert [e["corr"] for e in errors] == [cmd_id]
            assert errors[0]["payload"]["error"]["code"] == "E_BUSY"

            # The stdin turn got the fallback; the loop ends on EOF, lock free.
            await asyncio.wait_for(stdin_loop, TIMEOUT)
            assert not live.lock.locked()

            # The Body's next input is served, with the same fallback.
            await _send_text(ws, "are you there")
            await read_until(2)
            assert [c["payload"]["text"] for c in chunks] == [LLM_UNREACHABLE_REPLY] * 2
            assert all(d["payload"]["interrupted"] is False for d in dones)
            assert len(errors) == 1
        out = capsys.readouterr().out
        assert out.count(f"JARVIS: {LLM_UNREACHABLE_REPLY}") == 2


async def _until(cond):
    while not cond():
        await asyncio.sleep(0.005)


# ── Voice mode: typed turn through the voice loop ─────────────────────────────

class TestVoiceModeTypedInput:
    async def test_ollama_down_typed_turn_replies_and_returns_to_idle(self):
        h = Harness()
        conversation = _engine(_DownLLM(), bus=h.bus)
        h.orch._conversation = conversation
        replies = []

        async def on_chunk(ev):
            replies.append(ev.text)

        dones = []

        async def on_done(ev):
            dones.append(ev)

        h.bus.subscribe(ReplyChunk, on_chunk)
        h.bus.subscribe(ReplyDone, on_done)
        async with h:
            for turn in (1, 2):                               # the loop survives: twice
                assert h.orch.request_text("what is the capital of France") is True
                await h.until(lambda turn=turn: len(dones) == turn)
                await h.until(lambda: h.orch.state.is_(VoiceState.IDLE) and h.orch._wake_q is not None)
        assert replies == [LLM_UNREACHABLE_REPLY, LLM_UNREACHABLE_REPLY]
        assert all(d.interrupted is False for d in dones)
        assert h.player.enqueued == []                        # typed: shown, not spoken

