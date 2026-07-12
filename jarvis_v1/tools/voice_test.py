"""
tools/voice_test.py — quickly A/B test TTS voice, rate, and pitch.

Speaks a set of sample JARVIS lines through the real TTSEngine — same
synthesis/playback path the assistant uses — without needing STT, an LLM,
or the full Brain. Use this to tune core/config.py's TTSConfig (voice, rate,
pitch) by ear before committing to a choice.

Usage:
    python -m tools.voice_test                              # current config defaults
    python -m tools.voice_test --voice en-GB-RyanNeural
    python -m tools.voice_test --rate -8% --pitch -2Hz
    python -m tools.voice_test --list-voices                 # curated JARVIS-ish options
    python -m tools.voice_test --text "Say this instead, Sir."
"""

from __future__ import annotations

import argparse
import asyncio

from core.config import TTSConfig
from perception.tts import TTSEngine

# A curated shortlist of deep/composed neural voices worth comparing for a
# JARVIS persona. Full catalogue: `edge-tts --list-voices` (needs network).
_SUGGESTED_VOICES = {
    "en-US-ChristopherNeural": "Deep, professional (current default)",
    "en-US-GuyNeural":         "Warm, conversational",
    "en-GB-RyanNeural":        "British, composed — closest to Paul Bettany's JARVIS",
    "en-GB-ThomasNeural":      "British, younger, brisk",
    "en-AU-WilliamNeural":     "Australian, calm",
    "en-IE-ConnorNeural":      "Irish, measured",
}

_SAMPLE_LINES = [
    "Good evening, Sir. All systems are online.",
    "Might I suggest a shorter route? Traffic on the interstate is heavier than usual.",
    "Sir, I've marked the report as complete and logged it to your daily note.",
    "Vault memory is online. I recall you mentioned pasta for lunch.",
    "Understood. I'll keep things brief while you're running low.",
]


async def main() -> None:
    ap = argparse.ArgumentParser(description="A/B test JARVIS's TTS voice.")
    ap.add_argument("--voice", default=None, help="edge-tts voice name (see --list-voices)")
    ap.add_argument("--rate", default=None, help='e.g. "-4%%" (default from config)')
    ap.add_argument("--pitch", default=None, help='e.g. "+0Hz" (default from config)')
    ap.add_argument("--text", default=None, help="Speak this instead of the sample lines")
    ap.add_argument("--list-voices", action="store_true", help="Print suggested voices and exit")

    # argparse's negative-number detection doesn't recognize "-8%" or "-2Hz" as
    # values (only bare integers/floats), so `--rate -8%` errors as "expected
    # one argument". Rewrite bare `--rate X` / `--pitch X` to `--rate=X` /
    # `--pitch=X` before parsing so the natural CLI syntax works.
    import sys
    raw = sys.argv[1:]
    fixed: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in ("--rate", "--pitch") and i + 1 < len(raw):
            fixed.append(f"{tok}={raw[i + 1]}")
            i += 2
        else:
            fixed.append(tok)
            i += 1

    args = ap.parse_args(fixed)

    if args.list_voices:
        print("Suggested JARVIS-ish voices (edge-tts):\n")
        for name, desc in _SUGGESTED_VOICES.items():
            print(f"  {name:28} {desc}")
        print("\nTry one with:  python -m tools.voice_test --voice <name>")
        return

    cfg = TTSConfig()
    if args.voice:
        cfg.voice = args.voice
    if args.rate:
        cfg.rate = args.rate
    if args.pitch:
        cfg.pitch = args.pitch

    print(f"Voice: {cfg.voice} | rate: {cfg.rate} | pitch: {cfg.pitch}\n")

    engine = TTSEngine(cfg)
    await engine.initialize()
    try:
        lines = [args.text] if args.text else _SAMPLE_LINES
        for line in lines:
            print(f"  -> {line}")
            await engine.speak(line)
    finally:
        await engine.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
