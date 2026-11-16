using System.Text.Json.Serialization;

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
    IReadOnlyList<PendingConfirmation> PendingConfirmations,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    IReadOnlyList<RunningTool>? RunningTools = null);

/// <summary>The agent status, in the welcome snapshot and as the agent.state
/// event (milestone 5). Fallback: JARVIS has dropped to its lighter model.</summary>
public sealed record AgentSnapshot(string Mood, string Intent, string Model, LoadSnapshot Load, bool Fallback = false);

/// <summary>A tool call still running when the welcome was sent (amendment 23).</summary>
public sealed record RunningTool(string CallId, string Tool, string Summary, double ElapsedS = 0);

public sealed record LoadSnapshot(double? RamPct, double? VramPct);

public sealed record GoalSnapshot(string Name, int Priority);

/// <summary>A confirm.request still waiting; in the welcome snapshot TimeoutS is the time left.</summary>
public sealed record PendingConfirmation(string ConfirmId, string Prompt, string Risk, double TimeoutS, string? Detail = null,
                                         ConfirmAction? Action = null);

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

// Milestone 3: notifications and confirmations

public sealed record NotifyAction(string Action, string Label);

/// <summary>level: info, warn, alert. Actions is always empty in v0.1.</summary>
public sealed record NotifyPayload(string NotifyId, string Level, string Title, string Body, IReadOnlyList<NotifyAction> Actions);

/// <summary>by: body, voice, text, timeout, error.</summary>
public sealed record ConfirmResolvedPayload(string ConfirmId, bool Approved, string By);

public sealed record ConfirmResponsePayload(string ConfirmId, bool Approved);

public sealed record NotifyActionPayload(string NotifyId, string Action);

public sealed record ErrorPayload(bool Ok, ErrorInfo Error);

// Milestone 5: live activity (amendment 23)

public sealed record ToolStartedPayload(string CallId, string Tool, string Summary);

/// <summary>status: ok, denied, error, cancelled; absent from an older Brain.</summary>
public sealed record ToolFinishedPayload(
    string CallId, bool Ok, string Summary,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Status = null);

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
    public const string Notify = "notify";
    public const string NotifyAction = "notify.action";
    public const string ConfirmRequest = "confirm.request";
    public const string ConfirmResolved = "confirm.resolved";
    public const string ConfirmResponse = "confirm.response";
    public const string ToolStarted = "tool.started";
    public const string ToolFinished = "tool.finished";
    public const string AgentState = "agent.state";
}
