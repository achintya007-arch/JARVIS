# JARVIS Body (Windows HUD)

The Body is the Windows-side shell for JARVIS. It connects to the Brain over the
[Brain–Body Protocol](../docs/protocol/BBP_v0.1.md), a WebSocket on
`ws://127.0.0.1:8765/bbp`, and shows what the Brain is doing. The Brain never
depends on it: voice and text mode work the same with or without a Body attached.

**Milestone 1 (this version):** an always-on-top, transparent HUD showing the
connection status, the voice state (idle / listening / processing / speaking),
the last thing JARVIS heard, and the reply as it streams in.
The text box, tray icon, push-to-talk hotkey, notifications and confirmation
dialogs come in milestones 2 and 3.

## Layout

```
body/
├── Jarvis.Body.sln
├── src/Bbp.Client/        # protocol + networking (no UI, no Windows APIs)
│   ├── Envelope.cs        #   message envelope, JSON settings, constants
│   ├── Payloads.cs        #   typed payload records
│   ├── SchemaValidator.cs #   validates against ../protocol/*.schema.json
│   ├── BbpClient.cs       #   hello/welcome, 5 s ping, 3 missed pongs, reconnect
│   ├── ReconnectPolicy.cs #   0.5 s, 1 s, 2 s, then every 5 s
│   ├── BodyState.cs       #   what the HUD shows, built from events
│   ├── TokenLocator.cs    #   finds jarvis_v1/data/bbp_token
│   └── Transport.cs       #   ClientWebSocket behind a small interface
├── src/Jarvis.Body/       # WPF HUD (UI only)
└── tests/Bbp.Client.Tests # xunit: envelope, schemas, backoff, heartbeat, HUD state
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

Drag the HUD to move it. Right-click it to quit.

### Settings (optional)

Copy `body.settings.example.json` to `body.settings.json` next to
`Jarvis.Body.exe` to override the defaults:

```json
{ "url": "ws://127.0.0.1:8765/bbp", "token_path": "C:/path/to/jarvis_v1/data/bbp_token" }
```

The `JARVIS_BBP_TOKEN_PATH` environment variable also sets the token path.

## Status dot

| Dot | Meaning |
| --- | --- |
| Green | Connected; the Brain sent its welcome snapshot |
| Amber | Connecting |
| Grey | Brain not reachable; retrying (0.5 s, 1 s, 2 s, then every 5 s) |
| Red | Token rejected (close code 4401); check the token file |
