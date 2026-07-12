# JARVIS — Testing & Task List

## How to run

```powershell
.\run.ps1              # V3 (vault brain), voice
.\run.ps1 -Text        # V3, keyboard input (no mic needed) — easiest for a first check
.\run.ps1 -V2          # V2 (stable brain)
.\run.ps1 -Test        # run the automated test suite (111 tests)
.\run.ps1 -VoiceTest   # A/B test TTS voice/rate/pitch without the full pipeline
```

### Tuning the voice

`tools/voice_test.py` speaks sample lines through the real TTS engine so you
can compare voices/cadence without running STT or the LLM:

```powershell
python -m tools.voice_test --list-voices              # curated JARVIS-ish options
python -m tools.voice_test --voice en-GB-RyanNeural    # try a different voice
python -m tools.voice_test --rate -8% --pitch -2Hz     # tune cadence
python -m tools.voice_test --text "Custom line, Sir."  # test one line
```

Once you land on settings you like, put them in `config.yaml` under `tts:`
(`voice`, `rate`, `pitch`) or edit the defaults in `core/config.py::TTSConfig`.

The script preflights Python + Ollama + required models before launching.

## Prerequisites for the full experience

- [ ] `ollama serve` running
- [ ] `ollama pull qwen2.5:7b-instruct-q4_K_M`  (main LLM)
- [ ] `ollama pull nomic-embed-text`  (semantic memory recall)
- [ ] Speakers + microphone (for voice mode)

Without Ollama, JARVIS still starts and handles tool commands (time, timers,
notes, goals, volume) — but LLM replies and semantic recall are disabled.

---

## Live verification checklist

### Startup
- [ ] Greeting is spoken (time + CPU/RAM). Says "Vault memory is online" once `nomic-embed-text` is pulled.
- [ ] `Ctrl+C` shuts down cleanly ("Brain V3 shutdown complete." in the log).

### Fast routing (works without Ollama)
- [ ] "what time is it" → speaks the time
- [ ] "set a timer for 1 hour 30 minutes" → confirms **1h 30m** (not 3600s)
- [ ] "set the volume to 30" → volume changes; "what's the volume" → reports it
- [ ] "open chrome and set volume to 50" → both run (compound command)

### Memory (needs `nomic-embed-text`)
- [ ] "I had pasta for lunch" → later, "what did I have for lunch?" recalls it
- [ ] Restart JARVIS, ask again → still remembered (cross-session)
- [ ] Open the vault: `vault/JARVIS/daily/<today>.md` shows the conversation

### Goals (single response — no double-talk)
- [ ] "remind me to submit the report" → "Goal noted, Sir." (once)
- [ ] "what are my goals" → lists it
- [ ] "I finished the report" → "marked complete" **once**, no second reply
- [ ] Open `vault/JARVIS/goals.md` → task flipped to `- [x]`

### Vault tools
- [ ] "make a note about the meeting tomorrow" → note file appears in `vault/JARVIS/memory/`
- [ ] "what did I do today" → summarizes today's note
- [ ] "what do my notes say about X" → recalls a relevant note (needs embeddings)

### Barge-in (voice mode)
- [ ] While JARVIS is speaking, say "stop" → cuts off + "Stopped, Sir."
- [ ] While speaking, say "Jarvis, what time is it" → cuts off + answers
- [ ] Ambient noise / normal chatter does **not** interrupt it

### Ambient presence & live context
- [ ] Leave it idle a few minutes → it may volunteer a check-in
- [ ] "what am I working on" → references your foreground window

### Voice
- [ ] Delivery sounds composed (the -4% cadence); voice is the deep neural one

### Resource awareness
- [ ] Under heavy GPU/RAM load → warns and switches to the compact model

### Web knowledge (optional — only if you built the index)
- [ ] `python -m data.build_web_index --gb 1.0` completes
- [ ] Set `web_rag.enabled: true`; ask a factual question → grounded answer

---

## Project task list

### Done
- [x] Security hardening (shell allowlist, sandbox, fail-closed confirm)
- [x] Packaging (`pyproject.toml`), 111-test suite, CI, README
- [x] Phase B — vault as memory
- [x] Phase C — vault tools + vault-backed goals
- [x] Phase D — FineWeb web-knowledge RAG (code; index build is a user step)
- [x] Phase F — voice tuning, barge-in, ambient presence, live context
- [x] Fixes: volume (pycaw API), goal double-response race

### Next up
- [ ] **Phase G — coding partner** (the big "code my stuff" capability)
- [ ] Phase E — cutover: dogfood V3 for a few days, then move `core_v2/` to `legacy/`
- [ ] Phase H — benchmarks (`bench_stt`, `bench_routing`, `bench_e2e`) + demo video

### Small follow-ups
- [ ] The unnamed "something else" from the Phase F menu — clarify & do
- [ ] `ruff --fix` pass, then flip CI ruff from non-blocking to blocking
- [ ] XTTS voice cloning (deferred; needs a reference voice clip)
