namespace Bbp.Client;

/// <summary>Which element a desktop.uia.* request targets (amendment 19).</summary>
public sealed record UiElementRef(string? Name = null, string? ControlType = null, string? AutomationId = null);

/// <summary>The exact desktop request a confirmation approves (confirm.request
/// <c>action</c>, amendment 20).</summary>
public sealed record ConfirmAction(string Topic, string App, UiElementRef Element, string? Text = null)
{
    /// <summary>Same request: topic, app (case-insensitive id), element fields and text exactly.</summary>
    public bool SameAs(ConfirmAction other) =>
        Topic == other.Topic
        && string.Equals(App, other.App, StringComparison.OrdinalIgnoreCase)
        && Element == other.Element
        && Text == other.Text;
}

/// <summary>
/// The Body's own record of what you approved (BBP amendment 20). A
/// desktop.uia.invoke on a risky element, or any desktop.uia.set_text, runs
/// only with the confirm_id of a confirmation this Body saw approved
/// (confirm.resolved) for exactly that request, once, within
/// <see cref="UseWithin"/> of the approval. So a confused or compromised Brain
/// can't skip the dialog by sending the request directly.
///
/// Fed from the receive loop (events arrive in order before the request that
/// depends on them); thread-safe, because requests are handled on other threads.
/// </summary>
public sealed class ApprovalLedger(Func<DateTimeOffset>? clock = null)
{
    public static readonly TimeSpan UseWithin = TimeSpan.FromSeconds(30);

    /// <summary>How long a request waits for the confirm.resolved of its
    /// confirm_id: the Brain may queue it just after the request.</summary>
    public static readonly TimeSpan ResolutionWait = TimeSpan.FromSeconds(2);

    private static readonly TimeSpan Forget = TimeSpan.FromMinutes(10);

    private readonly Func<DateTimeOffset> _clock = clock ?? (() => DateTimeOffset.Now);
    private readonly object _gate = new();
    private readonly Dictionary<string, Entry> _entries = new();

    private sealed class Entry(ConfirmAction? action, DateTimeOffset seen)
    {
        public ConfirmAction? Action { get; set; } = action;
        public DateTimeOffset Seen { get; } = seen;
        public bool? Approved { get; set; }
        public DateTimeOffset ResolvedAt { get; set; }
        public bool Used { get; set; }
        public TaskCompletionSource Resolved { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    /// <summary>confirm.request / confirm.resolved; other topics are ignored.</summary>
    public void Apply(Envelope message)
    {
        switch (message.Topic)
        {
            case Topics.ConfirmRequest:
                var p = message.PayloadAs<PendingConfirmation>();
                Record(p.ConfirmId, p.Action);
                break;
            case Topics.ConfirmResolved:
                var r = message.PayloadAs<ConfirmResolvedPayload>();
                lock (_gate)
                {
                    if (!_entries.TryGetValue(r.ConfirmId, out var entry) || entry.Approved is not null)
                        return;                     // never saw the request: nothing to approve
                    entry.Approved = r.Approved;
                    entry.ResolvedAt = _clock();
                    entry.Resolved.TrySetResult();
                }
                break;
        }
    }

    /// <summary>A (re)connect: the snapshot's pending confirmations (with their actions).</summary>
    public void ApplyWelcome(WelcomePayload welcome)
    {
        foreach (var p in welcome.Snapshot.PendingConfirmations ?? [])
            Record(p.ConfirmId, p.Action);
    }

    /// <summary>The link dropped: nothing approved before it can be used after it.</summary>
    public void Clear()
    {
        lock (_gate)
        {
            foreach (var e in _entries.Values)
                e.Resolved.TrySetResult();
            _entries.Clear();
        }
    }

    /// <summary>Null when <paramref name="confirmId"/> approves exactly
    /// <paramref name="action"/> (and uses it up); otherwise why not.</summary>
    public async Task<string?> CheckAsync(string? confirmId, ConfirmAction action, CancellationToken ct = default)
    {
        if (string.IsNullOrEmpty(confirmId))
            return "you weren't asked";
        Entry? entry;
        lock (_gate)
            _entries.TryGetValue(confirmId, out entry);
        if (entry is null)
            return "this Body never saw that confirmation";
        if (entry.Approved is null)
        {
            try
            {
                await entry.Resolved.Task.WaitAsync(ResolutionWait, ct).ConfigureAwait(false);
            }
            catch (TimeoutException)
            {
                return "it hasn't been answered";
            }
        }
        lock (_gate)
        {
            if (!_entries.ContainsKey(confirmId))
                return "the link to JARVIS dropped since";
            if (entry.Approved is not true)
                return "you denied it";
            if (entry.Used)
                return "that approval was already used";
            if (_clock() - entry.ResolvedAt > UseWithin)
                return "that approval has expired";
            if (entry.Action is null || !entry.Action.SameAs(action))
                return "that approval was for something else";
            entry.Used = true;
            return null;
        }
    }

    private void Record(string confirmId, ConfirmAction? action)
    {
        lock (_gate)
        {
            var now = _clock();
            foreach (var old in _entries.Where(e => now - e.Value.Seen > Forget).Select(e => e.Key).ToList())
                _entries.Remove(old);
            if (_entries.TryGetValue(confirmId, out var known))
                known.Action ??= action;            // snapshot after the event: keep what we have
            else
                _entries[confirmId] = new Entry(action, now);
        }
    }
}
