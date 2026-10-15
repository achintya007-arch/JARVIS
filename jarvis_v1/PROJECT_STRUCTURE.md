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
│   ├── brain.py            # Composition root: wires cognition + voice layer
│   ├── conversation.py     # ConversationEngine — text → response sentences
│   │                       #   (clean cognition boundary; no audio)
│   ├── voice/              # Voice state machine + orchestration
│   │   ├── state.py        #   VoiceState / VoiceStateManager
│   │   ├── orchestrator.py #   VoiceOrchestrator (IDLE→LISTEN→PROCESS→SPEAK)
│   │   └── interruption.py #   InterruptionController (barge-in via Silero)
│   ├── adapters/           # Bus adapters (no cognition of their own)
│   │   ├── resource.py     #   ResourceAdapter (model switching on pressure)
│   │   ├── bbp_protocol.py #   Brain–Body Protocol envelopes + schema validation
│   │   ├── bbp_server.py   #   BBP WebSocket server (off unless bbp.enabled)
│   │   └── bbp_commands.py #   Body commands: input.text, voice.control, ui.hotkey
│   ├── event_bus.py
│   └── events.py           # V3 events incl. VoiceStateChanged, TranscriptReady,
│                           #   ReplyChunk, ReplyDone (forwarded to Bodies)
│
├── perception/            # voice primitives (small, single-purpose)
│   ├── microphone.py      # MicrophoneInput — single mic owner, async fan-out
│   ├── wakeword.py        # WakeWordDetector — openWakeWord "hey_jarvis" (V3)
│   ├── endpointer.py      # EndpointDetector — Silero VAD onset/endpoint (V3)
│   ├── stt.py             # STTEngine — faster-whisper transcription (V3 uses
│   │                      #   transcribe(); the mic/VAD capture serves V2)
│   ├── tts.py             # TTSEngine — Piper/edge synthesis (+ V2 queue/speak)
│   ├── audio_player.py    # AudioPlayer — cancellable playback + interrupt() (V3)
│   ├── hotword.py         # tiny.en wake (V2 only; V3 uses wakeword.py)
│   └── cues.py            # Audio cues (wake chime, etc.)
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
├── tests/                  # pytest suite — 315 tests, no GPU/Ollama needed
│   ├── conftest.py
│   ├── test_security.py        # shell allowlist / sandbox / confirmation
│   ├── test_fast_router.py / test_hot_path.py
│   ├── test_agent_state.py / test_resource_alerts.py
│   ├── test_vault.py / test_vault_goals.py / test_obsidian_tools.py
│   ├── test_voice_*.py         # V3 voice: endpoint, player, state, orchestrator
│   ├── test_conversation_followup.py
│   ├── test_stt_gating.py / test_stt_vad.py / test_tts_engine.py
│   ├── test_wake_matching.py   # V2 hotword matcher
│   ├── test_web_rag.py / test_fineweb_pipeline.py
│   ├── test_bbp_contract.py / test_bbp_server.py / test_bbp_events.py
│   ├── test_bbp_commands.py / test_voice_body_controls.py
│   └── test_phase_f.py
│
├── tools/                  # Diagnostics (launched via run.ps1 flags)
│   ├── wake_check.py       # -WakeCheck: live wake-word score meter
│   ├── mic_smoke.py        # -MicSmoke: V3 end-to-end voice + latency
│   ├── mic_check.py        # -MicCheck: V2 speech-capture diagnostic
│   ├── voice_test.py       # -VoiceTest: TTS voice/rate/pitch A/B tester
│   └── bbp_probe.py        # fake Body: prints every BBP event (python -m tools.bbp_probe)
│
└── docs/
    ├── JARVIS_Design_Document_v0.1.md
    └── JARVIS_Design_Document_v0.1.pdf
```

Outside `jarvis_v1/`: `../protocol/` (BBP JSON Schemas, shared by Brain and
Body), `../body/` (C# Windows HUD), `../docs/protocol/BBP_v0.1.md` (spec).

## Known gaps

- V3 has no subscriber for `ActionSuggestion`, so actionable goal reminders
  ("Sir, shall I …?") are dropped; plain reminders still work. Tracked in
  [issue #1](https://github.com/achintya007-arch/JARVIS/issues/1).

## Runtime data (git-ignored, created on demand)

```
data/conversations.db       # V2 SQLite conversation history
data/chroma/                # V2 ChromaDB vector store
data/vault_index/           # V3 vault vector index
data/web_index/             # V3 FineWeb RAG index (if built)
data/workspace/             # file-tool sandbox
data/bbp_token              # BBP shared auth token (current user only)
../vault/                   # user's Obsidian vault (personal notes + daily logs)
```

None of the above is committed — JARVIS recreates the structure on startup.
