namespace Bbp.Client;

/// <summary>One confirmation the Body is showing a dialog for.</summary>
public sealed record ConfirmationDialog(
    string ConfirmId,
    string Prompt,
    string Detail,
    string Risk,
    double TimeoutS,
    DateTimeOffset Deadline)
{
    /// <summary>run_shell and close_app: Approve takes a mouse click, never a key.</summary>
    public bool HighRisk => Risk == "high";

    public TimeSpan Remaining(DateTimeOffset now) =>
        Deadline > now ? Deadline - now : TimeSpan.Zero;

    /// <summary>1 when it arrives, 0 at the deadline: the countdown bar.</summary>
    public double FractionLeft(DateTimeOffset now) =>
        TimeoutS <= 0 ? 0 : Math.Clamp(Remaining(now).TotalSeconds / TimeoutS, 0, 1);
}

/// <summary>Why a dialog closed.</summary>
public enum DialogClose
{
    /// <summary>confirm.resolved: someone answered (here, another Body, voice, the console) or it timed out.</summary>
    Resolved,
    /// <summary>The deadline passed without a confirm.resolved (the Brain denies it anyway).</summary>
    Expired,
    /// <summary>The link to the Brain dropped: this Body can't answer any more.</summary>
    Disconnected,
}

/// <summary>
/// Which confirmations need a dialog (BBP milestone 3): opened by
/// confirm.request or the welcome snapshot, closed by confirm.resolved, the
/// deadline, or a disconnect. Plain logic, no UI, so it is unit-tested directly.
/// Not thread-safe: use it from one thread (the UI thread).
/// </summary>
public sealed class ConfirmationTracker(Func<DateTimeOffset>? clock = null)
{
    /// <summary>Close an expired dialog this long after its deadline, so the
    /// Brain's own confirm.resolved (timeout) normally closes it first.</summary>
    public static readonly TimeSpan ExpiryGrace = TimeSpan.FromSeconds(1.5);

    private readonly Func<DateTimeOffset> _clock = clock ?? (() => DateTimeOffset.Now);
    private readonly Dictionary<string, ConfirmationDialog> _open = new();

    public event Action<ConfirmationDialog>? Opened;

    /// <summary>(confirm_id, why, the resolution if there was one)</summary>
    public event Action<string, DialogClose, ConfirmResolvedPayload?>? Closed;

    public IReadOnlyCollection<ConfirmationDialog> Open => _open.Values;

    public DateTimeOffset Now => _clock();

    /// <summary>confirm.request / confirm.resolved; false for other topics.</summary>
    public bool Apply(Envelope message)
    {
        switch (message.Topic)
        {
            case Topics.ConfirmRequest:
                Show(message.PayloadAs<PendingConfirmation>());
                return true;
            case Topics.ConfirmResolved:
                var resolved = message.PayloadAs<ConfirmResolvedPayload>();
                Close(resolved.ConfirmId, DialogClose.Resolved, resolved);
                return true;
            default:
                return false;
        }
    }

    /// <summary>A (re)connect: show exactly what is still pending.</summary>
    public void ApplyWelcome(WelcomePayload welcome)
    {
        var pending = welcome.Snapshot.PendingConfirmations ?? [];
        foreach (var id in _open.Keys.Except(pending.Select(p => p.ConfirmId)).ToList())
            Close(id, DialogClose.Resolved, null);
        foreach (var p in pending)
            Show(p);
    }

    /// <summary>The link dropped: no dialog can be answered from here.</summary>
    public void Disconnected()
    {
        foreach (var id in _open.Keys.ToList())
            Close(id, DialogClose.Disconnected, null);
    }

    /// <summary>The Brain refused our answer (E_NOT_FOUND): it isn't pending any more.</summary>
    public void Dismiss(string confirmId) => Close(confirmId, DialogClose.Resolved, null);

    /// <summary>Close dialogs whose deadline passed (call from a timer).</summary>
    public void ExpireOverdue()
    {
        var now = _clock();
        foreach (var d in _open.Values.Where(d => now >= d.Deadline + ExpiryGrace).ToList())
            Close(d.ConfirmId, DialogClose.Expired, null);
    }

    private void Show(PendingConfirmation p)
    {
        if (_open.ContainsKey(p.ConfirmId))
            return;                                 // already showing (snapshot after the event)
        var dialog = new ConfirmationDialog(p.ConfirmId, p.Prompt, p.Detail ?? "", p.Risk, p.TimeoutS,
                                            _clock() + TimeSpan.FromSeconds(p.TimeoutS));
        _open[p.ConfirmId] = dialog;
        Opened?.Invoke(dialog);
    }

    private void Close(string id, DialogClose why, ConfirmResolvedPayload? resolved)
    {
        if (_open.Remove(id))
            Closed?.Invoke(id, why, resolved);
    }
}
