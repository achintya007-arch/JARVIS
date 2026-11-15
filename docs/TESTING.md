# JARVIS — Testing & Task List

Run every command below from `jarvis_v1/`.

## How to run

```powershell
.\run.ps1              # V3 (vault brain), voice
.\run.ps1 -Text        # V3, keyboard input (no mic needed) — easiest for a first check
.\run.ps1 -V2          # V2 (stable brain)
.\run.ps1 -Test        # run the automated test suite (678 tests)
.\run.ps1 -VoiceTest   # A/B test TTS voice/rate/pitch without the full pipeline
```

### Brain–Body Protocol (desktop HUD)

With `bbp: {enabled: true}` in `config.yaml`, the Brain serves
`ws://127.0.0.1:8765/bbp`. Watch it with the fake Body, or the real HUD:

```powershell
python -m tools.bbp_probe              # prints welcome snapshot + every event
python -m tools.bbp_probe --raw        # full JSON envelopes
python -m tools.bbp_probe --token bad  # should close with 4401
cd ..\body; dotnet test; dotnet run --project src\Jarvis.Body
```

With the HUD running: type in its box and press Enter (the reply shows on the HUD and is not spoken). Press Ctrl+Alt+J and speak (push-to-talk). Right-click the tray icon to mute or unmute: the HUD shows "muted", and Ctrl+Alt+J is then refused with a notice. The Body log is %LOCALAPPDATA%\JARVIS\body.log.

### Desktop control benchmark

`tools/desktop_bench.py` opens, focuses, lists and reads real app windows
(Notepad, Chrome, VS Code, File Explorer) and nudges the volume by 1–2 %,
checking each result with Win32 calls. It closes only the windows it opened and
restores your volume and foreground window afterwards.

```powershell
python -m tools.desktop_bench                  # the Python tools
python -m tools.desktop_bench --backend body   # through the C# Body (start it pointed at port 8766)
```

For `--backend body`, put `{"url": "ws://127.0.0.1:8766/bbp"}` in
`body.settings.json` next to `Jarvis.Body.exe`, start the bench, then start the
Body. Results are saved under `data/bench/`.

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

### Desktop control (needs the Body; `bbp.enabled: true`)
Watch the JARVIS console for `desktop <topic> via body` (or `via python`).
- [ ] "open calculator" → Calculator opens
- [ ] Minimize it or bury it under other windows; "switch to calculator" → it comes to the front
- [ ] With Calculator in front, "what's on my screen?" → a short description of Calculator
- [ ] With an app that isn't on the allowlist in front (e.g. WhatsApp) → refused, naming the app
- [ ] "set volume to 30", "volume up" → changes; the reply says if output is muted
- [ ] "open cmd" → refused, nothing runs
- [ ] Quit the Body; "open notepad" → still opens (`via python`)
- [ ] `bbp.desktop_fallback: false`, Body closed → refused because the Body isn't connected
- [ ] Remove Notepad from `desktop.allowlist.json` next to `Jarvis.Body.exe`, restart the Body; "open notepad" → refused by the Body

### Confirmations and notifications (needs the Body; `bbp.enabled: true`)
Gated actions: `write_file`, `run_shell` (if enabled), `close_app`, `close_window`.
- [ ] Ask JARVIS to write a short note to a file in `data/workspace/` → a dialog shows the file and its size, **Deny** highlighted, a 15 s countdown
- [ ] Click **Approve** → it runs; try again and click **Deny** → it doesn't
- [ ] Answer by voice ("yes" / "no") instead → the dialog closes by itself
- [ ] Text mode (`.\run.ps1 -Text`): the console shows `Approve? yes/no`; typing works, and so does the dialog
- [ ] Don't answer → after 15 s JARVIS says it won't do it; the dialog closes
- [ ] While the dialog is open, type "yes" in the HUD box → refused ("waiting for you to approve or deny")
- [ ] Quit the Body while a dialog is open → the action is denied at once
- [ ] "close calculator" → the dialog says **High risk**; Tab can't reach Approve, only a click can
- [ ] "set a timer for 10 seconds" → when it goes off: a card on the HUD and a Windows notification

### Clicking and typing in apps (needs the Body)
- [ ] "open calculator", then "click one in calculator", "click plus", "click two", "click equals" → 3, no dialog, the mouse doesn't move
- [ ] "press the equals button in calculator" → pressed (narrowed to a Button)
- [ ] "click compose in chrome" → refused: Chrome isn't marked `act` in `desktop.allowlist.json`
- [ ] "click nine in calculator" with Calculator closed → "Calculator has no open window"
- [ ] Mark an app `"act": true`, ask JARVIS to press its **Send** or **Delete** button → a **High risk** dialog first; Deny → nothing pressed
- [ ] Ask JARVIS to type something into a search box → the dialog shows the exact text; Approve → it's in the box, Enter not pressed
- [ ] Two buttons with the same name → JARVIS lists them and says which differ, instead of guessing
- [ ] Close Calculator, then "open calculator" and straight away "click seven in calculator" → pressed (it waits for the window)
- [ ] "click 7 in calculator", "click multiply", "click equal" → Seven, Multiply by, Equals
- [ ] "click sevn in calculator" → "did you mean 'Seven'?", nothing pressed
- [ ] In a **new, empty** Notepad tab, ask JARVIS to type a line into Notepad's 'Text editor' → dialog shows it; Approve → it's there. In a tab that already has text → refused, nothing replaced

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
- [x] Brain–Body Protocol milestones 1–2: HUD, typed input, push-to-talk, tray
- [x] BBP milestone 3: notifications and confirmations (dialog, voice, console; fail closed)
- [x] BBP milestone 4: clicking and typing in apps through UI Automation, approvals bound to the request
- [x] Safe desktop control through the Body (launch, focus, list, volume, `uia.read`)

### Next up
- [ ] **Phase G — coding partner** (the big "code my stuff" capability)
- [x] Phase E — consolidation: V1 (`legacy/`) and dead code retired; V2 kept as the
      stable fallback alongside V3 (snapshot tag `pre-retire-old-modules`)
- [ ] Phase H — benchmarks (`bench_stt`, `bench_routing`, `bench_e2e`) + demo video

### Small follow-ups
- [ ] The unnamed "something else" from the Phase F menu — clarify & do
- [ ] `ruff --fix` pass, then flip CI ruff from non-blocking to blocking
- [ ] XTTS voice cloning (deferred; needs a reference voice clip)
