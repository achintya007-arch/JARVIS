"""
Fake Body for the Brain–Body Protocol: connects to a running Brain, prints the
welcome snapshot and every event, and pings every 5 s. Every message received
is validated against protocol/ — a schema violation is printed loudly.

    python -m tools.bbp_probe                 # ws://127.0.0.1:8765/bbp, token from data/bbp_token
    python -m tools.bbp_probe --raw           # print full JSON envelopes
    python -m tools.bbp_probe --token XYZ     # e.g. to watch a bad-token rejection (4401)

Start the Brain first with `bbp: {enabled: true}` in config.yaml. Ctrl+C to exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from core_v3.adapters import bbp_protocol as bbp
from core_v3.adapters.bbp_server import resolve_token_path

PING_INTERVAL_S = 5.0


def _line(message: dict, raw: bool) -> str:
    if raw:
        return json.dumps(message, indent=2, ensure_ascii=False)
    p = message["payload"]
    topic = message["topic"]
    if topic == "voice.state":
        return f"[voice]  {p['state']}{'  (muted)' if p['muted'] else ''}"
    if topic == "voice.transcript":
        conf = "" if p.get("confidence") is None else f"  ({p['confidence']:.2f})"
        return f"[heard]  {p['text']}{conf}"
    if topic == "reply.chunk":
        return f"[reply]  #{p['seq']} {p['text']}"
    if topic == "reply.done":
        return f"[done]   turn {p['turn_id'][:8]}{'  INTERRUPTED' if p['interrupted'] else ''}"
    if topic == "error":
        return f"[error]  {p['error']['code']}: {p['error']['message']}"
    return f"[{message['kind']}] {topic} {json.dumps(p, ensure_ascii=False)}"


async def _pinger(ws) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        await ws.send(bbp.dumps(bbp.envelope("ping", "ping", {})))


async def probe(url: str, token: str, raw: bool) -> int:
    print(f"connecting to {url} ...")
    async with connect(url) as ws:
        await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
            "client": "bbp-probe", "version": "0.1.0", "token": token, "capabilities": [],
        })))
        pinger = asyncio.create_task(_pinger(ws))
        try:
            async for raw_msg in ws:
                message = json.loads(raw_msg)
                try:
                    bbp.validate(message)
                except bbp.BBPError as e:
                    print(f"!! SCHEMA VIOLATION: {e}\n   {raw_msg}")
                    continue
                kind = message["kind"]
                if kind == "pong":
                    continue
                if kind == "welcome":
                    p = message["payload"]
                    print(f"welcome: session {p['session'][:8]}, brain {p['brain_version']}")
                    print(json.dumps(p["snapshot"], indent=2, ensure_ascii=False))
                    continue
                print(_line(message, raw), flush=True)
        except ConnectionClosed:
            pass
        finally:
            pinger.cancel()
        print(f"closed: {ws.close_code} {ws.close_reason}")
        return 1 if ws.close_code not in (1000, 1001) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="ws://127.0.0.1:8765/bbp")
    ap.add_argument("--token", help="override the token file")
    ap.add_argument("--token-path", default="data/bbp_token")
    ap.add_argument("--raw", action="store_true", help="print full envelopes")
    args = ap.parse_args()

    token = args.token
    if token is None:
        path = resolve_token_path(args.token_path)
        if not path.is_file():
            print(f"no token at {path} — start the Brain with bbp.enabled first", file=sys.stderr)
            return 2
        token = path.read_text(encoding="utf-8").strip()
    try:
        return asyncio.run(probe(args.url, token, args.raw))
    except KeyboardInterrupt:
        return 0
    except OSError as e:
        print(f"cannot connect: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
