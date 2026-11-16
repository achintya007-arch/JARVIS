namespace Jarvis.Launcher;

/// <summary>
/// When to restart something that stopped unexpectedly: at most
/// <see cref="MaxRestarts"/> restarts within <see cref="Window"/>, each after a
/// longer wait. One more crash inside the window and it stays stopped.
/// </summary>
public sealed class RestartPolicy(int maxRestarts = 3, TimeSpan? window = null, IReadOnlyList<TimeSpan>? backoff = null)
{
    private readonly List<DateTimeOffset> _recent = [];

    public int MaxRestarts { get; } = maxRestarts;
    public TimeSpan Window { get; } = window ?? TimeSpan.FromMinutes(5);
    public IReadOnlyList<TimeSpan> Backoff { get; } =
        backoff ?? [TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(15)];

    /// <summary>Restarts counted in the current window.</summary>
    public int Recent => _recent.Count;

    /// <summary>The wait before the next restart, or null to give up. Counts it.</summary>
    public TimeSpan? Next(DateTimeOffset now)
    {
        _recent.RemoveAll(t => now - t >= Window);
        if (_recent.Count >= MaxRestarts)
            return null;
        var wait = Backoff[Math.Min(_recent.Count, Backoff.Count - 1)];
        _recent.Add(now);
        return wait;
    }

    /// <summary>You asked for a restart, or it ran fine: start counting afresh.</summary>
    public void Reset() => _recent.Clear();
}
