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
5. **Capabilities matter:** the Brain only routes a desktop tool to the Body if the Body declared that capability. Otherwise the tool reports itself unavailable.

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
| `confirm.request` | `confirm_id`, `prompt`, `risk` (low, high), `timeout_s` | Approve or deny dialog |
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
| `desktop.app.launch` | `app_id` (allowlisted name) | `pid` |
| `desktop.window.focus` | `app` or `title_contains` | `hwnd`, `title` |
| `desktop.window.list` | none | Visible windows: `title`, `app`, `hwnd` |
| `desktop.audio.set_volume` | `level` 0–100, optional `device` | Applied level |
| `desktop.uia.read` | `app`, `scope` (window or focused) | Trimmed UI tree: name, control type, automation ID |
| `desktop.uia.invoke` | `app`, `element` {`name`, `control_type`, `automation_id`} | `ok` |
| `desktop.uia.set_text` | `app`, `element`, `text` | `ok` |

A failure returns `{ "ok": false, "error": { "code", "message" } }`.

**Safety rules:**

- The Python tools in `agent_executor` keep their names and the Brain's allowlist, but become thin proxies that forward to the Body.
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

**Versioning:** adding fields or topics keeps `v: 1`, and receivers ignore fields they don't know. Removing or renaming anything bumps `v`. Unknown events are dropped silently; unknown requests get `E_UNKNOWN_TOPIC`.

## Example: "Hey Jarvis, focus VS Code"

1. Brain → `voice.state` `listening` after the wake word; the HUD ring lights up.
2. Brain → `voice.transcript` `{ "text": "focus VS Code", "final": true }`.
3. Brain → `voice.state` `processing`. The `FastRouter` matches it, so no LLM call.
4. Brain → `tool.started` `{ "tool": "focus_window", "summary": "Focusing VS Code" }`.
5. Brain → `request` `desktop.window.focus` `{ "app": "Code" }` with `id: A1`.
6. Body checks its allowlist, focuses the window, and replies `response` `{ "ok": true, "title": "JARVIS – Visual Studio Code" }` with `corr: A1`.
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

## Amendments (Oct 15, 2026)

Additive changes made while implementing milestones 1–3; `v` stays `1`.

1. **Session topics:** for `hello`, `welcome`, `ping` and `pong`, `topic` equals `kind`.
2. **Command errors:** an unacceptable command gets a `response` with `topic: "error"`, `corr` set to the command's `id`, and an `{ ok: false, error }` payload.
3. **`confirm.resolved`:** new event, broadcast when a confirmation is settled by any channel, so every Body can close its dialog.
4. **`reply.done.interrupted`:** true when the reply was cut off by barge-in, cancel, or an error mid-reply.
5. **`confirm.request.risk`:** derived from the gated tool name (see the confirmation note above).
6. **`voice.transcript.confidence`:** derived from Whisper's average log-probability; null when unavailable.
7. **`voice.state.muted`:** reflects `voice.control` mute/unmute (microphone off).
8. **Decisions:** `input.text` replies are HUD-only by default (`bbp.speak_text_input`); `ui.hotkey push_to_talk` maps to wake as tap-to-talk; `notify.action` is accepted and logged only in v0.1.
9. **Close code 4408 (approved):** the Brain closes a Body it has heard nothing from (no `ping` or any other message) for `heartbeat_timeout_s` with code 4408 "idle timeout". This mirrors the Body-side rule (three missed pongs). The Brain closes with 1000 when it shuts down. Body behaviour for every code is in the close-code table under "Connection lifecycle".

10. **Command refusals (proposed, pending approval):** a valid command the Brain can't act on right now is answered with an error response, like `E_UNKNOWN_TOPIC` and `E_BAD_PAYLOAD`:
    - `E_BUSY`: `input.text` or wake while a turn is already in progress.
    - `E_NOT_ALLOWED`: `voice.control` or `push_to_talk` while the Brain runs in text mode; wake or `push_to_talk` while the microphone is muted.
    - `E_NOT_FOUND`: `ui.hotkey` with a name that maps to nothing; `unmute` when no microphone can be opened.
11. **`voice.control` details (proposed, pending approval):**
    - `cancel` stops the current turn wherever it is (listening, thinking or speaking) and returns to idle. It interrupts like barge-in but doesn't listen again, because it comes from a button, not speech.
    - wake (or `push_to_talk`) while JARVIS is speaking is a barge-in: speech stops and JARVIS listens for the new command.
    - `mute` closes the microphone stream, so the OS microphone indicator goes off. A reply already being spoken finishes. The muted state survives a Body reconnect (it is in the `welcome` snapshot).

### Brain configuration (`config.yaml`, section `bbp`)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Start the BBP server with the Brain |
| `host` / `port` / `path` | `127.0.0.1` / `8765` / `/bbp` | Listen address |
| `token_path` | `data/bbp_token` | Shared token (created on first run, readable only by the current user, never committed) |
| `heartbeat_timeout_s` | `15` | Drop a Body that has sent nothing for this long |
| `confirm_timeout_s` | `15` | `confirm.request.timeout_s` |
| `speak_text_input` | `false` | Also speak replies to `input.text` |
