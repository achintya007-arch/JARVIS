# JARVIS Brain–Body Protocol v0.1

Oct 14, 2026 · Akella Ahlad Achintya
Amended Oct 15, 2026 (see [Amendments](#amendments-oct-15-2026)); all changes are additive, so `v` stays `1`.

JSON Schemas for the envelope, the session messages and every topic live in `protocol/` at the repo root and are the machine-readable form of this spec.

## Purpose and principles

JARVIS splits into a Python **Brain** (the existing `core_v3`) and a C# **Body** (a Windows shell), and they share nothing but this message protocol.

Events and action requests flow from Brain to Body; commands and responses flow back.

- **Brain owns cognition:** wake word, speech-to-text, LLM, memory, tool decisions, text-to-speech.
- **Body owns the desktop:** HUD overlay, tray icon, global hotkey, notifications, and native Windows actions (UI Automation, windows, audio, app launch).
- **Brain is the source of truth.** The Body renders state and executes requested actions; it never decides on its own.
- **Location-agnostic.** The same protocol works over localhost today and over the network later, when the Brain moves to a server.
- **Fail safe.** If the link drops, voice keeps working in the Brain, the HUD shows offline, and no desktop action runs without a reply from the Body.
- **Defense in depth.** The LLM's context can contain untrusted text, so both sides enforce allowlists independently.

## Transport, roles, auth

Use a WebSocket at `ws://127.0.0.1:8765/bbp`, with the Brain as server and the Body as client.

| Decision | Choice | Why |
| --- | --- | --- |
| Transport | WebSocket, JSON text frames, UTF-8 | Native in both Python and .NET, easy to debug, works remotely later as `wss://` |
| Server | Brain | Later, several bodies (laptop, phone, second PC) can connect to one brain |
| Client | Body | Reconnects with backoff: 0.5 s, 1 s, 2 s, then every 5 s |
| Bind | `127.0.0.1` only in v0.1 | Nothing outside the machine can connect |
| Auth | Shared token in the `hello` message | Token generated on first Brain run and saved to a local file both sides read |
| Binary frames | Reserved | For streaming audio to the Body later |

Named pipes would be slightly faster locally, but they can't cross machines, which blocks the move to a remote brain.

## Message envelope

Every message in either direction uses one envelope:

```json
{
  "v": 1,
  "id": "7f3c…",
  "kind": "event",
  "topic": "voice.state",
  "ts": "2026-10-14T01:10:00.123+05:30",
  "corr": null,
  "payload": { "state": "listening" }
}
```

| Field | Meaning |
| --- | --- |
| `v` | Protocol major version |
| `id` | Unique message ID (UUID) |
| `kind` | `event` (fire and forget), `command` (Body asks Brain), `request` (Brain asks Body to act), `response` (reply to a request), `hello` / `welcome` / `ping` / `pong` (session) |
| `topic` | Dotted name, such as `voice.state` or `desktop.window.focus` |
| `ts` | Sender timestamp, ISO 8601 |
| `corr` | For a `response`, the `id` of the request (or command) it answers; otherwise null |
| `payload` | Topic-specific object |

For the session kinds (`hello`, `welcome`, `ping`, `pong`), `topic` equals `kind`.

## Connection lifecycle

The Body says hello, the Brain answers with a full state snapshot, and heartbeats detect a dead link within 15 s.

1. **Body → `hello`:** `{ "client": "win-shell", "version": "0.1.0", "token": "…", "capabilities": ["hud", "notify", "hotkey", "uia", "audio", "apps"] }`
2. **Brain → `welcome`:** `{ "session": "…", "brain_version": "3.x", "snapshot": { voice state, agent state, active goals, pending confirmations } }`. A wrong token closes the socket with code 4401.
3. **Heartbeat:** the Body sends `ping` every 5 s; the Brain answers `pong`. Three missed pongs means disconnected.
4. **Reconnect:** the Body retries with backoff, and each new `welcome` snapshot lets the HUD redraw instantly without replaying history.
5. **Capabilities matter:** the Brain only routes a desktop tool to the Body if the Body declared that capability. Otherwise the tool runs its Python version, or, with `bbp.desktop_fallback: false`, reports itself unavailable. `desktop.uia.read` has no Python version.

**Close codes:**

| Code | Meaning | Body behaviour |
| --- | --- | --- |
| 1000 | Normal close (brain shutting down) | Reconnect with normal backoff |
| 4401 | Bad or missing token | Stop retrying; show "auth failed, check token" on the HUD and tray |
| 4408 | Idle timeout (no message for 15 s) | Reconnect immediately, then normal backoff |
| Anything else / network drop | Unexpected | Reconnect with normal backoff |

After a 4401 the Body makes no further attempts with the rejected token. It tries again only when the token file holds a different token, or when you choose Reconnect from the tray. Each session that reached `welcome` earns one immediate retry after a 4408.

## Brain → Body events

Events mirror what already flows on the `EventBus`, so the server is a thin adapter like the existing ones.

| Topic | Payload | HUD use |
| --- | --- | --- |
| `voice.state` | `state`: idle, listening, processing, speaking, interrupted; `muted` (bool, microphone off) | Orb or ring animation; muted badge |
| `voice.transcript` | `text`, `final` (bool), `confidence` (0–1 or null) | Live caption of what you said |
| `reply.chunk` | `turn_id`, `seq`, `text` | Reply streams in sentence by sentence |
| `reply.done` | `turn_id`, `interrupted` (bool) | Closes the reply card; `interrupted` is true if the reply was cut off (barge-in, cancel, or an error mid-reply) |
| `tool.started` | `call_id`, `tool`, `summary` | "Opening VS Code…" |
| `tool.finished` | `call_id`, `ok`, `summary` | Tick or error badge |
| `agent.state` | `mood`, `intent`, `model`, `load` (VRAM %, RAM %) | Status strip; shows fallback-model downshifts |
| `notify` | `notify_id`, `level` (info, warn, alert), `title`, `body`, `actions[]` | Windows toast plus HUD card |
| `confirm.request` | `confirm_id`, `prompt`, `risk` (low, high), `timeout_s`, optional `detail` (amendment 17) | Approve or deny dialog |
| `confirm.resolved` | `confirm_id`, `approved` (bool), `by` (body, voice, text, timeout, error) | Closes the dialog on every Body, whoever answered |
| `sentinel.finding` | Reserved for v0.2 | Intelligence alerts |

`confirm.request` carries over your existing rule: no answer before `timeout_s` means **deny**. The default `timeout_s` is 15 (configurable as `bbp.confirm_timeout_s`). `risk` comes from the gated tool: `run_shell` and `close_app` are high; `write_file` and `close_window` are low. While a confirmation is pending, voice/text and every connected Body race to answer; the first answer wins and `confirm.resolved` is broadcast.

`voice.transcript` is always `final: true` in v0.1 (Whisper does not stream partial results). `confidence` is derived from Whisper's average log-probability over the kept segments.

## Body → Brain commands

Commands are what you do in the shell: type, press a hotkey, click a button.

| Topic | Payload | Brain does |
| --- | --- | --- |
| `input.text` | `text` | Same path as typed input in text mode. The reply streams to the HUD as `reply.*` and is **not** spoken unless `bbp.speak_text_input` is true |
| `voice.control` | `action`: wake, mute, unmute, cancel | Wake skips the wake word; mute turns the **microphone** off (wake word and listening; typed input still works); cancel triggers barge-in |
| `confirm.response` | `confirm_id`, `approved` | Resolves the pending gated action |
| `notify.action` | `notify_id`, `action` | Runs the button's action, such as snooze or open. In v0.1 `actions[]` is always empty and this is accepted and logged only |
| `ui.hotkey` | `name` | Maps to a configured intent. `push_to_talk` maps to wake (tap-to-talk: Win32 `RegisterHotKey` has no key-up event, so the endpointer decides when you've finished) |

A command the Brain can't accept is answered with a `response` whose `topic` is `error`, whose `corr` is the command's `id`, and whose payload is `{ "ok": false, "error": { "code", "message" } }` (`E_UNKNOWN_TOPIC` or `E_BAD_PAYLOAD`). Accepted commands get no response.

## Desktop actions and safety

The Brain sends a `request`, the Body acts natively and replies with a `response` whose `corr` matches. Default timeout is 5 s.

| Topic | Payload | Response `result` |
| --- | --- | --- |
| `desktop.app.launch` | `app_id` (allowlisted name) | `pid`, `app_id` |
| `desktop.window.focus` | `app` or `title_contains` | `hwnd`, `title`, `app`, optional `strategy` (how the foreground lock was passed) |
| `desktop.window.list` | none | Visible windows: `title`, `app`, `hwnd`, `allowed` |
| `desktop.audio.set_volume` | `level` 0–100 **or** `delta` −100…100, optional `device` (accepted but ignored in v0.1: always the default output) | `level`, `muted` |
| `desktop.uia.read` | optional `app` (default: the foreground window's app), `scope` (window or focused) | Trimmed UI tree: name, control type, automation ID |
| `desktop.uia.invoke` | `app`, `element` {`name`, `control_type`, `automation_id`}, `confirm_id` when risky (amendments 19–20) | `app`, `name`, `control_type`, `automation_id`, `pattern` |
| `desktop.uia.set_text` | `app`, `element`, `text`, `confirm_id` (amendments 19–20) | `app`, `name`, `control_type`, `automation_id`, `length` |

Success returns `{ "ok": true, "result": { … } }` and a failure returns `{ "ok": false, "error": { "code", "message" } }` (amendment 14).

**Safety rules:**

- The Python tools in `agent_executor` keep their names and the Brain's allowlist, and forward to the Body when one with the capability is connected. Without one they fall back to their Python implementation, unless `bbp.desktop_fallback` is false.
- The Body keeps its **own** allowlist of apps and control types, so a compromised or confused Brain still can't drive arbitrary software.
- `uia.invoke` on any element whose name looks destructive or outbound (send, delete, submit, pay, post) requires a `confirm.request` first, approved by you.
- `uia.read` trims passwords and secure fields before sending anything to the Brain.
- Text read back from apps is untrusted data; the Brain never treats it as instructions.

## Errors and versioning

| Code | Meaning |
| --- | --- |
| `E_AUTH` | Bad or missing token (socket closes, 4401) |
| `E_UNKNOWN_TOPIC` | Receiver doesn't handle this topic |
| `E_BAD_PAYLOAD` | Payload failed schema validation |
| `E_NOT_ALLOWED` | Blocked by an allowlist |
| `E_DENIED` | You declined, or the confirmation timed out |
| `E_NOT_FOUND` | App, window or UI element not found |
| `E_TIMEOUT` | No response in time |
| `E_BUSY` | Receiver is mid-action; retry later |
| `E_INTERNAL` | Receiver failed unexpectedly (amendment 14) |

**Versioning:** adding fields or topics keeps `v: 1`, and receivers ignore fields they don't know. Removing or renaming anything bumps `v`. Unknown events are dropped silently; unknown requests get `E_UNKNOWN_TOPIC`.

## Example: "Hey Jarvis, focus VS Code"

1. Brain → `voice.state` `listening` after the wake word; the HUD ring lights up.
2. Brain → `voice.transcript` `{ "text": "focus VS Code", "final": true }`.
3. Brain → `voice.state` `processing`. The `FastRouter` matches it, so no LLM call.
4. Brain → `tool.started` `{ "tool": "focus_window", "summary": "Focusing VS Code" }`.
5. Brain → `request` `desktop.window.focus` `{ "app": "vscode" }` with `id: A1`.
6. Body checks its allowlist, focuses the window, and replies `response` `desktop.window.focus` `{ "ok": true, "result": { "hwnd": 1314, "title": "JARVIS – Visual Studio Code", "app": "vscode" } }` with `corr: A1`.
7. Brain → `tool.finished` `ok`, then `reply.chunk` "Done.", then `voice.state` `speaking`, then `idle`.

## Implementation and build order

Build read-only first, then input, then confirmations, and only then let the Body touch the desktop.

**Brain side (Python):**

- New adapter, for example `core_v3/adapters/bbp_server.py`, using the `websockets` library and subscribing to the `EventBus` like your existing adapters.
- A small `RemoteDesktop` client that tools call; it sends a `request` and awaits the matching `response`.
- Contract tests in `tests/` that validate every topic against the schemas.

**Body side (C#):**

- .NET 8 with WPF (simplest for a transparent, always-on-top HUD) or WinUI 3 (more modern look).
- `ClientWebSocket` plus `System.Text.Json` for the link.
- FlaUI (a UI Automation wrapper) for the `desktop.uia.*` actions.
- A tray icon and a global hotkey registered through Win32 `RegisterHotKey`.

**Shared:** put JSON Schema files for every topic in a `protocol/` folder at the repo root. Both sides validate against them, and they act as the living spec.

**Build order:**

1. Brain server plus `voice.state`, `voice.transcript` and `reply.*`; Body shows them in a bare HUD.
2. `input.text` and `voice.control` from the Body, plus the push-to-talk hotkey.
3. `notify` and the `confirm.request` round trip.
4. `desktop.window.*` and `desktop.app.launch`; migrate one Python tool and compare reliability.
5. `desktop.uia.*` with the Body-side allowlist and confirmations.
6. Later: `sentinel.finding`, then `wss://` with per-device keys when the Brain moves off your PC.

**Order actually followed (changed Oct 2026):** 1 and 2, then the read-only and low-risk half of 4 and 5 (`desktop.app.launch`, `desktop.window.*`, `desktop.audio.set_volume`, `desktop.uia.read`; none of them clicks or types into an app), then 3, then `desktop.uia.invoke` and `desktop.uia.set_text`, which need confirmations.

## Amendments (Oct 15, 2026)

Additive changes made while implementing milestones 1–4 and their follow-ups; `v` stays `1`.

1. **Session topics:** for `hello`, `welcome`, `ping` and `pong`, `topic` equals `kind`.
2. **Command errors:** an unacceptable command gets a `response` with `topic: "error"`, `corr` set to the command's `id`, and an `{ ok: false, error }` payload.
3. **`confirm.resolved`:** new event, broadcast when a confirmation is settled by any channel, so every Body can close its dialog.
4. **`reply.done.interrupted`:** true when the reply was cut off by barge-in, cancel, or an error mid-reply.
5. **`confirm.request.risk`:** derived from the gated tool name (see the confirmation note above).
6. **`voice.transcript.confidence`:** derived from Whisper's average log-probability; null when unavailable.
7. **`voice.state.muted`:** reflects `voice.control` mute/unmute (microphone off).
8. **Decisions:** `input.text` replies are HUD-only by default (`bbp.speak_text_input`); `ui.hotkey push_to_talk` maps to wake as tap-to-talk; `notify.action` is accepted and logged only in v0.1.
9. **Close code 4408 (approved):** the Brain closes a Body it has heard nothing from (no `ping` or any other message) for `heartbeat_timeout_s` with code 4408 "idle timeout". This mirrors the Body-side rule (three missed pongs). The Brain closes with 1000 when it shuts down. Body behaviour for every code is in the close-code table under "Connection lifecycle".

10. **Command refusals (approved):** a valid command the Brain can't act on right now is answered with an error response, like `E_UNKNOWN_TOPIC` and `E_BAD_PAYLOAD`:
    - `E_BUSY`: `input.text` or wake while a turn is already in progress.
    - `E_NOT_ALLOWED`: `voice.control` or `push_to_talk` while the Brain runs in text mode; wake or `push_to_talk` while the microphone is muted.
    - `E_NOT_FOUND`: `ui.hotkey` with a name that maps to nothing; `unmute` when no microphone can be opened.
11. **`voice.control` details (approved):**
    - `cancel` stops the current turn wherever it is (listening, thinking or speaking) and returns to idle. It interrupts like barge-in but doesn't listen again, because it comes from a button, not speech.
    - wake (or `push_to_talk`) while JARVIS is speaking is a barge-in: speech stops and JARVIS listens for the new command.
    - `mute` closes the microphone stream, so the OS microphone indicator goes off. A reply already being spoken finishes. The muted state survives a Body reconnect (it is in the `welcome` snapshot).

12. **`desktop.audio.set_volume` delta (approved):** the request carries exactly one of `level` (0–100) or `delta` (−100…100, relative to the current level, clamped to 0–100). The result is `{ level, muted }`. Reading the volume and muting stay on the Brain's Python path for now.
13. **`desktop.uia.read` scope and limits (approved):**
    - `app` is optional and defaults to the app owning the foreground window. Either way the app must be on the Body's allowlist (the same list as launch and focus); otherwise the answer is `E_NOT_ALLOWED` and the message names the app, so JARVIS can say which app it may not read.
    - Password and other secure fields (`IsPassword`) are always removed, with their subtrees.
    - The tree is capped (default depth 12, 300 nodes, names cut to 200 characters). `result` is `{ app, title, root, node_count, truncated }`, where each node is `{ name, control_type, automation_id, children }` and empty fields are omitted.
14. **Desktop responses (approved, milestone 4):** a successful request is answered by a `response` on the **same topic** with payload `{ ok: true, result }`, validated against `protocol/topics/<topic>.response.schema.json`. A failed one is answered like a refused command: a `response` with topic `error`, `corr` set to the request `id`, and `{ ok: false, error }` (codes from the table above, plus `E_TIMEOUT` when the Brain gives up after 5 s, and a new `E_INTERNAL` for an unexpected failure on the receiving side).
15. **Desktop capabilities and the Body allowlist (approved, milestone 4):** `apps` covers `desktop.app.launch` and `desktop.window.*`, `audio` covers `desktop.audio.*`, and `uia` covers `desktop.uia.*`. The Brain sends a desktop request only to a Body that declared the capability. The Body's allowlist (`desktop.allowlist.json`, next to the Body executable) names the apps it may launch, focus or read; `desktop.window.list` lists every real window but marks each with `allowed`.

16. **Confirmation answers (approved, milestone 3):**
    - `confirm.response` with a `confirm_id` that isn't pending (unknown, already answered, or timed out) is refused with `E_NOT_FOUND`. The first answer wins; later ones change nothing.
    - Who can answer: the dialog on any Body (`by: body`), your voice (`by: voice`), and the Brain's own console in text mode (`by: text`). Typed input through BBP (`input.text`, from the HUD box, `tools/jarvis_send` or a phone button) never answers a confirmation: while one is pending, `input.text` is refused with `E_BUSY`.
    - With the microphone muted, voice doesn't answer; the dialog (or the console in text mode) is the only way.
    - Fail closed: the deadline runs from when the request is sent, for every channel. No answer by `timeout_s` resolves `by: timeout`, denied. A failure while asking, a cancelled turn, or a HUD Body (capability `hud`) disconnecting while a confirmation is pending resolves `by: error`, denied.
17. **`confirm.request.detail` (approved, milestone 3):** an optional string naming exactly what would run: the shell command, the file path (and size), the app, or the window title. It can come from the LLM, so control and bidirectional-override characters are removed, it's cut to 200 characters, and the Body shows it as plain text. `pending_confirmations` items in the `welcome` snapshot carry it too, with `timeout_s` set to the time left.
18. **What becomes a `notify` (approved, milestone 3):** the Brain keeps speaking these as before (except suggestions, which V3 doesn't speak today); the Body shows them on screen (a HUD card and a Windows notification) and never speaks them.

    | Source | `level` | `title` |
    | --- | --- | --- |
    | Goal suggestion ("shall I open Spotify?") | `info` | Suggestion |
    | Goal reminder | `warn` | Reminder |
    | Timer finished | `warn` | Timer |
    | Resource pressure starts (model switch) | `alert` | Resource pressure |
    | Resource pressure clears | `info` | Resources |

19. **`desktop.uia.invoke` and `desktop.uia.set_text` (approved, milestone 4):**
    - `app` is required: an id on the Body's allowlist whose entry also has `"act": true`. Reading an app doesn't allow acting in it; the shipped list allows acting only in Notepad and Calculator. Otherwise `E_NOT_ALLOWED`. When you don't name an app, the Brain asks `desktop.uia.read` which app is in front, so the confirmation can name it, and sends that id.
    - `element` has `name` (case-insensitive; amendment 22 adds a careful fallback when no name matches exactly) and/or `automation_id` (exact); `control_type` narrows it. The Body searches only that app's own windows. Exactly one element must match: none, or more than one, is `E_NOT_FOUND`, and the message says how many match so JARVIS can ask which. Nothing is guessed.
    - Refused with `E_NOT_ALLOWED`: password and other secure fields, disabled elements, read-only fields, and control types outside the Body's lists. Invoke: Button, MenuItem, Hyperlink, TabItem, ListItem, TreeItem, CheckBox, RadioButton. Set text: Edit, and Document only while it is empty (amendment 22).
    - Invoke uses UI Automation patterns only: Toggle first for a checkbox; SelectionItem first for list, tree and tab items and radio buttons (Invoke on a list item would *open* it); Invoke first for everything else. An element with none of them is `E_NOT_ALLOWED`: the Body never moves the mouse or sends keys. The result names the element and the `pattern` used (`invoke`, `toggle`, `select`).
    - `set_text` replaces the field's text through the Value pattern and never presses Enter. `text` is at most 2000 characters, and may contain no control characters other than tab and newline, and no bidirectional or zero-width characters (`E_BAD_PAYLOAD`), so the dialog shows exactly what is typed.
20. **Approvals bound to the request (approved, milestone 4):**
    - `desktop.uia.set_text` always needs approval (risk `low`). `desktop.uia.invoke` needs approval (risk `high`) when the element's name or automation id looks destructive or outbound. The words are in [`protocol/policy/risky-words.json`](../../protocol/policy/risky-words.json), shared by the Brain and the Body, together with examples both test suites check. Matching ignores case, full-width letters, accents, zero-width characters and common Cyrillic/Greek look-alikes, splits camelCase, and accepts the endings -s, -es, -ed, -d and -ing. A word mixing Latin with other letters counts as risky. If the list can't be loaded, everything is risky.
    - `confirm.request` gains an optional `action` `{ topic, app, element, text? }`: the exact desktop request it approves. `welcome` `pending_confirmations` items carry it too. The Body shows `action.text` in full in the dialog.
    - The desktop request carries `confirm_id`. The Body runs it only if it saw that `confirm.request` and then a `confirm.resolved` with `approved: true`, the action matches exactly (topic, app id, element fields and text), the approval is less than 30 s old, and it hasn't been used. Otherwise `E_DENIED`, with the reason. If the `confirm.resolved` hasn't arrived yet, the Body waits up to 2 s for it, because the Brain may queue it just after the request. Approvals are forgotten when the link drops.
    - The Body checks the element it actually finds, not just what the Brain asked for. If that element looks risky and the request has no valid approval, the answer is `E_DENIED`. The Brain then asks you once, saying why, and retries with the new `confirm_id`.
21. **Brain tools (approved, milestone 4):** `click_element` and `set_element_text` (arguments `app?`, `name`, `control_type?`, `automation_id?`, and `text`) map to the two requests. They work only through a connected Body; there is no Python fallback. They are off when `agent.screen_enabled` is false. "click X", "press the X button", "click X in <app>" and similar phrases are routed to `click_element` without the LLM. Key names ("press enter"), coordinates and media words stay with the existing tools. A declined or refused approval reads "I didn't do it, Sir: …".
22. **Finding the element reliably (approved, after milestone 4):**
    - **Waiting:** when the app has no window yet, or nothing matches, the Body looks again every 250 ms for up to 2 s, so "open calculator" followed at once by "click seven" works.
    - **Several exact matches:** the Body keeps only those of a type it acts on, then the enabled ones, then the ones on screen, stopping when one is left. Still more than one is `E_NOT_FOUND`, and the message lists them (name, type, automation id). Automation ids made only of digits (WinForms numbers unnamed controls by window handle) don't count as telling elements apart; when nothing does, the message says the Body won't guess.
    - **No exact name match:** names are compared as words, ignoring case, punctuation, accents and camelCase, with a single digit equal to its word ("7" = "Seven") and a plural -s dropped. The first rule with any match decides: (1) the same words; (2) the element's name starts with them ("multiply" = "Multiply by"); (3) the element's name contains all of them ("root" = "Square root"). The Body acts only when that rule matches exactly one element. It never acts this way on an element that looks risky: that is `E_NOT_FOUND` naming the element, so you say its full name and approve that. An `automation_id` is never matched loosely.
    - **Not found:** the message suggests up to three names spelled within a third of the query's length (a swap of two letters counts as one edit): "did you mean 'Send'?". A suggestion is never pressed.
    - **Typing into a document:** `set_text` may fill a `Document` (Windows 11 Notepad's editor) only when the Value pattern reports it empty, because setting the value replaces everything; otherwise `E_NOT_ALLOWED`.
    - **Brain:** `desktop.uia.invoke` and `desktop.uia.set_text` wait up to 15 s (or `request_timeout_s`, if longer) for the Body. On `E_TIMEOUT` JARVIS says it can't tell whether it was done. `E_NOT_FOUND`, `E_TIMEOUT` and `E_BAD_PAYLOAD` read "I couldn't do that, Sir: …".

### Brain configuration (`config.yaml`, section `bbp`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Start the BBP server with the Brain |
| `host` / `port` / `path` | `127.0.0.1` / `8765` / `/bbp` | Listen address |
| `token_path` | `data/bbp_token` | Shared token (created on first run, readable only by the current user, never committed) |
| `heartbeat_timeout_s` | `15` | Drop a Body that has sent nothing for this long |
| `confirm_timeout_s` | `15` | `confirm.request.timeout_s` |
| `speak_text_input` | `false` | Also speak replies to `input.text` |
| `desktop_fallback` | `true` | No Body with the capability connected → run the desktop tool in Python instead of refusing |
| `request_timeout_s` | `5` | How long the Brain waits for a desktop `response` before `E_TIMEOUT` |
