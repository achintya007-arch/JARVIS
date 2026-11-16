namespace Bbp.Client;

/// <summary>How a tool call ended, as the HUD shows it.</summary>
public enum ToolOutcome
{
    Running,
    Ok,         // ✓
    Denied,     // grey "declined": you said no, or it wasn't allowed
    Error,      // red ✗
    Cancelled,  // grey "stopped": the turn was stopped while it ran
}

public sealed record ActivityLine(string CallId, string Summary, ToolOutcome Outcome);

/// <summary>
/// The HUD's activity list (amendment 23): one line per tool call. A finished
/// line stays <see cref="HoldFinished"/>; a line that never finishes clears
/// itself <see cref="RunningTimeout"/> after the call started. Plain logic with
/// an injected clock, so it is unit-tested directly.
/// </summary>
public sealed class ToolActivity
{
    public static readonly TimeSpan HoldFinished = TimeSpan.FromSeconds(2);
    public static readonly TimeSpan RunningTimeout = TimeSpan.FromSeconds(30);
    public const int MaxLines = 6;

    private sealed class Entry
    {
        public required string CallId;
        public required string Summary;
        public ToolOutcome Outcome;
        public DateTime StartedAt;
        public DateTime FinishedAt;
    }

    private readonly Func<DateTime> _clock;
    private readonly List<Entry> _entries = new();     // oldest first

    public ToolActivity(Func<DateTime>? clock = null) => _clock = clock ?? (() => DateTime.UtcNow);

    public IReadOnlyList<ActivityLine> Lines =>
        _entries.Select(e => new ActivityLine(e.CallId, e.Summary, e.Outcome)).ToList();

    public bool IsEmpty => _entries.Count == 0;

    /// <summary>Goes up whenever the lines change, so a view can rebuild only then.</summary>
    public int Version { get; private set; }

    /// <summary>A Body that (re)connects: the calls still running replace whatever was shown.</summary>
    public void Restore(IReadOnlyList<RunningTool>? running)
    {
        _entries.Clear();
        var now = _clock();
        foreach (var r in running ?? [])
            Add(new Entry
            {
                CallId = r.CallId, Summary = r.Summary, Outcome = ToolOutcome.Running,
                StartedAt = now - TimeSpan.FromSeconds(Math.Max(0, r.ElapsedS)),
            });
        Prune();
        Version++;
    }

    public void Started(ToolStartedPayload p)
    {
        _entries.RemoveAll(e => e.CallId == p.CallId);
        Add(new Entry { CallId = p.CallId, Summary = p.Summary, Outcome = ToolOutcome.Running, StartedAt = _clock() });
        Version++;
    }

    public void Finished(ToolFinishedPayload p)
    {
        var now = _clock();
        var entry = _entries.Find(e => e.CallId == p.CallId);
        if (entry is null)
        {
            // Its tool.started came before this Body connected; show the result anyway.
            entry = new Entry { CallId = p.CallId, Summary = p.Summary, StartedAt = now };
            Add(entry);
        }
        entry.Summary = p.Summary;
        entry.Outcome = OutcomeOf(p);
        entry.FinishedAt = now;
        Version++;
    }

    /// <summary>Drop lines whose time is up; true when something changed.</summary>
    public bool Prune()
    {
        var now = _clock();
        var removed = _entries.RemoveAll(e => e.Outcome == ToolOutcome.Running
            ? now - e.StartedAt >= RunningTimeout
            : now - e.FinishedAt >= HoldFinished) > 0;
        if (removed)
            Version++;
        return removed;
    }

    public void Clear()
    {
        if (_entries.Count == 0)
            return;
        _entries.Clear();
        Version++;
    }

    public static ToolOutcome OutcomeOf(ToolFinishedPayload p) => p.Status switch
    {
        "ok" => ToolOutcome.Ok,
        "denied" => ToolOutcome.Denied,
        "cancelled" => ToolOutcome.Cancelled,
        "error" => ToolOutcome.Error,
        _ => p.Ok ? ToolOutcome.Ok : ToolOutcome.Error,     // older Brain, or a status we don't know
    };

    private void Add(Entry entry)
    {
        _entries.Add(entry);
        while (_entries.Count > MaxLines)
        {
            // Make room by dropping a finished line first, else the oldest.
            var finished = _entries.FindIndex(e => e.Outcome != ToolOutcome.Running);
            _entries.RemoveAt(finished >= 0 && finished < _entries.Count - 1 ? finished : 0);
        }
    }
}
