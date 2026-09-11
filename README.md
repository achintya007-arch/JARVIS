# JARVIS — a local, voice-driven AI assistant

Speech in → local LLM reasoning → tool execution / desktop control → speech out.
**Everything runs on your own machine.** No cloud API, no data leaving the box:
the LLM is served by [Ollama](https://ollama.com), speech-to-text by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), text-to-speech by
local [Piper](https://github.com/OHF-Voice/piper1-gpl) (with
[edge-tts](https://github.com/rany2/edge-tts) as a cloud fallback), and
long-term memory lives in a local [Obsidian](https://obsidian.md) vault + an
on-disk vector index.

> **Platform:** Windows-targeted (desktop-control tools use Windows APIs; the
> core conversation loop is cross-platform).
> **Status:** V2 is stable; V3 (vault-centric memory + wake-word voice) is the
> active default.

The application lives in [`jarvis_v1/`](jarvis_v1/). All commands below assume
you have `cd`'d into that directory.

---

## What it does

- **Voice or text conversation** with a local LLM, streamed sentence-by-sentence
  into TTS so JARVIS starts speaking before the full reply is generated.
- **Wake-word voice loop** — say *"Jarvis"*, hear a chime, then speak. A single
  microphone owner, half-duplex turn-taking, and barge-in (interrupt mid-reply).
- **Deterministic fast-path routing** (`FastRouter`) for common commands — time,
  weather, timers, volume, launching apps/URLs — with zero LLM latency.
- **Agentic tool use** — the LLM calls tools (system info, files, shell, web
  search, screen capture, volume, timers) through a multi-turn planning loop.
- **Persistent memory** — conversations and facts are stored and semantically
  recalled (SQLite + ChromaDB in V2; an Obsidian vault + vector index in V3).
- **Proactive goals & reminders** — set goals by voice; JARVIS tracks them in
  `goals.md` and reminds you when you're idle.
- **Resource-aware** — downshifts to a smaller model under VRAM/RAM pressure.

## Architecture at a glance

Event-driven and single-process. An in-memory `EventBus` decouples perception,
cognition, and action; thin adapters bridge each subsystem to the bus.

```
 Perception  (wake-word · STT · text)
        │  UserInput
        ▼
      Brain  ──►  AgentState (mood · intent · goals · system load)
        │         DecisionEngine → FastRouter (reflex) │ rules │ LLM
        ▼
   Action  (FastRouter · AgentExecutor tools · TTS)  ◄── LLMClient (Ollama, streaming)
        │
   Memory  (V2: SQLite + ChromaDB   ·   V3: Obsidian vault + vector index)
```

Full engineering write-up — with sequence diagrams for the voice flow, memory
pipeline, and decision pipeline — is in
**[`jarvis_v1/docs/JARVIS_Design_Document_v0.1.md`](jarvis_v1/docs/JARVIS_Design_Document_v0.1.md)**
(a rendered [PDF](jarvis_v1/docs/JARVIS_Design_Document_v0.1.pdf) is alongside it).

### Three orchestrator generations

| Launch                        | Orchestrator        | State                                   |
|-------------------------------|---------------------|-----------------------------------------|
| `.\run.ps1` / `python -m core_v3` | `core_v3/brain.py`  | **Active default** — vault memory, wake voice |
| `.\run.ps1 -V2` / `python main.py` | `core_v2/brain.py`  | Stable — SQLite + ChromaDB memory       |
| `legacy/assistant.py`         | V1                  | Retained for reference only             |

Shared primitives (`EventBus`, event types, `SpeakingGuard`) live in
`core_common/` so V2 and V3 don't depend on each other.

## Prerequisites

- **Python 3.10+**
- **[Ollama](https://ollama.com)** running locally, with the models pulled:
  ```bash
  ollama pull qwen2.5:7b-instruct-q4_K_M   # main reasoning model
  ollama pull qwen2.5:3b-instruct-q4_K_M   # lighter fallback under load
  ollama pull nomic-embed-text             # memory embeddings
  ```
- A **CUDA GPU** is recommended for faster-whisper STT (it falls back to CPU).
- For the wake-word voice loop: a working **microphone** and **speakers**
  (headphones recommended for the always-listening `voice` mode).

## Install & run

```bash
cd jarvis_v1
pip install -r requirements.txt     # pinned core dependencies

# optional feature sets:
pip install -e ".[piper]"           # local offline TTS (recommended — see below)
pip install -e ".[vision]"          # YOLO object detection (heavy — pulls torch)
pip install -e ".[dev]"             # pytest, ruff, mypy
```

### Text-to-speech: local Piper (default) vs. cloud edge-tts

By default (`tts.engine: "piper"`) JARVIS speaks with a **local Piper** voice —
lower first-audio latency than cloud TTS and no network dependency. It needs the
`[piper]` extra above plus a voice model placed in `jarvis_v1/voices/`:

```bash
# download a voice (.onnx + .onnx.json) into voices/ — e.g. from
# https://huggingface.co/rhasspy/piper-voices  (en_US-lessac-medium is a good,
# low-latency default; en_US-ryan-high sounds richer but is slower per sentence)
```

Set the model name via `tts.voice` (e.g. `en_US-lessac-medium`). **If the Piper
package or the model file is missing, JARVIS automatically falls back to
edge-tts** — so it still speaks out of the box with no extra setup. To force the
cloud backend, set `tts.engine: "edge"`.

### Launch (Windows, recommended)

`run.ps1` runs preflight checks (Python, Ollama reachability, required models)
before starting:

```powershell
.\run.ps1            # V3 vault brain, wake-word voice  (default)
.\run.ps1 -Text      # V3, keyboard input (no microphone needed)
.\run.ps1 -V2        # V2 stable brain
.\run.ps1 -Test      # run the test suite and exit
.\run.ps1 -VoiceTest # A/B test TTS voice / rate / pitch
```

### Launch (any platform)

```bash
python -m core_v3          # V3 vault brain (wake mode)
JARVIS_MODE=text python -m core_v3   # keyboard input
python main.py             # V2 stable brain
```

Set the input mode without editing config via the `JARVIS_MODE` environment
variable: `wake` (say "Jarvis"), `text` (keyboard), or `voice` (always-listening).

## Configuration

Configuration is read from `config.yaml` in `jarvis_v1/` **if present**;
otherwise sensible built-in defaults are used. Every option and its default is
defined in [`core/config.py`](jarvis_v1/core/config.py). A minimal example:

```yaml
llm:
  model: "qwen2.5:7b-instruct-q4_K_M"
  fallback_model: "qwen2.5:3b-instruct-q4_K_M"
  temperature: 0.7
  context_window: 4096

stt:
  model: "base.en"
  device: "cuda"        # "cpu" if you have no CUDA GPU
  compute_type: "int8"

tts:
  engine: "piper"       # "piper" (local, default) or "edge" (cloud fallback)
  voice: "en_US-ryan-high"   # Piper model name in voices/ (or an en-US-* edge voice)
  rate: "-4%"           # prosody; slightly slower reads as composed

agent:
  shell_enabled: false  # OFF by default — see Security
  file_enabled: true
  sandbox_dir: "data/workspace"
  confirmation_required: true

mode: "wake"            # wake | text | voice
```

### Voice input (STT) tuning

Speech capture adapts its silence threshold to your mic and room, but if it cuts
you off, misses quiet speech, or mishears, run the diagnostic to see exactly
what it hears and calibrate:

```powershell
.\run.ps1 -MicCheck        # or:  python -m tools.mic_check
```

It prints your ambient noise floor, the adaptive onset threshold, and — for each
test utterance — the stop reason, duration, and Whisper confidence. Then adjust
`STTConfig` in [`core/config.py`](jarvis_v1/core/config.py) (or `config.yaml`):
`end_silence_sec` (raise if it truncates mid-sentence), `silence_threshold`
(lower if quiet speech is missed), `avg_logprob_min` (lower if real speech is
dropped), or set `debug_audio: true` to see the same diagnostics during normal use.

### Optional: local web-knowledge index (V3)

For grounded answers to factual questions, build a one-time RAG corpus from
FineWeb-Edu (offline after the initial download; needs GPU time to embed):

```bash
pip install -e ".[data]"                   # the `datasets` package
ollama pull nomic-embed-text
python -m data.build_web_index --gb 1.0    # ~200k chunks, content-filtered
```

Then set `web_rag.enabled: true` in `config.yaml`. Until built, JARVIS runs fine
without it — the index is queried only for factual queries and stays off by default.

## Security posture

Tool execution is an LLM-driven surface, and the LLM's context can include
untrusted data (web results, file contents, clipboard). The executor is hardened
accordingly:

- **`run_shell` is off by default.** When enabled it runs with `shell=False`,
  rejects shell metacharacters, and permits only an **executable allowlist**.
- **File tools are sandboxed** to `data/workspace`; paths outside it are denied.
- **App/URL launches are allowlisted** (`open_url` accepts `http`/`https` only).
- **Gated actions require confirmation** via a voice/text-answerable prompt and
  **fail closed** if no confirmation channel is available.

See [`action/agent_executor.py`](jarvis_v1/action/agent_executor.py) and
[`tests/test_security.py`](jarvis_v1/tests/test_security.py).

## Testing

Portable unit + security tests (no GPU, desktop, or Ollama required — heavy deps
are skipped via `pytest.importorskip`):

```bash
cd jarvis_v1
pytest tests/ -v          # 118 tests
ruff check .              # lint (CI-blocking)
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the suite on
Python 3.10 and 3.11, with `ruff` as a blocking gate.

## Project layout

```
jarvis_v1/
├── main.py            # V2 entry point
├── run.ps1            # Windows launcher (preflight + mode/version flags)
├── pyproject.toml     # pinned deps + ruff/pytest/mypy config
├── core/              # config (dataclasses + config.yaml loader)
├── core_common/       # shared primitives: EventBus, events, SpeakingGuard
├── core_v2/           # V2 orchestrator (stable): brain, adapters, bus
├── core_v3/           # V3 orchestrator (active): vault brain, wake voice
├── perception/        # stt, tts, hotword, audio cues, vision
├── cognition/         # llm_client, decision_engine, agent_state, goals,
│   └── agent_core/    #   context_manager, memory_store; tool-calling loop
├── action/            # fast_router, agent_executor, obsidian_tools
│   └── tools/         #   screen + web tools
├── infra/             # resource monitor (VRAM/RAM watchdog)
├── memory/            # V3 vault: vault, indexer, embeddings, web_rag
├── data/              # offline FineWeb index builder
├── tests/             # pytest suite (118 tests)
├── tools/             # voice A/B tester
├── legacy/            # V1 orchestrator (reference only)
└── docs/              # engineering design document (md + pdf)
```

A deeper module-by-module map is in
[`jarvis_v1/PROJECT_STRUCTURE.md`](jarvis_v1/PROJECT_STRUCTURE.md).

## Known limitations

- **Windows-only** for desktop-control tools (volume, window focus, screen capture).
- `web_search` uses DuckDuckGo's Instant Answer API — results are limited to the
  set of queries that API answers.
- The default TTS (Piper) is fully local. The edge-tts fallback requires a
  network connection; it is used only when no local Piper voice is available.

## Documentation

| Document | What it covers |
|----------|----------------|
| [`jarvis_v1/README.md`](jarvis_v1/README.md) | Developer-focused quick reference |
| [`jarvis_v1/PROJECT_STRUCTURE.md`](jarvis_v1/PROJECT_STRUCTURE.md) | Module-by-module layout |
| [`jarvis_v1/docs/JARVIS_Design_Document_v0.1.md`](jarvis_v1/docs/JARVIS_Design_Document_v0.1.md) | Full engineering design + diagrams |
| [`jarvis_v1/TESTING.md`](jarvis_v1/TESTING.md) | Test suite and how to run it |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, conventions, PR flow |

## License

[MIT](LICENSE) © Achintya
