# JARVIS

A local, voice-driven AI assistant. Speech in → local LLM reasoning → tool
execution / desktop control → speech out. Everything runs on your machine:
the LLM is served by [Ollama](https://ollama.com), speech-to-text by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), and text-to-speech
by local [Piper](https://github.com/OHF-Voice/piper1-gpl) (with
[edge-tts](https://github.com/rany2/edge-tts) as a cloud fallback).

> **Platform:** Windows-targeted (desktop-control tools use Windows APIs).
> **Status:** V3 (vault-centric memory + wake-word voice) is the active default; V2 remains stable.
>
> This is the developer-focused reference. For the full overview, setup, and
> documentation index, see the [root README](../README.md).

---

## What it does

- **Voice or text conversation** with a local LLM, streamed sentence-by-sentence to TTS.
- **Deterministic fast-path routing** (`FastRouter`) for common commands — time,
  weather, timers, volume, app/URL launching — with zero LLM latency.
- **Agentic tool use** — the LLM can call tools (system info, files, shell,
  web search, screen control) through a multi-turn planning loop.
- **Persistent memory** — conversations are stored (SQLite + ChromaDB in V2;
  an Obsidian vault in V3) and semantically recalled.
- **Proactive goals & reminders** — set goals by voice; JARVIS reminds you when idle.
- **Resource-aware** — downshifts to a smaller model under VRAM/RAM pressure.

## Architecture

Event-driven, single-process. An in-memory `EventBus` decouples perception,
cognition, and action; thin adapters bridge each subsystem to the bus.

```
 Perception (STT / hotword / text)
        │  UserInput
        ▼
      Brain  ──►  AgentState (mood, intent, goals, system load)
        │         DecisionEngine  → FastRouter (reflex) │ rules │ LLM
        ▼
   Action (AgentExecutor tools, TTS)  ◄── LLMClient (Ollama, streaming)
        │
   Memory (SQLite + ChromaDB  ·  V3: Obsidian vault)
```

Two orchestrators coexist; shared primitives live in `core_common/`:

| Entry point         | Orchestrator       | State                            |
|---------------------|--------------------|----------------------------------|
| `python -m core_v3` | `core_v3/brain.py` | **Active default** (vault memory) |
| `python main.py`    | `core_v2/brain.py` | Stable (SQLite + ChromaDB)       |

The original V1 (`legacy/assistant.py`) is retained for reference only.

## Security posture

Tool execution is an LLM-driven surface, and the LLM's context can include
untrusted data (web results, file contents, clipboard). The executor is
hardened accordingly:

- **`run_shell` is off by default.** When enabled it runs with `shell=False`,
  rejects shell metacharacters, and only permits an **executable allowlist**.
- **File tools are sandboxed** to `data/workspace`; paths outside it are denied.
- **App/URL launches are allowlisted** (`open_url` accepts http/https only).
- **Gated actions require confirmation** via a voice/text-answerable prompt and
  **fail closed** if no confirmation channel is available.

See `action/agent_executor.py` and `tests/test_security.py`.

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) running locally, with models pulled:
  ```
  ollama pull qwen2.5:7b-instruct-q4_K_M      # main model
  ollama pull qwen2.5:3b-instruct-q4_K_M       # lighter fallback under load
  ollama pull nomic-embed-text                 # memory embeddings
  ```
- A CUDA GPU is recommended for faster-whisper (falls back to CPU).

## Install & run

```bash
pip install -r requirements.txt        # core dependencies (pinned)
# optional feature sets:
pip install -e ".[vision]"             # YOLO object detection (heavy)
pip install -e ".[dev]"                # pytest, ruff, mypy

python -m core_v3                      # start V3 (vault brain, wake mode) — default
python main.py                         # start V2 (stable brain)
```

On Windows, prefer `.\run.ps1` (it runs preflight checks first): `.\run.ps1`
(V3), `.\run.ps1 -Text` (keyboard), `.\run.ps1 -V2`, `.\run.ps1 -Test`.

Configuration is read from `config.yaml` if present (see `core/config.py` for
all options and defaults); otherwise built-in defaults are used.

### Optional: web-knowledge index (V3)

For grounded answers to factual questions, build a local RAG corpus from
FineWeb-Edu (one-time, offline; needs internet + GPU time for embedding):

```bash
pip install -e ".[data]"                 # the datasets package
ollama pull nomic-embed-text
python -m data.build_web_index --gb 1.0   # ~200k chunks, filtered via data/blocklists
```

Then set `web_rag.enabled: true` in `config.yaml`. Until built, JARVIS runs
fine without it — the index is queried only for factual queries and stays off
by default.

## Testing

Portable unit + security tests (no GPU/desktop/Ollama required):

```bash
pytest tests/ -v
```

CI (`.github/workflows/ci.yml`) runs these on Python 3.10 and 3.11.

## Project layout

```
perception/   STT, TTS, hotword detection
cognition/    LLM client, decision engine, agent state, memory, goals, agent-core loop
action/        FastRouter, AgentExecutor (tools), desktop control
infra/         resource monitor
core/          config
core_v2/       V2 event-driven orchestrator (stable)
core_v3/       V3 vault-centric orchestrator (in development)
memory/        V3 vault memory layer
legacy/        V1 orchestrator (reference only)
tests/         pytest suite
```

## Known limitations

- Windows-only for desktop-control tools.
- `web_search` uses DuckDuckGo's Instant Answer API and returns results for a
  limited set of queries.
- V3 memory and vault tooling are still under construction (see the roadmap).
