# JARVIS Body (Windows HUD)

The Body is the Windows-side shell for JARVIS. It connects to the Brain over the
[Brain–Body Protocol](../docs/protocol/BBP_v0.1.md), a WebSocket on
`ws://127.0.0.1:8765/bbp`, and shows what the Brain is doing. The Brain never
depends on it: voice and text mode work the same with or without a Body attached.

**What it does (milestones 1–2):**

- **HUD:** an always-on-top, transparent panel. It shows the connection, the
  voice state (idle / listening / processing / speaking, plus "muted"), what
  JARVIS heard, and the reply as it streams in.
- **Type to JARVIS:** the text box at the bottom of the HUD. Press Enter to
  send. The reply appears on the HUD and isn't spoken, unless the Brain sets
  `bbp.speak_text_input: true`. Typing still works while the mic is muted.
- **Push-to-talk:** Ctrl+Alt+J (configurable) works anywhere in Windows, like
  saying "hey jarvis". It's a tap, not a hold: JARVIS listens until you stop
  talking. Pressed while JARVIS is speaking, it interrupts him and listens.
- **Tray icon:** shows the state at a glance. Right-click it to mute or unmute
  the microphone, show or hide the HUD, reconnect, or quit.

Notifications and confirmation dialogs come in milestone 3.

## Layout

```
body/
├── Jarvis.Body.sln
├── src/Bbp.Client/        # protocol + networking (no UI, no Windows APIs)
│   ├── Envelope.cs        #   message envelope, JSON settings, constants
│   ├── Payloads.cs        #   typed payload records (events and commands)
│   ├── SchemaValidator.cs #   validates against ../protocol/*.schema.json
│   ├── BbpClient.cs       #   hello/welcome, ping, close-code handling, commands
│   ├── ReconnectPolicy.cs #   0.5 s, 1 s, 2 s, then every 5 s
│   ├── BodyState.cs       #   what the HUD shows, built from events
│   ├── TokenLocator.cs    #   finds jarvis_v1/data/bbp_token
│   └── Transport.cs       #   ClientWebSocket behind a small interface
├── src/Jarvis.Body/       # WPF HUD (UI only)
│   ├── MainWindow.xaml    #   the HUD
│   ├── TrayIcon.cs        #   notification-area icon and menu
│   ├── GlobalHotkey.cs    #   RegisterHotKey + "Ctrl+Alt+J" parsing
│   └── BodyLog.cs         #   %LOCALAPPDATA%\JARVIS\body.log
└── tests/
    ├── Bbp.Client.Tests   # envelope, schemas, backoff, close codes, commands, HUD state
    └── Jarvis.Body.Tests  # hotkey parsing and conflicts, tray status
```

The JSON Schemas in the repo-root [`protocol/`](../protocol/) folder are copied
next to the binaries at build time. The Body validates every message it sends
and receives against them, the same files the Brain uses.

## Build and run

Needs the .NET 8 SDK or newer. The projects target .NET 8 with
`RollForward=Major`, so a machine that only has .NET 9 runs them too.

```bash
cd body
dotnet build
dotnet test
dotnet run --project src/Jarvis.Body
```

1. Enable the server on the Brain side. In `jarvis_v1/config.yaml`:

   ```yaml
   bbp:
     enabled: true
   ```

2. Start JARVIS (`.\run.ps1` or `.\run.ps1 -Text`). On first run the Brain
   creates `jarvis_v1/data/bbp_token`, which only your Windows user can read.
3. Start the Body. It finds the token by searching the folders above its
   executable for `jarvis_v1/data/bbp_token`. You can start it before the Brain:
   it keeps retrying until the Brain is up.

Drag the HUD to move it. Right-click it to hide it or quit. Double-click the
tray icon to bring it back.

In text mode (`.\run.ps1 -Text`) there is no microphone, so mute and
push-to-talk are refused with a short notice on the HUD; typing works.

### Settings (optional)

Copy `body.settings.example.json` to `body.settings.json` next to
`Jarvis.Body.exe` to override the defaults:

```json
{
  "url": "ws://127.0.0.1:8765/bbp",
  "token_path": "C:/path/to/jarvis_v1/data/bbp_token",
  "hotkey": "Ctrl+Alt+J"
}
```

`hotkey` takes Ctrl, Alt, Shift and Win plus one key (A–Z, 0–9, F1–F24 or
Space), and needs at least one of Ctrl, Alt or Win. If another app already owns
the combination, the Body says so once on the HUD, writes it to the log, and
runs without push-to-talk. The `JARVIS_BBP_TOKEN_PATH` environment variable
also sets the token path.

The log is at `%LOCALAPPDATA%\JARVIS\body.log`.

## Status at a glance

| HUD dot / tray icon | Meaning |
| --- | --- |
| Green | Connected |
| Amber (tray: with a slash) | Connected, microphone muted |
| Grey | Brain not reachable; retrying (0.5 s, 1 s, 2 s, then every 5 s) |
| Red (tray: with "!") | Token rejected (4401): "auth failed, check token" |

How the Body reacts to each close code:

| Code | Meaning | Body behaviour |
| --- | --- | --- |
| 1000 | Brain shutting down | Reconnect with normal backoff |
| 4401 | Bad or missing token | Stop retrying until the token file changes or you choose **Reconnect** in the tray |
| 4408 | Idle timeout | Reconnect immediately, then normal backoff |
| Anything else / network drop | Unexpected | Reconnect with normal backoff |
