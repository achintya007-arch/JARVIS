namespace Bbp.Client.Tests;

/// <summary>
/// The spec's close-code table, one case each:
///   1000 → normal backoff · 4401 → stop retrying, show auth failed ·
///   4408 → reconnect immediately, then normal backoff ·
///   anything else / network drop → normal backoff.
/// </summary>
public class CloseCodeTests
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(5);

    /// <summary>Backoff and auth-poll waits complete instantly and are recorded
    /// with how many connection attempts had happened by then; ping waits are real.</summary>
    private sealed class Clock(
        CancellationTokenSource cts, int stopAfter, BbpClientOptions options, Func<int> attempts,
        Action<int>? onWait = null, bool authPollNeverEnds = false)
    {
        private readonly List<(double Seconds, int AttemptsSoFar)> _waits = [];

        public IReadOnlyList<(double Seconds, int AttemptsSoFar)> Waits
        {
            get { lock (_waits) return [.. _waits]; }
        }

        public Task Delay(TimeSpan d, CancellationToken ct)
        {
            if (d == options.PingInterval)
                return Task.Delay(d, ct);
            if (authPollNeverEnds && d == options.AuthRetryPollInterval)
                return Task.Delay(System.Threading.Timeout.Infinite, ct);
            int count;
            lock (_waits)
            {
                _waits.Add((d.TotalSeconds, attempts()));
                count = _waits.Count;
            }
            onWait?.Invoke(count);
            if (count >= stopAfter)
                cts.Cancel();
            return Task.CompletedTask;
        }
    }

    private static BbpClientOptions Options(Func<string?>? token = null) => new()
    {
        TokenProvider = token ?? (() => "tok"),
        PingInterval = TimeSpan.FromMilliseconds(4321),
        AuthRetryPollInterval = TimeSpan.FromMilliseconds(3210),
    };

    /// <summary>A Brain that welcomes the hello, then ends the session.</summary>
    private static FakeSocket WelcomeThen(Action<FakeSocket> end) => new()
    {
        OnSend = (msg, s) =>
        {
            if ((string)msg["kind"]! != "hello") return;
            s.Push(Brain.Welcome());
            end(s);
        },
    };

    private static FakeSocket RejectToken() => new()
    {
        OnSend = (msg, s) => { if ((string)msg["kind"]! == "hello") s.ServerClose(4401, "E_AUTH"); },
    };

    private static List<(ConnectionState State, string Detail)> Track(BbpClient client)
    {
        var states = new List<(ConnectionState, string)>();
        client.StateChanged += (s, d) => { lock (states) states.Add((s, d)); };
        return states;
    }

    private static async Task Run(BbpClient client, CancellationTokenSource cts)
    {
        cts.CancelAfter(Timeout);
        await client.RunAsync(cts.Token);
    }

    // ── 1000 ───────────────────────────────────────────────────────────────

    [Fact]
    public async Task Close1000_ReconnectsWithNormalBackoff()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(n => n == 1 ? WelcomeThen(s => s.ServerClose(1000, "brain shutting down")) : null);
        var clock = new Clock(cts, 2, opts, () => factory.Attempts);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);

        await Run(client, cts);

        Assert.Equal([(0.5, 1), (1, 2)], clock.Waits);
    }

    // ── 4401 ───────────────────────────────────────────────────────────────

    [Fact]
    public async Task Close4401_StopsRetrying_AndReportsAuthFailed()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(_ => RejectToken());
        var clock = new Clock(cts, 4, opts, () => factory.Attempts);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        var states = Track(client);

        await Run(client, cts);

        Assert.Equal(1, factory.Attempts);                               // never retried
        Assert.All(clock.Waits, w => Assert.Equal(3.21, w.Seconds));     // only token-file polls
        Assert.Contains((ConnectionState.AuthFailed, "auth failed, check token"), states);
        Assert.DoesNotContain(states, s => s.State == ConnectionState.Connected);
    }

    [Fact]
    public async Task Close4401_RetriesOnceTheTokenFileChanges()
    {
        using var cts = new CancellationTokenSource();
        var token = "old";
        var opts = Options(() => token);
        var good = Brain.Socket();
        var factory = new FakeFactory(n => n == 1 ? RejectToken() : n == 2 ? good : null);
        var clock = new Clock(cts, 10, opts, () => factory.Attempts, onWait: i => { if (i == 2) token = "new"; });
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        client.Welcomed += _ => cts.Cancel();

        await Run(client, cts);

        Assert.Equal(2, factory.Attempts);
        Assert.Equal("new", (string)good.Sent[0]["payload"]!["token"]!);
        Assert.Equal(2, clock.Waits.Count);                              // two polls, no backoff
    }

    [Fact]
    public async Task Close4401_RetriesWhenReconnectIsRequested()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(n => n == 1 ? RejectToken() : n == 2 ? Brain.Socket() : null);
        var clock = new Clock(cts, 10, opts, () => factory.Attempts, authPollNeverEnds: true);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        client.StateChanged += (s, detail) =>
        {
            if (s == ConnectionState.AuthFailed)
                _ = Task.Run(async () => { await Task.Delay(50); client.Reconnect(); });
        };
        client.Welcomed += _ => cts.Cancel();

        await Run(client, cts);

        Assert.Equal(2, factory.Attempts);
    }

    // ── 4408 ───────────────────────────────────────────────────────────────

    [Fact]
    public async Task Close4408_ReconnectsImmediately_ThenNormalBackoff()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(n => n == 1 ? WelcomeThen(s => s.ServerClose(4408, "heartbeat timeout")) : null);
        var clock = new Clock(cts, 2, opts, () => factory.Attempts);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        var states = Track(client);

        await Run(client, cts);

        // Attempt 2 happened with no wait; only its failure started the backoff.
        Assert.Equal([(0.5, 2), (1, 3)], clock.Waits);
        Assert.Contains(states, s => s.Detail.Contains("4408"));
    }

    [Fact]
    public async Task Close4408_EveryWelcomedSessionGetsItsOwnImmediateRetry()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(n => n <= 2 ? WelcomeThen(s => s.ServerClose(4408)) : null);
        var clock = new Clock(cts, 1, opts, () => factory.Attempts);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);

        await Run(client, cts);

        Assert.Equal([(0.5, 3)], clock.Waits);
    }

    // ── Anything else ──────────────────────────────────────────────────────

    [Theory]
    [InlineData(1001)]
    [InlineData(1011)]
    [InlineData(4000)]
    public async Task UnexpectedCloseCode_ReconnectsWithNormalBackoff(int code)
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(n => n == 1 ? WelcomeThen(s => s.ServerClose(code)) : null);
        var clock = new Clock(cts, 2, opts, () => factory.Attempts);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        var states = Track(client);

        await Run(client, cts);

        Assert.Equal([(0.5, 1), (1, 2)], clock.Waits);
        Assert.DoesNotContain(states, s => s.State == ConnectionState.AuthFailed);
    }

    [Fact]
    public async Task NetworkDrop_ReconnectsWithNormalBackoff()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var factory = new FakeFactory(n => n == 1 ? WelcomeThen(s => s.Drop()) : null);
        var clock = new Clock(cts, 2, opts, () => factory.Attempts);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        var states = Track(client);

        await Run(client, cts);

        Assert.Equal([(0.5, 1), (1, 2)], clock.Waits);
        Assert.Contains(states, s => s.Detail.StartsWith("connection lost"));
    }
}
