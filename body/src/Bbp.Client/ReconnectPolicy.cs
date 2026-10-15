namespace Bbp.Client;

/// <summary>
/// Spec backoff: retry after 0.5 s, 1 s, 2 s, then every 5 s. A session that
/// reached <c>welcome</c> resets it, so a dropped link is retried quickly.
/// </summary>
public sealed class ReconnectPolicy
{
    private static readonly TimeSpan[] Ramp =
    [
        TimeSpan.FromMilliseconds(500),
        TimeSpan.FromSeconds(1),
        TimeSpan.FromSeconds(2),
    ];

    public static readonly TimeSpan Steady = TimeSpan.FromSeconds(5);

    private int _attempt;

    public TimeSpan NextDelay()
    {
        var delay = _attempt < Ramp.Length ? Ramp[_attempt] : Steady;
        if (_attempt <= Ramp.Length)
            _attempt++;
        return delay;
    }

    public void Reset() => _attempt = 0;
}
