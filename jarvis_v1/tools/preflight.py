"""
What the JARVIS launcher (the Body) needs to know before starting the Brain,
read from the same config.yaml the Brain uses, as one line of JSON:

    python -m tools.preflight            (run with cwd = jarvis_v1)

{
  "mode": "wake",
  "ollama_url": "http://localhost:11434",
  "models": {"required": ["qwen2.5:7b-instruct-q4_K_M", "nomic-embed-text"],
             "optional": ["qwen2.5:3b-instruct-q4_K_M"]},
  "bbp": {"enabled": true, "port": 8765},
  "phonebox": {"enabled": false, "url": "http://127.0.0.1:8780"},
  "warnings": ["..."]
}

"required" models are the ones without which the Brain is broken (the main LLM
and the embedding model for memory); a missing "optional" one (the lighter
fallback model) is only a warning. The launcher checks Ollama itself, because it
may have to start it first. Exit code 0 even with warnings; 1 only if the
config can't be read.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

VOICE_MODULES = ("openwakeword", "silero_vad", "piper")


def report(config) -> dict:
    llm = config.llm
    warnings = []
    if config.mode == "wake":
        missing = [m for m in VOICE_MODULES if importlib.util.find_spec(m) is None]
        if missing:
            warnings.append(f"voice packages missing ({', '.join(missing)}): the wake word and "
                            "speech detection won't work well; install with pip install -e \".[voice]\"")
    optional = [llm.fallback_model] if llm.fallback_model and llm.fallback_model != llm.model else []
    return {
        "mode": config.mode,
        "ollama_url": llm.base_url,
        "models": {"required": [llm.model, config.web_rag.embed_model], "optional": optional},
        "bbp": {"enabled": bool(config.bbp.enabled), "port": int(config.bbp.port)},
        "phonebox": {"enabled": bool(config.phonebox.enabled), "url": config.phonebox.url},
        "warnings": warnings,
    }


def main() -> int:
    try:
        from core.config import Config
        config = Config.load(Path("config.yaml"))
    except Exception as e:  # noqa: BLE001 — report any config problem to the launcher
        print(json.dumps({"error": f"config.yaml can't be read: {e}"}))
        return 1
    print(json.dumps(report(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
