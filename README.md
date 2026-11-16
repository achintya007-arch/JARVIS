# JARVIS — a local, voice-driven AI assistant

[![CI](https://github.com/achintya007-arch/JARVIS/actions/workflows/ci.yml/badge.svg)](https://github.com/achintya007-arch/JARVIS/actions/workflows/ci.yml)

Speech in → local LLM reasoning → tool execution / desktop control → speech out.
**The core runs on your own machine:** no cloud LLM API, and your conversations
and memory stay local. The LLM is served by [Ollama](https://ollama.com), speech-to-text by
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), text-to-speech by
local [Piper](https://github.com/OHF-Voice/piper1-gpl) (with
[edge-tts](https://github.com/rany2/edge-tts) as a cloud fallback), and
long-term memory lives in a local [Obsidian](https://obsidian.md) vault + an
on-disk vector index. A few tools do go online: weather (wttr.in), web search
(DuckDuckGo), and the edge-tts voice when no local Piper voice is installed.
If you turn on the optional [PhoneBox panel](#optional-phone-desk-panel-phonebox),
what you say and JARVIS's replies are shown on your phone.

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
- **Wake-word voice loop** — say *"hey jarvis"*, hear a chime, then speak. A
  single microphone owner, barge-in (interrupt mid-reply), and follow-up mode
  (answer JARVIS's questions without repeating the wake word).
- **Deterministic fast-path routing** (`FastRouter`) for common commands — time,
  weather, timers, volume, launching apps/URLs, switching windows, "what's on my
  screen?" — with zero LLM latency.
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
**[`docs/design/JARVIS_Design_Document_v0.1.md`](docs/design/JARVIS_Design_Document_v0.1.md)**
(a rendered [PDF](docs/design/JARVIS_Design_Document_v0.1.pdf) is alongside it).

### Two orchestrators

| Launch                        | Orchestrator        | State                                   |
|-------------------------------|---------------------|-----------------------------------------|
| `.\run.ps1` / `python -m core_v3` | `core_v3/brain.py`  | **Active default** — vault memory, wake voice |
| `.\run.ps1 -V2` / `python main.py` | `core_v2/brain.py`  | Stable — SQLite + ChromaDB memory       |

The original V1 orchestrator has been retired; it is preserved in git history
(tag `pre-retire-old-modules`).

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
pip install -e ".[voice]"           # full V3 voice layer: Piper TTS + Silero VAD + openWakeWord (recommended)
pip install -e ".[dev]"             # pytest, ruff, mypy
# (or install the voice pieces individually: .[piper] .[vad] .[wakeword])
```

### V3 voice layer

The V3 voice interaction is an explicit, cancellation-aware state machine —
**IDLE → LISTENING → PROCESSING → SPEAKING → (INTERRUPTED)** — built from small,
separately-testable parts (`perception/microphone.py`, `wakeword.py`,
`endpointer.py`, `audio_player.py`; `core_v3/voice/`):

- **Wake word:** a dedicated [openWakeWord](https://github.com/dscripka/openWakeWord)
  model (pretrained **"hey jarvis"**, ~3 ms/frame on CPU) — say *"hey jarvis"*.
- **Endpointing:** Silero VAD decides when you've actually finished speaking, so
  a mid-sentence pause ("open Chrome and then— actually, VS Code instead") isn't
  cut off.
- **Speaking:** Piper streams sentence-by-sentence; audio starts on the first
  sentence while the rest is still being generated.
- **Barge-in:** while JARVIS is speaking, say *"hey jarvis"* — playback stops
  immediately, generation is cancelled, and it listens for your new command (or
  "stop"). Only the wake word interrupts, so JARVIS's own voice leaking from open
  speakers can't cut itself off. With headphones you can set
  `VoiceConfig.barge_in_mode = "vad"` so any speech interrupts.
- **Follow-up mode:** when JARVIS ends a reply with a question, it keeps listening
  for ~6 s without the wake word, so you can just answer "yes" or "no".
  `VoiceConfig.follow_up`: `"question"` (default), `"always"`, or `"off"`.

Validate it live (prints wake→first-audio and end-of-speech→first-audio latencies):

```powershell
.\run.ps1 -MicSmoke        # or:  python -m tools.mic_smoke
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

### Install and start (Windows, one click)

Once the venv is set up (above), install the HUD as an app that starts
everything for you:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1                    # from the repo root
powershell -ExecutionPolicy Bypass -File .\install.ps1 -StartWithWindows  # also start at sign-in
```

This needs the .NET 8 SDK or newer. It publishes the Body (one exe, about
2 MB) to `%LOCALAPPDATA%\Programs\JARVIS` and points it at this repo's
`jarvis_v1`. It also adds a **JARVIS** shortcut to the Start Menu and the
Desktop. No admin rights are needed.

Double-click the shortcut and the Body does what `run.ps1` does, with no
console windows:

- If a JARVIS Brain is already running (you started one by hand), it uses that
  one and never stops it.
- Otherwise it runs the same preflight: is Ollama answering, and are the models
  pulled? If Ollama is installed but not running, it starts it.
- Then it starts the Brain hidden, in wake mode. Set `brain_mode` to `text` in
  `body.settings.json` for text mode.
- If something is missing, it says so on the HUD and in a Windows notification
  instead of starting a broken Brain.
- If the Brain crashes, it is restarted, at most 3 times in 5 minutes, and then
  it tells you.
- PhoneBox is started too when `config.yaml` enables it and it isn't already
  running.

From the tray icon (right-click):

- **Restart brain**.
- **Open logs folder**: `%LOCALAPPDATA%\JARVIS`, holding `body.log`,
  `brain.log` and `ollama.log`, each rotated at 5 MB.
- **Start with Windows**.
- **Quit**: asks the Brain to shut down cleanly (BBP close 1000; it is killed
  only if it hangs), then stops Ollama and PhoneBox only if the Body started
  them.

`Jarvis.Body.exe --quit` does the same as the tray's Quit. Run `install.ps1`
again after pulling changes to update the app; `uninstall.ps1` removes the
shortcuts and the startup entry (`-RemoveFiles` also deletes the installed
app). Settings are in [`body/README.md`](body/README.md#settings-optional).

### Launch (Windows, from a terminal)

`run.ps1` uses the project's `venv` interpreter and runs preflight checks
(Python, voice dependencies, Ollama — started automatically if installed but not
running — and required models) before starting:

```powershell
.\run.ps1            # V3 vault brain, wake-word voice  (default)
.\run.ps1 -Text      # V3, keyboard input (no microphone needed)
.\run.ps1 -V2        # V2 stable brain
.\run.ps1 -Test      # run the test suite and exit
.\run.ps1 -WakeCheck # live wake-word score meter (tune the threshold)
.\run.ps1 -MicSmoke  # V3 end-to-end voice test with latency readout
.\run.ps1 -MicCheck  # V2 speech-capture diagnostic
.\run.ps1 -VoiceTest # A/B test TTS voice / rate / pitch
```

### Launch (any platform)

```bash
python -m core_v3          # V3 vault brain (wake mode)
JARVIS_MODE=text python -m core_v3   # keyboard input
python main.py             # V2 stable brain
```

Set the input mode without editing config via the `JARVIS_MODE` environment
variable: `wake` (say "hey jarvis"), `text` (keyboard), or `voice` (V2
always-listening).

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
  voice: "en_US-lessac-medium"   # Piper model in voices/ (en_US-ryan-high: richer, slower)
  rate: "-4%"           # prosody; slightly slower reads as composed

agent:
  shell_enabled: false  # OFF by default — see Security
  file_enabled: true
  sandbox_dir: "data/workspace"
  confirmation_required: true

mode: "wake"            # wake | text | voice
```

### Voice input (STT) tuning

Speech capture uses a neural VAD ([Silero](https://github.com/snakers4/silero-vad),
the `vad` extra) for robust speech endpointing — it needs no per-mic threshold
tuning. Without the `[vad]` extra it falls back to an adaptive energy-based VAD
(`vad_backend: "rms"`), which adapts its threshold to your room.

- **"hey jarvis" not triggering (or only when you raise your voice):** run
  `.\run.ps1 -WakeCheck` and lower `WakeConfig.threshold` toward the peak scores
  you see when speaking normally.
- **V3 voice latency / barge-in:** `.\run.ps1 -MicSmoke`.
- **V2 capture tuning:** `.\run.ps1 -MicCheck` prints your ambient noise floor,
  the adaptive onset threshold, and — per test utterance — the stop reason,
  duration, and Whisper confidence. Then adjust
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

### Optional: desktop HUD (Brain–Body Protocol)

JARVIS can stream its state to a separate Windows app, the **Body**, over a
local WebSocket ([protocol spec](docs/protocol/BBP_v0.1.md), schemas in
[`protocol/`](protocol/)). The first Body is a WPF HUD in [`body/`](body/). It
shows the voice state, what JARVIS heard, and the reply as it streams in. You
can also type to JARVIS from it, mute the microphone from its tray icon, and use
Ctrl+Alt+J as push-to-talk from anywhere in Windows.

The server is **off by default**. To turn it on, add this to `config.yaml`:

```yaml
bbp:
  enabled: true          # listens on ws://127.0.0.1:8765/bbp (localhost only)
```

On first run the Brain creates a shared auth token at `data/bbp_token`, readable
only by your user and never committed. Then start the HUD (`dotnet run --project
body/src/Jarvis.Body`), or watch the raw events with the probe. The installed
app ([Install and start](#install-and-start-windows-one-click)) needs no
config change: a Brain it starts always has BBP on.

```bash
python -m tools.bbp_probe
```

If the server can't start, or no Body is connected, voice and text mode work
exactly as before.

With the Body connected, JARVIS's desktop tools run there with native Windows
APIs: opening apps, switching windows (it reliably gets past Windows' focus
lock, which the Python tool couldn't), listing windows, setting the volume, and
answering "what's on my screen?" from the window's UI text instead of a
screenshot. It can also press a named button ("click seven in calculator") or
fill a field, without moving the mouse, in apps you've marked for it (Notepad
and Calculator as shipped). Typing, and buttons like Send, Delete or Pay, ask
you first. The Body only touches apps on its own allowlist
(`body/src/Jarvis.Body/desktop.allowlist.json`); see
[`body/README.md`](body/README.md#desktop-control). Two settings in the `bbp`
section control this:

```yaml
bbp:
  desktop_fallback: true   # no Body connected → use the Python desktop tools
  request_timeout_s: 5     # how long the Brain waits for the Body to answer
```

The HUD also shows **confirmations**: when JARVIS wants to write a file or close
an app, a dialog shows exactly what it would do, with Deny as the default and a
countdown (`bbp.confirm_timeout_s`, 15 s; no answer means no). Voice or the
console can answer too, and the first answer wins. **Notifications** (goal
reminders, finished timers, resource alerts) appear as a card on the HUD and as
a Windows notification. See
[`body/README.md`](body/README.md#confirmations-and-notifications).

### Optional: phone desk panel (PhoneBox)

[PhoneBox](https://github.com/ersinkoc/phonebox) is a separate program that
turns a phone into a desk panel for your PC. JARVIS can show its state there,
and phone buttons can talk to JARVIS.

**JARVIS → phone.** With this in `config.yaml`, JARVIS pushes its state to the
PhoneBox agent's local webhooks
([`core_v3/adapters/phonebox.py`](jarvis_v1/core_v3/adapters/phonebox.py)):

```yaml
phonebox:
  enabled: true
  url: http://127.0.0.1:8780   # the PhoneBox agent (loopback only)
  show_transcripts: true       # what JARVIS heard
  show_replies: true           # JARVIS's last reply
  notify: true                 # goal reminders and resource alerts as notifications
```

| Pushed key | Shows |
|---|---|
| `jarvis` | Listening / Thinking / Speaking / Idle / Offline, with the model in use |
| `jarvis_active` | `on` during a turn, for a PhoneBox rule that shows the JARVIS page while it lasts |
| `jarvis_heard`, `jarvis_reply` | What JARVIS heard, and its last reply (one row per sentence) |
| `jarvis_model`, `jarvis_muted` | The model in use; `on` while the microphone is muted |
| `jarvis_timers` | A live countdown while a timer runs ("set a timer for 10 minutes"); one row per timer when there are several. A notification says when one is done |
| `jarvis_goals` | Active goals, highest priority first (P8+ in the warning colour) |

Show them with PhoneBox **Pushed value** widgets (style *Auto* for `jarvis_timers`, *List* for
`jarvis_goals`). Timers and goals have no bus events, so the adapter reads them every second
while a timer runs and every 5 seconds otherwise, and posts only changes.
If PhoneBox is not running, JARVIS works exactly as before.

**What reaches the phone.** The adapter itself only posts to `127.0.0.1`; a
non-loopback `url` disables it. But PhoneBox then shows these values on your
phone, so what you said to JARVIS and its replies do leave the PC, over
PhoneBox's own connection to the phone (see PhoneBox's docs for how it
connects). To keep them off the phone, set `show_transcripts: false` and
`show_replies: false`; the state, timers and goals still show.

**Phone → JARVIS.** A PhoneBox shortcut key runs a program on the PC.
[`tools/jarvis_send.py`](jarvis_v1/tools/jarvis_send.py) sends one command to a
running JARVIS over the Brain–Body Protocol (so `bbp.enabled` must be on):

```bash
python -m tools.jarvis_send "Give me a quick briefing for today"
python -m tools.jarvis_send --voice wake      # or mute / unmute / cancel
```

For a key, use `jarvis_v1\venv\Scripts\pythonw.exe` as the program (no console
window), `-m tools.jarvis_send --voice wake` as the arguments, and `jarvis_v1` as
the working folder.

**Ports.** PhoneBox and the BBP server both default to `8765`. Run PhoneBox with
`--port 8780` (add it to PhoneBox's Startup shortcut) so the HUD keeps its port.

## Security posture

Tool execution is an LLM-driven surface, and the LLM's context can include
untrusted data (web results, file contents, clipboard). The executor is hardened
accordingly:

- **`run_shell` is off by default.** When enabled it runs with `shell=False`,
  rejects shell metacharacters, and permits only an **executable allowlist**.
- **File tools are sandboxed** to `data/workspace`; paths outside it are denied.
- **App launches are allowlisted** ([`action/app_launcher.py`](jarvis_v1/action/app_launcher.py)):
  a known name resolves to an absolute path and starts without a shell; anything
  else is refused with `E_NOT_ALLOWED`. `open_url` accepts `http`/`https` only.
- **The Body checks again.** With the desktop HUD connected, every desktop action
  is checked against the Body's own allowlist too, and password fields are
  removed before any screen content leaves it.
- **Screen and web text is untrusted.** What `read_screen_ui` returns is labelled
  as data; JARVIS never follows instructions found in it.
- **Gated actions require confirmation** and **fail closed**. Writing a file,
  closing an app or window, and shell commands (off by default) ask first. You
  can answer by voice, in the console in text mode, or in the HUD's dialog,
  which shows exactly what would run. No answer in 15 s, an error, or the HUD
  closing mid-question means no. Typing "yes" into the HUD box or a phone
  button never counts as an answer.

See [`action/agent_executor.py`](jarvis_v1/action/agent_executor.py),
[`tests/test_security.py`](jarvis_v1/tests/test_security.py) and
[`tests/test_injection.py`](jarvis_v1/tests/test_injection.py).

## Testing

Portable unit + security tests (no GPU, desktop, or Ollama required — heavy deps
are skipped via `pytest.importorskip`):

```bash
cd jarvis_v1
pytest tests/ -v          # ~690 tests
ruff check .              # lint incl. tests/ (CI-blocking)
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the suite on
Python 3.10 and 3.11, with `ruff` as a blocking gate, and builds and tests the
C# Body (`body/`) on Windows.

## Project layout

```
jarvis_v1/
├── main.py            # V2 entry point
├── run.ps1            # terminal launcher (preflight + mode/version flags)
├── pyproject.toml     # pinned deps + ruff/pytest/mypy config
├── core/              # config (dataclasses + config.yaml loader)
├── core_common/       # shared primitives: EventBus, events, SpeakingGuard
├── core_v2/           # V2 orchestrator (stable): brain, adapters, bus
├── core_v3/           # V3 orchestrator (active): composition root,
│   ├── adapters/      #   conversation engine, resource + BBP server adapters,
│   └── voice/         #   voice state machine
├── perception/        # mic, wake word, endpointer, stt, tts, audio player,
│                      #   cues; hotword (V2)
├── cognition/         # llm_client, decision_engine, agent_state, goals,
│   └── agent_core/    #   context_manager, memory_store; tool-calling loop
├── action/            # fast_router, agent_executor, app_launcher, obsidian_tools
│   └── tools/         #   screen + web tools
├── infra/             # resource monitor (VRAM/RAM watchdog)
├── memory/            # V3 vault: vault, indexer, embeddings, web_rag
├── data/              # offline FineWeb index builder
├── tests/             # pytest suite
└── tools/             # diagnostics: wake_check, mic_smoke, mic_check, voice_test,
                       #   bbp_probe (fake Body), desktop_bench; preflight
                       #   (the Body launcher's config/model check)
```

Next to `jarvis_v1/`: `protocol/` holds the Brain–Body Protocol JSON Schemas,
`body/` the C# Windows HUD and launcher, `install.ps1` / `uninstall.ps1` install
it as an app, and [`docs/`](docs/) the project documents.

A deeper module-by-module map is in
[`docs/PROJECT_STRUCTURE.md`](docs/PROJECT_STRUCTURE.md).

## Known limitations

- **Windows-only** for desktop-control tools (volume, window focus, screen capture).
- Switching windows can still fail when the app in front runs as administrator
  (Task Manager, for example): Windows blocks input from a normal program.
- "What's on my screen?" needs the Body and reads UI names, control types and
  IDs, not document text, so JARVIS can describe an app but not read a document
  aloud.
- `web_search` uses DuckDuckGo's Instant Answer API — results are limited to the
  set of queries that API answers.
- The default TTS (Piper) is fully local. The edge-tts fallback requires a
  network connection; it is used only when no local Piper voice is available.

## Documentation

| Document | What it covers |
|----------|----------------|
| [`jarvis_v1/README.md`](jarvis_v1/README.md) | Developer-focused quick reference |
| [`docs/PROJECT_STRUCTURE.md`](docs/PROJECT_STRUCTURE.md) | Module-by-module layout |
| [`docs/design/JARVIS_Design_Document_v0.1.md`](docs/design/JARVIS_Design_Document_v0.1.md) | Full engineering design + diagrams |
| [`docs/TESTING.md`](docs/TESTING.md) | Running tests, the desktop bench, hand-test checklist |
| [`docs/protocol/BBP_v0.1.md`](docs/protocol/BBP_v0.1.md) | Brain–Body Protocol spec |
| [`protocol/README.md`](protocol/README.md) | The protocol's JSON Schemas |
| [`body/README.md`](body/README.md) | The Windows HUD: build, run, settings, desktop control |
| [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md) | Dev setup, conventions, PR flow |

## License

[MIT](LICENSE) © Achintya
