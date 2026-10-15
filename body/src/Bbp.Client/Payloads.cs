namespace Bbp.Client;

// Typed payloads for the milestone-1 topics. Field names map to the schema's
// snake_case through BbpJson.Options. Receivers ignore unknown fields, so a
// newer Brain adding a field never breaks these.

public sealed record HelloPayload(string Client, string Version, string Token, IReadOnlyList<string> Capabilities);

public sealed record WelcomePayload(string Session, string BrainVersion, Snapshot Snapshot);

public sealed record Snapshot(
    VoiceStatePayload Voice,
    AgentSnapshot Agent,
    IReadOnlyList<GoalSnapshot> ActiveGoals,
    IReadOnlyList<PendingConfirmation> PendingConfirmations);

public sealed record AgentSnapshot(string Mood, string Intent, string Model, LoadSnapshot Load);

public sealed record LoadSnapshot(double? RamPct, double? VramPct);

public sealed record GoalSnapshot(string Name, int Priority);

public sealed record PendingConfirmation(string ConfirmId, string Prompt, string Risk, double TimeoutS);

public sealed record VoiceStatePayload(string State, bool Muted);

public sealed record TranscriptPayload(string Text, bool Final, double? Confidence);

public sealed record ReplyChunkPayload(string TurnId, int Seq, string Text);

public sealed record ReplyDonePayload(string TurnId, bool Interrupted);

public sealed record ErrorInfo(string Code, string Message);

// Body → Brain commands (milestone 2)

public sealed record InputTextPayload(string Text);

/// <summary>action: wake, mute, unmute, cancel</summary>
public sealed record VoiceControlPayload(string Action);

public sealed record HotkeyPayload(string Name);

public sealed record ErrorPayload(bool Ok, ErrorInfo Error);

public static class Topics
{
    public const string VoiceState = "voice.state";
    public const string VoiceTranscript = "voice.transcript";
    public const string ReplyChunk = "reply.chunk";
    public const string ReplyDone = "reply.done";
    public const string Error = "error";
    public const string InputText = "input.text";
    public const string VoiceControl = "voice.control";
    public const string UiHotkey = "ui.hotkey";
}
