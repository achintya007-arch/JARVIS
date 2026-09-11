# JARVIS — Project Structure

A module-by-module map of the codebase. For the high-level overview and setup,
see the [root README](../README.md); for the full design rationale see
[`docs/JARVIS_Design_Document_v0.1.md`](docs/JARVIS_Design_Document_v0.1.md).

The system is event-driven and single-process: an in-memory `EventBus` carries
typed events between perception, cognition, action, and memory, with thin
adapters bridging each subsystem to the bus.

```
jarvis_v1/
│
├── main.py                 # V2 entry point  (python main.py)
├── run.ps1                 # Windows launcher: preflight + mode/version flags
├── pyproject.toml          # Pinned deps; ruff / pytest / mypy config
├── requirements.txt        # Installs the pinned deps from pyproject
│
├── core/
│   └── config.py           # All config as dataclasses + config.yaml loader,
│                           #   JARVIS_MODE env override
│
├── core_common/            # Primitives shared by V2 and V3 (neither depends
│   │                       #   on the other)
│   ├── event_bus.py        # In-memory async pub/sub EventBus
│   ├── events.py           # Typed event definitions (UserInput, etc.)
│   └── speaking.py         # SpeakingGuard — half-duplex / self-echo guard
│
├── core_v2/                # V2 orchestrator — STABLE
│   ├── brain.py            # Event-driven orchestrator
│   ├── adapters.py         # Bridges perception/cognition/action/memory to bus
│   ├── event_bus.py        # (V2-local bus; core_common is the shared one)
│   └── events.py
│
├── core_v3/                # V3 orchestrator — ACTIVE DEFAULT
│   ├── __main__.py         # Entry point  (python -m core_v3)
│   ├── brain.py            # Vault-centric brain + wake-word voice loop,
│   │                       #   barge-in, sentence-streamed TTS
│   ├── adapters.py
│   ├── event_bus.py
│   └── events.py
│
├── perception/
│   ├── stt.py              # faster-whisper STT (base.en, CUDA int8),
│   │                       #   hallucination gating, single mic owner
│   ├── tts.py              # edge-tts TTS with interrupt() for barge-in
│   ├── hotword.py          # Wake-word detector + fuzzy "jarvis" matcher
│   ├── cues.py             # Audio cues (wake chime, etc.)
│   └── vision.py           # Optional YOLO object detection ([vision] extra)
│
├── cognition/
│   ├── llm_client.py       # Ollama streaming client; sentence_stream() for TTS;
│   │                       #   dynamic system-prompt builder
│   ├── decision_engine.py  # Routes input: FastRouter (reflex) | rules | LLM
│   ├── agent_state.py      # Mood, intent, active goals, system-load state
│   ├── goal.py             # Goal model
│   ├── goal_manager.py     # Vault-backed goals; single-owner handle_input()
│   ├── context_manager.py  # Conversation history + recall assembly
│   ├── memory_store.py     # V2 memory (SQLite + ChromaDB)
│   └── agent_core/         # Multi-turn tool-calling loop
│       ├── planner.py      # TOOL_SCHEMAS + planning prompt
│       ├── parser.py       # Parses tool calls from LLM output
│       └── schema.py       # Tool-call data structures
│
├── action/
│   ├── fast_router.py      # Deterministic fast-path for common commands
│   ├── agent_executor.py   # Tool dispatcher — shell allowlist, FS sandbox,
│   │                       #   fail-closed confirmation (security boundary)
│   ├── obsidian_tools.py   # Read/write helpers for the Obsidian vault
│   └── tools/
│       ├── screen_tool.py  # Screen capture / control
│       └── web_tools.py    # web_search (DuckDuckGo Instant Answer)
│
├── infra/
│   └── resource_monitor.py # VRAM/RAM watchdog; edge-triggered alerts,
│                           #   model downshift under pressure
│
├── memory/                 # V3 vault-centric memory layer
│   ├── vault.py            # Obsidian vault I/O (daily notes, memory notes)
│   ├── vault_indexer.py    # Indexes vault notes into the vector store
│   ├── vault_memory.py     # Two-tier recall: rolling window + semantic
│   ├── embeddings.py       # OllamaEmbedder (nomic-embed-text)
│   └── web_rag.py          # Read-only FineWeb RAG corpus (off by default)
│
├── data/                   # Offline data pipelines (build-time only)
│   ├── build_web_index.py  # Builds the FineWeb web-knowledge index
│   └── fineweb_pipeline.py # FineWeb-Edu download + content filtering
│
├── tests/                  # pytest suite — 118 tests, no GPU/Ollama needed
│   ├── conftest.py
│   ├── test_security.py        # shell allowlist / sandbox / confirmation
│   ├── test_fast_router.py
│   ├── test_agent_state.py
│   ├── test_vault.py / test_vault_goals.py
│   ├── test_wake_matching.py / test_wake_mode.py
│   ├── test_stt_gating.py
│   ├── test_resource_alerts.py
│   ├── test_web_rag.py / test_fineweb_pipeline.py
│   ├── test_obsidian_tools.py
│   └── test_phase_f.py
│
├── tools/
│   └── voice_test.py       # TTS voice/rate/pitch A/B tester
│
├── legacy/                 # V1 orchestrator — reference only, not linted/run
│   └── assistant.py
│
└── docs/
    ├── JARVIS_Design_Document_v0.1.md
    └── JARVIS_Design_Document_v0.1.pdf
```

## Runtime data (git-ignored, created on demand)

```
data/conversations.db       # V2 SQLite conversation history
data/chroma/                # V2 ChromaDB vector store
data/vault_index/           # V3 vault vector index
data/web_index/             # V3 FineWeb RAG index (if built)
data/workspace/             # file-tool sandbox
../vault/                   # user's Obsidian vault (personal notes + daily logs)
```

None of the above is committed — JARVIS recreates the structure on startup.
