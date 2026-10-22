"""
desktop_bench backend that measures the C# Body: the same JARVIS tool calls as
the Python backend (AgentExecutor), routed AgentExecutor → RemoteDesktop →
BBP → Body, with the Python fallback OFF so every number is the Body's.

The bench plays the Brain: it starts a BBP server (default port 8766, so a
running JARVIS on 8765 is unaffected) and waits for a Body to connect. Point
the Body at it with body.settings.json next to Jarvis.Body.exe:
    {"url": "ws://127.0.0.1:8766/bbp"}
(the token is the normal data/bbp_token, created if missing).
"""

from __future__ import annotations

import asyncio
import os

from action.agent_executor import AgentExecutor
from core.config import AgentConfig, BBPConfig
from core_v3.adapters.bbp_server import BBPServer
from core_v3.adapters.remote_desktop import RemoteDesktop
from core_v3.event_bus import EventBus

_SNAPSHOT = {
    "voice": {"state": "idle", "muted": False},
    "agent": {"mood": "neutral", "intent": "unknown", "model": "desktop-bench",
              "load": {"ram_pct": None, "vram_pct": None}},
    "active_goals": [], "pending_confirmations": [],
}


class BodyBackend:
    name = "body"

    def __init__(self) -> None:
        port = int(os.environ.get("JARVIS_BENCH_BBP_PORT", "8766"))
        self._server = BBPServer(BBPConfig(enabled=True, port=port), EventBus(), lambda: _SNAPSHOT)
        self._ex = AgentExecutor(config=AgentConfig(confirmation_required=False))
        self._ex.attach_remote_desktop(RemoteDesktop(self._server, timeout_s=5.0), fallback=False)

    async def start(self, wait_s: float = 60.0) -> None:
        if not await self._server.start():
            raise SystemExit("could not start the bench BBP server (port in use?)")
        print(f"waiting for a Body on ws://127.0.0.1:{self._server.port}/bbp …", flush=True)
        for _ in range(int(wait_s * 10)):
            if all(self._server.body_with(c) for c in ("apps", "audio", "uia")):
                body = self._server.body_with("apps")
                print(f"Body connected: {body.client} {body.version}", flush=True)
                return
            await asyncio.sleep(0.1)
        raise SystemExit("no Body with apps/audio/uia capabilities connected")

    async def _call(self, tool: str, args: dict):
        [res] = await self._ex.execute([{"tool": tool, "args": args}])
        if res["status"] != "ok":
            raise RuntimeError(f"{res.get('code', '')} {res.get('error') or res.get('reason')}".strip())
        return res["result"]

    async def launch(self, app) -> str:
        return str(await self._call("open_app", app.executor_args))

    async def focus(self, title: str) -> str:
        return str(await self._call("focus_window", {"title": title}))

    async def list_windows(self) -> list[str]:
        out = await self._call("list_windows", {})
        # "title (app)" per line → title
        return [] if out == "No open windows found." else [ln.rsplit(" (", 1)[0] for ln in out.split("\n")]

    async def set_volume(self, level: int) -> str:
        return str(await self._call("set_volume", {"level": level}))

    async def read_screen(self, app: str | None = None) -> dict:
        text = await self._call("read_screen_ui", {"app": app} if app else {})
        inner = text.split("<<<", 1)[1].rsplit(">>>", 1)[0].strip().splitlines()
        return {"ok": len(inner) > 1, "nodes": len(inner),
                "note": "structured UI tree" + (" (truncated)" if "(truncated)" in text.split("<<<")[0] else "")}

    async def close(self) -> None:
        await self._ex.shutdown()
        await self._server.stop()
