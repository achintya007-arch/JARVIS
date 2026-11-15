"""
Send one command to a running JARVIS over the Brain–Body Protocol, then exit.
Made for PhoneBox shortcut keys (a phone button runs a program on the PC), but
works from any script or terminal.

    python -m tools.jarvis_send "what's on my calendar today?"   # typed input
    python -m tools.jarvis_send --voice wake                      # start listening (no wake word)
    python -m tools.jarvis_send --voice mute | unmute | cancel
    python -m tools.jarvis_send --reply "time?"                   # also print the reply

Needs JARVIS running with `bbp: {enabled: true}`. Exit codes: 0 accepted,
1 refused by JARVIS (e.g. busy), 2 JARVIS not reachable / no token.
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

# A refusal (E_BUSY…) comes back almost at once; an accepted command gets no response.
REFUSAL_WAIT_S = 1.5


def build_command(text: str | None, voice: str | None) -> dict:
    if voice:
        return bbp.envelope("command", "voice.control", {"action": voice})
    return bbp.envelope("command", "input.text", {"text": text})


async def send(url: str, token: str, command: dict, *, reply: bool, reply_timeout: float) -> int:
    async with connect(url, open_timeout=3) as ws:
        await ws.send(bbp.dumps(bbp.envelope("hello", "hello", {
            "client": "jarvis-send", "version": "0.1.0", "token": token, "capabilities": [],
        })))
        await ws.send(bbp.dumps(command))
        deadline = asyncio.get_running_loop().time() + (reply_timeout if reply else REFUSAL_WAIT_S)
        sentences: list[str] = []
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except (asyncio.TimeoutError, ConnectionClosed):
                break
            msg = json.loads(raw)
            if msg.get("kind") == "response" and msg.get("corr") == command["id"]:
                err = msg["payload"].get("error", {})
                print(f"JARVIS refused: {err.get('code')}: {err.get('message')}", file=sys.stderr)
                return 1
            if reply and msg.get("topic") == "reply.chunk":
                sentences.append(msg["payload"]["text"])
                print(msg["payload"]["text"], flush=True)
            if reply and msg.get("topic") == "reply.done" and sentences:
                break
        if ws.close_code not in (None, 1000, 1001):
            print(f"JARVIS closed the connection: {ws.close_code} {ws.close_reason}", file=sys.stderr)
            return 2
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="what to say to JARVIS (typed input)")
    ap.add_argument("--voice", choices=["wake", "mute", "unmute", "cancel"], help="a voice control instead of text")
    ap.add_argument("--reply", action="store_true", help="wait for the reply and print it")
    ap.add_argument("--reply-timeout", type=float, default=60.0)
    ap.add_argument("--url", default="ws://127.0.0.1:8765/bbp")
    ap.add_argument("--token-path", default="data/bbp_token")
    args = ap.parse_args(argv)

    if bool(args.text and args.text.strip()) == bool(args.voice):
        ap.error("give either some text or --voice ACTION")

    token_path = resolve_token_path(args.token_path)   # relative to jarvis_v1/, from any folder
    if not token_path.is_file():
        print(f"no token at {token_path}: start JARVIS with bbp.enabled first", file=sys.stderr)
        return 2
    token = token_path.read_text(encoding="utf-8").strip()

    try:
        return asyncio.run(send(args.url, token, build_command(args.text, args.voice),
                                reply=args.reply, reply_timeout=args.reply_timeout))
    except (OSError, asyncio.TimeoutError) as e:
        print(f"JARVIS is not reachable at {args.url}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
