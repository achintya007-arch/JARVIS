"""
BBPServer integration: a real websockets client against the server on an
ephemeral 127.0.0.1 port. Covers auth (bad token → 4401), the welcome snapshot,
event forwarding to several Bodies, ping/pong, heartbeat timeout (4408),
command errors, wrong path, and a start failure that must not raise.
"""

import asyncio
import json
import os
import stat
from dataclasses import dataclass

import pytest

pytest.importorskip("websockets")
pytest.importorskip("jsonschema")

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from core.config import BBPConfig
from core_v3.adapters import bbp_protocol as bbp
from core_v3.adapters.bbp_server import BBPServer, load_or_create_token
from core_v3.event_bus import EventBus
from core_v3.events import ReplyChunk, ReplyDone, TranscriptReady, VoiceStateChanged

SNAPSHOT = {
    "voice": {"state": "idle", "muted": False},
    "agent": {"mood": "focused", "intent": "task", "model": "test-model",
              "load": {"ram_pct": 50.0, "vram_pct": None}},
    "active_goals": [{"name": "ship BBP", "priority": 7}],
    "pending_confirmations": [],
}


@dataclass
class Harness:
    server: BBPServer
    bus: EventBus
    token: str
    cfg: BBPConfig

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.server.port}{self.cfg.path}"


@pytest.fixture
async def harness(tmp_path):
    cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "bbp_token"),
                    heartbeat_timeout_s=1.0)
    bus = EventBus()
    server = BBPServer(cfg, bus, lambda: SNAPSHOT)
    assert await server.start()
    token = (tmp_path / "bbp_token").read_text(encoding="utf-8").strip()
    yield Harness(server, bus, token, cfg)
    await server.stop()


def _hello(token):
    return bbp.dumps(bbp.envelope("hello", "hello", {
        "client": "pytest", "version": "0.0.1", "token": token, "capabilities": ["hud"],
    }))


async def _recv(ws, timeout=2.0):
    message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    bbp.validate(message)              # everything the Brain sends must match the schemas
    return message


async def _connect(h: Harness):
    ws = await connect(h.url)
    await ws.send(_hello(h.token))
    welcome = await _recv(ws)
    assert welcome["kind"] == "welcome"
    return ws, welcome


async def _until_clients(server, n, timeout=2.0):
    async def wait():
        while len(server.clients) != n:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


# ── Auth ──────────────────────────────────────────────────────────────────────

class TestAuth:
    async def test_bad_token_closes_4401(self, harness):
        async with connect(harness.url) as ws:
            await ws.send(_hello("wrong-token"))
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 2.0)
            assert ws.close_code == 4401
        assert not harness.server.clients

    async def test_first_message_not_hello_closes_4401(self, harness):
        async with connect(harness.url) as ws:
            await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 2.0)
            assert ws.close_code == 4401

    async def test_garbage_hello_closes_4401(self, harness):
        async with connect(harness.url) as ws:
            await ws.send("{not json")
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 2.0)
            assert ws.close_code == 4401

    async def test_silent_client_closes_4401(self, harness):
        async with connect(harness.url) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3.0)
            assert ws.close_code == 4401

    async def test_wrong_path_is_404(self, harness):
        with pytest.raises(InvalidStatus) as e:
            await connect(f"ws://127.0.0.1:{harness.server.port}/nope")
        assert e.value.response.status_code == 404


# ── Session ───────────────────────────────────────────────────────────────────

class TestSession:
    async def test_welcome_carries_snapshot(self, harness):
        ws, welcome = await _connect(harness)
        async with ws:
            p = welcome["payload"]
            assert p["brain_version"] == "3.0" and p["session"]
            assert p["snapshot"] == SNAPSHOT

    async def test_snapshot_failure_sends_empty_snapshot(self, tmp_path):
        def boom():
            raise RuntimeError("snapshot broke")
        cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "t"))
        server = BBPServer(cfg, EventBus(), boom)
        assert await server.start()
        try:
            h = Harness(server, None, (tmp_path / "t").read_text().strip(), cfg)
            ws, welcome = await _connect(h)
            async with ws:
                snap = welcome["payload"]["snapshot"]
                assert snap["active_goals"] == [] and snap["pending_confirmations"] == []
        finally:
            await server.stop()

    async def test_ping_gets_pong_with_corr(self, harness):
        ws, _ = await _connect(harness)
        async with ws:
            ping = bbp.envelope("ping", "ping", {})
            await ws.send(bbp.dumps(ping))
            pong = await _recv(ws)
            assert pong["kind"] == "pong" and pong["corr"] == ping["id"]

    async def test_heartbeat_timeout_closes_4408(self, harness):
        ws, _ = await _connect(harness)
        async with ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3.0)   # we never ping; timeout is 1 s
            assert ws.close_code == 4408
        await _until_clients(harness.server, 0)

    async def test_pings_keep_connection_alive(self, harness):
        ws, _ = await _connect(harness)
        async with ws:
            for _ in range(4):                       # 2 s total, > 1 s timeout
                await asyncio.sleep(0.5)
                await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))
                assert (await _recv(ws))["kind"] == "pong"
            assert len(harness.server.clients) == 1


# ── Events ────────────────────────────────────────────────────────────────────

class TestEvents:
    async def test_bus_events_reach_every_body(self, harness):
        a, _ = await _connect(harness)
        b, _ = await _connect(harness)
        async with a, b:
            await _until_clients(harness.server, 2)
            harness.bus.publish(VoiceStateChanged(state="listening"))
            harness.bus.publish(TranscriptReady(text="open chrome", confidence=0.9))
            harness.bus.publish(ReplyChunk(turn_id="t1", seq=0, text="Opening Chrome, Sir."))
            harness.bus.publish(ReplyDone(turn_id="t1"))
            for ws in (a, b):
                got = [await _recv(ws) for _ in range(4)]
                assert [m["topic"] for m in got] == [
                    "voice.state", "voice.transcript", "reply.chunk", "reply.done"]
                assert got[0]["payload"] == {"state": "listening", "muted": False}
                assert got[1]["payload"]["confidence"] == 0.9
                assert got[2]["payload"]["seq"] == 0
                assert got[3]["payload"] == {"turn_id": "t1", "interrupted": False}

    async def test_disconnect_removes_body(self, harness):
        ws, _ = await _connect(harness)
        await _until_clients(harness.server, 1)
        await ws.close()
        await _until_clients(harness.server, 0)
        harness.bus.publish(VoiceStateChanged(state="idle"))     # no Body: no error
        await asyncio.sleep(0.05)

    async def test_stop_closes_bodies_and_unsubscribes(self, harness):
        ws, _ = await _connect(harness)
        await harness.server.stop()
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 2.0)
        assert ws.close_code == 1000                          # spec: brain shutting down
        assert ws.close_reason == "brain shutting down"
        assert not harness.bus._handlers[VoiceStateChanged]


# ── Commands ──────────────────────────────────────────────────────────────────

class TestCommands:
    async def test_unknown_command_gets_E_UNKNOWN_TOPIC(self, harness):
        ws, _ = await _connect(harness)
        async with ws:
            cmd = bbp.envelope("command", "desktop.launch", {"target": "x"})
            await ws.send(bbp.dumps(cmd))
            resp = await _recv(ws)
            assert resp["kind"] == "response" and resp["topic"] == "error"
            assert resp["corr"] == cmd["id"]
            assert resp["payload"]["error"]["code"] == "E_UNKNOWN_TOPIC"

    async def test_invalid_envelope_gets_E_BAD_PAYLOAD(self, harness):
        ws, _ = await _connect(harness)
        async with ws:
            bad = {"id": "abc123", "kind": "command"}           # missing everything else
            await ws.send(json.dumps(bad))
            resp = await _recv(ws)
            assert resp["corr"] == "abc123"
            assert resp["payload"]["error"]["code"] == "E_BAD_PAYLOAD"
            # connection survives a bad message
            await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))
            assert (await _recv(ws))["kind"] == "pong"

    async def test_registered_command_runs_handler(self, harness):
        seen = []

        async def handler(client, message):
            seen.append(message["topic"])
        # A topic with a schema but no Brain handler yet; register one for the test.
        harness.server.register_command("voice.state", handler)
        ws, _ = await _connect(harness)
        async with ws:
            await ws.send(bbp.dumps(bbp.envelope("command", "voice.state",
                                                 {"state": "idle", "muted": False})))
            await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))
            assert (await _recv(ws))["kind"] == "pong"
            assert seen == ["voice.state"]

    async def test_handler_payload_validated(self, harness):
        async def handler(client, message):
            raise AssertionError("must not run")
        harness.server.register_command("voice.state", handler)
        ws, _ = await _connect(harness)
        async with ws:
            cmd = bbp.envelope("command", "voice.state", {"state": "dancing"})
            await ws.send(bbp.dumps(cmd))
            resp = await _recv(ws)
            assert resp["corr"] == cmd["id"]
            assert resp["payload"]["error"]["code"] == "E_BAD_PAYLOAD"


# ── Failure isolation + token ─────────────────────────────────────────────────

class TestFailureIsolation:
    async def test_port_in_use_returns_false(self, harness, tmp_path):
        cfg = BBPConfig(enabled=True, port=harness.server.port, token_path=str(tmp_path / "t2"))
        bus = EventBus()
        second = BBPServer(cfg, bus, lambda: SNAPSHOT)
        assert await second.start() is False                 # logs, doesn't raise
        assert not bus._handlers[VoiceStateChanged]          # nothing subscribed
        assert second.port == 0
        await second.stop()                                   # safe after failed start

    async def test_missing_schemas_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROTOCOL_DIR", str(tmp_path / "nowhere"))
        bbp._validator.cache_clear()
        try:
            cfg = BBPConfig(enabled=True, port=0, token_path=str(tmp_path / "t"))
            assert await BBPServer(cfg, EventBus(), lambda: SNAPSHOT).start() is False
        finally:
            monkeypatch.delenv("JARVIS_PROTOCOL_DIR")
            bbp._validator.cache_clear()


class TestToken:
    def test_created_once_and_reused(self, tmp_path):
        path = tmp_path / "sub" / "bbp_token"
        first = load_or_create_token(path)
        assert len(first) >= 32
        assert load_or_create_token(path) == first

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_posix_permissions_owner_only(self, tmp_path):
        path = tmp_path / "bbp_token"
        load_or_create_token(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name != "nt", reason="Windows ACL")
    def test_windows_acl_current_user_only(self, tmp_path):
        import subprocess
        path = tmp_path / "bbp_token"
        load_or_create_token(path)
        out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
        # Output: "<path> DOMAIN\user:(F)" then one line per extra ACE, a blank, a summary.
        aces = [out.split("\n")[0].replace(str(path), "").strip()]
        aces += [ln.strip() for ln in out.split("\n")[1:] if ln.strip() and "processed" not in ln]
        assert aces == [f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}:(F)"]
