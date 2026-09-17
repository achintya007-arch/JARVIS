"""
Wake-word diagnostic — shows the live openWakeWord score as you speak.

Run it, then say "hey jarvis" a few times. It prints the peak detection score
each second and flags when the score crosses the configured threshold, so you
can tell whether the wake word is being detected at all (and pick a good
threshold for your voice/mic).

    python -m tools.wake_check      # or:  .\run.ps1 -WakeCheck

Ctrl+C to exit.
"""

from __future__ import annotations

import asyncio
import time

from core.config import Config
from perception.microphone import MicrophoneInput
from perception.wakeword import WakeWordDetector


async def main() -> None:
    cfg = Config()
    wake = WakeWordDetector(cfg.wake)
    if not wake.available:
        print("openWakeWord is NOT available — wake detection is disabled.")
        print("Install it in this interpreter:  pip install -e \".[wakeword]\"")
        return

    mic = MicrophoneInput(frame_ms=cfg.voice.frame_ms)
    await mic.start()
    q = mic.subscribe()
    wake.reset()

    print(f"\n=== Wake check ===  model={wake.model_name}  threshold={wake.threshold}")
    print('Say "hey jarvis" a few times. Peak score is printed each second.\n', flush=True)

    peak = 0.0
    detections = 0
    t_window = time.perf_counter()
    try:
        while True:
            frame = await q.get()
            score = wake.score(frame)
            peak = max(peak, score)
            if score >= wake.threshold:
                detections += 1
                print(f"  >>> WAKE DETECTED  score={score:.2f}", flush=True)
                wake.reset()
                peak = 0.0
            now = time.perf_counter()
            if now - t_window >= 1.0:
                bar = "#" * int(peak * 40)
                flag = "  <- would trigger" if peak >= wake.threshold else ""
                print(f"  peak {peak:.2f} |{bar:<40}|{flag}", flush=True)
                peak = 0.0
                t_window = now
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        mic.unsubscribe(q)
        await mic.stop()
        print(f"\nStopped. Detections at threshold {wake.threshold}: {detections}")
        print("If peak scores stay well BELOW the threshold when you say 'hey jarvis',")
        print("lower wake.threshold in core/config.py; if they're ~0 always, the mic")
        print("or model isn't receiving audio.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
