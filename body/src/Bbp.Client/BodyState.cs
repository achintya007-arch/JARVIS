namespace Bbp.Client;

/// <summary>
/// What the HUD shows, rebuilt from the welcome snapshot and then updated by
/// events. Plain logic with no UI types, so it is unit-tested directly.
/// </summary>
public sealed class BodyState
{
    private readonly SortedDictionary<int, string> _chunks = new();

    public BodyState(Func<DateTime>? clock = null) => Activity = new ToolActivity(clock);

    public string VoiceState { get; private set; } = "idle";
    public bool Muted { get; private set; }
    public string Transcript { get; private set; } = "";

    /// <summary>True when <see cref="Transcript"/> is something you typed on the Body.</summary>
    public bool TranscriptTyped { get; private set; }
    public double? TranscriptConfidence { get; private set; }
    public string? TurnId { get; private set; }
    public bool ReplyDone { get; private set; }
    public bool ReplyInterrupted { get; private set; }
    public string Model => Agent?.Model ?? "";

    /// <summary>Model, fallback flag, mood, intent and load (welcome, then agent.state).</summary>
    public AgentSnapshot? Agent { get; private set; }

    /// <summary>The tool calls on the HUD (tool.started / tool.finished).</summary>
    public ToolActivity Activity { get; }

    /// <summary>The latest notify (reminder, timer, alert…): shown on screen, never spoken.</summary>
    public NotifyPayload? LastNotification { get; private set; }

    /// <summary>The streaming reply so far, in seq order.</summary>
    public string Reply => string.Join(" ", _chunks.Values);

    public void ApplyWelcome(WelcomePayload welcome)
    {
        VoiceState = welcome.Snapshot.Voice.State;
        Muted = welcome.Snapshot.Voice.Muted;
        Agent = welcome.Snapshot.Agent;
        Activity.Restore(welcome.Snapshot.RunningTools);
    }

    /// <summary>Apply one event; returns false for topics the HUD doesn't show.</summary>
    public bool Apply(Envelope message)
    {
        switch (message.Topic)
        {
            case Topics.VoiceState:
                var vs = message.PayloadAs<VoiceStatePayload>();
                VoiceState = vs.State;
                Muted = vs.Muted;
                return true;

            case Topics.VoiceTranscript:
                var tr = message.PayloadAs<TranscriptPayload>();
                Transcript = tr.Text;
                TranscriptConfidence = tr.Confidence;
                TranscriptTyped = false;
                ClearReply(null);       // a new utterance starts a new exchange
                return true;

            case Topics.ReplyChunk:
                var chunk = message.PayloadAs<ReplyChunkPayload>();
                if (chunk.TurnId != TurnId)
                    ClearReply(chunk.TurnId);
                _chunks[chunk.Seq] = chunk.Text;
                return true;

            case Topics.ReplyDone:
                var done = message.PayloadAs<ReplyDonePayload>();
                if (done.TurnId != TurnId)
                    ClearReply(done.TurnId);
                ReplyDone = true;
                ReplyInterrupted = done.Interrupted;
                return true;

            case Topics.Notify:
                LastNotification = message.PayloadAs<NotifyPayload>();
                return true;

            case Topics.ToolStarted:
                Activity.Started(message.PayloadAs<ToolStartedPayload>());
                return true;

            case Topics.ToolFinished:
                Activity.Finished(message.PayloadAs<ToolFinishedPayload>());
                return true;

            case Topics.AgentState:
                Agent = message.PayloadAs<AgentSnapshot>();
                return true;

            default:
                return false;
        }
    }

    /// <summary>You typed something (input.text): show it as the new exchange.
    /// The Brain sends no transcript for typed input.</summary>
    public void ApplyLocalInput(string text)
    {
        Transcript = text;
        TranscriptConfidence = null;
        TranscriptTyped = true;
        ClearReply(null);
    }

    private void ClearReply(string? turnId)
    {
        _chunks.Clear();
        TurnId = turnId;
        ReplyDone = false;
        ReplyInterrupted = false;
    }
}
