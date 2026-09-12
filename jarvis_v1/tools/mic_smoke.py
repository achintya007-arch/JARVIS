"""
Real-microphone smoke test for the rebuilt V3 voice layer.

Runs the actual Brain + VoiceOrchestrator on your mic and prints, for every
turn, the live latencies that matter:

    wake → first audio       (say "hey jarvis …")
    end-of-speech → first audio
    (barge-in: interrupt while Echo is speaking — playback should stop at once)

    python -m tools.mic_smoke      # or:  .\run.ps1 -MicSmoke

Ctrl+C to exit. Needs Ollama running + the wake/STT/TTS models available.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from core.config import Config
from core_v3.brain import Brain
from core_v3.voice.state import VoiceState

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")


def _instrument(orch) -> None:
    t = {"wake": 0.0, "endpoint": 0.0, "announced": False}

    def on_change(old: VoiceState, new: VoiceState) -> None:
        now = time.perf_counter()
        if new is VoiceState.LISTENING and old is VoiceState.IDLE:
            t["wake"] = now
            t["announced"] = False
        elif new is VoiceState.PROCESSING:
            t["endpoint"] = now
    orch.state._on_change = on_change

    orig_enqueue = orch._player.enqueue

    def timed_enqueue(fs, samples):
        if not t["announced"]:
            t["announced"] = True
            now = time.perf_counter()
            wake_ms = (now - t["wake"]) * 1000 if t["wake"] else 0
            ep_ms = (now - t["endpoint"]) * 1000 if t["endpoint"] else 0
            print(f"\n  ⏱  wake→first-audio: {wake_ms:6.0f} ms   "
                  f"end-of-speech→first-audio: {ep_ms:6.0f} ms\n", flush=True)
        orig_enqueue(fs, samples)
    orch._player.enqueue = timed_enqueue


async def main() -> None:
    config = Config.load(Path("config.yaml"))
    brain = Brain(config)
    if brain._orchestrator is not None:
        _instrument(brain._orchestrator)
    print("\n=== Mic smoke test ===\nSay \"hey jarvis\", then a command. "
          "Try interrupting mid-reply. Ctrl+C to exit.\n", flush=True)
    await brain.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
