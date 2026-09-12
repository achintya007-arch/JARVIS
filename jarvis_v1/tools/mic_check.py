"""
Mic diagnostic — calibrate and see exactly what the STT engine hears.

Run it to tune the voice engine on YOUR mic/room:

    python -m tools.mic_check          # or:  .\run.ps1 -MicCheck

It (1) measures your ambient noise floor and prints the adaptive onset
threshold it implies, then (2) captures a few spoken commands with full
diagnostics — stop reason, duration, and per-segment Whisper confidence — so
you can tell whether speech is being cut off early, missed, or dropped as
low-confidence. Use the numbers to adjust core/config.py STTConfig
(silence_threshold, end_silence_sec, onset_multiplier, avg_logprob_min).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.config import Config
from perception.stt import _CHUNK_SECS, STTEngine, _rms


async def main() -> None:
    cfg = Config.load(Path("config.yaml"))
    cfg.stt.debug_audio = True
    stt = STTEngine(cfg.stt)
    await stt.initialize()

    base = stt.silence_threshold
    mult = stt._onset_mult
    print("\n=== Mic check ===")
    vad = "silero (neural)" if stt._silero is not None else "rms (energy)"
    print(f"config: vad_backend={vad} end_silence={stt._end_silence_chunks * _CHUNK_SECS:.1f}s "
          f"max={stt._max_chunks * _CHUNK_SECS:.0f}s")
    print(f"        rms-fallback: adaptive={stt._adaptive} base_threshold={base:.4f} "
          f"onset_multiplier={mult}")

    # 1) Ambient floor
    print("\n[1] Measuring ambient noise for ~2s — please stay quiet...")
    await asyncio.sleep(0.3)
    stt.flush()
    win = await asyncio.to_thread(stt.read_window, 2.0, 1.5)
    floor = _rms(win) if win is not None else 0.0
    print(f"    ambient RMS floor ≈ {floor:.4f}")
    if stt._silero is not None:
        print("    (Silero neural VAD is active — it detects speech directly, so "
              "this RMS floor is informational only.)")
    else:
        lo, hi = base, round(base * mult, 4)
        print(f"    → adaptive onset will sit in [{lo:.4f}, {hi:.4f}]")
        if floor > hi:
            print("    ⚠ ambient floor is ABOVE the max onset threshold — the room is "
                  "noisy for this mic. Raise stt.silence_threshold or move the mic closer.")
        elif floor > base:
            print("    note: ambient is above base; onset will adapt upward (good).")

    # 2) Capture rounds
    print("\n[2] Capture test. Press Enter, then speak a full command "
          "(include a short pause mid-sentence to test truncation). Blank + Enter to quit.\n")
    n = 0
    while n < 5:
        cmd = await asyncio.to_thread(input, f"  round {n + 1} — Enter to listen (or 'q' to quit): ")
        if cmd.strip().lower() == "q":
            break
        stt.flush()
        audio = await stt.record_utterance()
        dur = len(audio) / stt.fs
        print(f"    captured {dur:.1f}s | rms={_rms(audio):.4f}")
        text = await stt.transcribe(audio)
        print(f"    TRANSCRIPT: {text!r}\n")
        n += 1

    await stt.shutdown()
    print("Done. If speech was cut off: raise end_silence_sec. If speech was "
          "missed: lower silence_threshold. If text was dropped: see the "
          "'drop logprob/no_speech' lines above and lower avg_logprob_min.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
