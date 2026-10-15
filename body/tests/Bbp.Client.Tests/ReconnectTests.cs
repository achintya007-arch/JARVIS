namespace Bbp.Client.Tests;

public class ReconnectPolicyTests
{
    [Fact]
    public void FollowsSpecBackoff_ThenSteadyFiveSeconds()
    {
        var p = new ReconnectPolicy();
        var delays = Enumerable.Range(0, 7).Select(_ => p.NextDelay().TotalSeconds);
        Assert.Equal([0.5, 1, 2, 5, 5, 5, 5], delays);
    }

    [Fact]
    public void Reset_StartsOverAtHalfASecond()
    {
        var p = new ReconnectPolicy();
        for (var i = 0; i < 10; i++) p.NextDelay();
        p.Reset();
        Assert.Equal(0.5, p.NextDelay().TotalSeconds);
    }
}

/// <summary>BbpClient's connect / backoff / heartbeat loop against fake sockets.</summary>
public class BbpClientTests
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(5);

    // Not 5000: keeps the ping interval distinguishable from the 5 s steady backoff.
    private static BbpClientOptions Options(string? token = "tok", int pingMs = 4321) => new()
    {
        TokenProvider = () => token,
        PingInterval = TimeSpan.FromMilliseconds(pingMs),
    };

    /// <summary>Records backoff delays (instantly) and cancels after <paramref name="stopAfter"/> of them.
    /// Ping-interval delays are real, so heartbeat tests run in real (short) time.</summary>
    private sealed class Clock(CancellationTokenSource cts, int stopAfter, TimeSpan pingInterval)
    {
        private readonly List<double> _backoffs = [];

        public IReadOnlyList<double> Backoffs { get { lock (_backoffs) return [.. _backoffs]; } }

        public Task Delay(TimeSpan d, CancellationToken ct)
        {
            if (d == pingInterval)
                return Task.Delay(d, ct);
            lock (_backoffs)
            {
                _backoffs.Add(d.TotalSeconds);
                if (_backoffs.Count >= stopAfter)
                    cts.Cancel();
            }
            return Task.CompletedTask;
        }
    }

    /// <summary>Backoff waits are instant; ping waits are real.</summary>
    private static Func<TimeSpan, CancellationToken, Task> InstantBackoff(BbpClientOptions o) =>
        (d, ct) => d == o.PingInterval ? Task.Delay(d, ct) : Task.CompletedTask;

    private static async Task Run(BbpClient client, CancellationTokenSource cts)
    {
        cts.CancelAfter(Timeout);
        await client.RunAsync(cts.Token);
    }

    [Fact]
    public async Task RefusedConnections_BackOffPerSpec()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var clock = new Clock(cts, stopAfter: 6, opts.PingInterval);
        var factory = new FakeFactory(_ => null);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);

        await Run(client, cts);

        Assert.Equal([0.5, 1, 2, 5, 5, 5], clock.Backoffs);
        Assert.Equal(6, factory.Attempts);
    }

    [Fact]
    public async Task SuccessfulSession_ResetsBackoff()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options();
        var clock = new Clock(cts, stopAfter: 5, opts.PingInterval);
        // attempts 1-3 refused, 4 welcomed then dropped by the Brain, 5+ refused
        var factory = new FakeFactory(n =>
        {
            if (n != 4) return null;
            var s = Brain.Socket();
            Task.Run(async () => { await Task.Delay(50); s.ServerClose(1001); });
            return s;
        });
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);

        await Run(client, cts);

        Assert.Equal([0.5, 1, 2, 0.5, 1], clock.Backoffs);
    }

    [Fact]
    public async Task Handshake_SendsHelloWithToken_ThenReportsConnectedAndWelcome()
    {
        using var cts = new CancellationTokenSource();
        var socket = Brain.Socket();
        var factory = new FakeFactory(n => n == 1 ? socket : null);
        var opts = Options(token: "secret");
        var client = new BbpClient(opts, Brain.Validator, factory, InstantBackoff(opts));
        WelcomePayload? welcome = null;
        var states = new List<ConnectionState>();
        client.StateChanged += (s, _) => { lock (states) states.Add(s); };
        client.Welcomed += w => { welcome = w; cts.Cancel(); };

        await Run(client, cts);

        var hello = socket.Sent[0];
        Assert.Equal("hello", (string)hello["kind"]!);
        Assert.Equal("secret", (string)hello["payload"]!["token"]!);
        Brain.Validator.Validate(hello);
        Assert.NotNull(welcome);
        Assert.Equal("test-model", welcome!.Snapshot.Agent.Model);
        Assert.Contains(ConnectionState.Connected, states);
    }

    [Fact]
    public async Task NoToken_DoesNotConnect_AndRetries()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options(token: null);
        var clock = new Clock(cts, stopAfter: 3, opts.PingInterval);
        var factory = new FakeFactory(_ => Brain.Socket());
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);

        await Run(client, cts);

        Assert.Equal(0, factory.Attempts);
        Assert.Equal(3, clock.Backoffs.Count);
    }

    [Fact]
    public async Task ThreeMissedPongs_DropsAndReconnects()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options(pingMs: 20);
        var clock = new Clock(cts, stopAfter: 1, opts.PingInterval);
        var silent = Brain.Socket(answerPings: false);
        var factory = new FakeFactory(n => n == 1 ? silent : null);
        var client = new BbpClient(opts, Brain.Validator, factory, clock.Delay);
        var errors = new List<string>();
        client.ProtocolError += e => { lock (errors) errors.Add(e); };

        await Run(client, cts);

        Assert.Equal(3, silent.Sent.Count(m => (string)m["kind"]! == "ping"));
        Assert.Contains(errors, e => e.Contains("pongs missed"));
        Assert.Equal([0.5], clock.Backoffs);      // session had been welcomed → fresh backoff
    }

    [Fact]
    public async Task AnsweredPings_KeepTheSessionAlive()
    {
        using var cts = new CancellationTokenSource();
        var opts = Options(pingMs: 10);
        var socket = Brain.Socket(answerPings: true);
        var factory = new FakeFactory(n => n == 1 ? socket : null);
        var client = new BbpClient(opts, Brain.Validator, factory, (d, ct) => Task.Delay(d, ct));
        var errors = new List<string>();
        client.ProtocolError += e => { lock (errors) errors.Add(e); };
        client.Welcomed += _ => cts.CancelAfter(300);     // ~30 ping intervals

        await client.RunAsync(cts.Token);

        Assert.True(socket.Sent.Count(m => (string)m["kind"]! == "ping") >= 5);
        Assert.Empty(errors);
        Assert.Equal(1, factory.Attempts);
    }

    [Fact]
    public async Task Events_AreValidatedAndDispatched_BadOnesDropped()
    {
        using var cts = new CancellationTokenSource();
        var socket = Brain.Socket();
        var factory = new FakeFactory(n => n == 1 ? socket : null);
        var opts = Options();
        var client = new BbpClient(opts, Brain.Validator, factory, InstantBackoff(opts));
        var received = new List<Envelope>();
        var errors = new List<string>();
        client.ProtocolError += e => { lock (errors) errors.Add(e); };
        client.EventReceived += e =>
        {
            lock (received) received.Add(e);
            if (e.Topic == "reply.done") cts.Cancel();
        };
        client.Welcomed += _ =>
        {
            socket.PushRaw("{broken");
            socket.Push(Brain.Event("voice.state", new VoiceStatePayload("dancing", false)));   // invalid
            socket.Push(Brain.Event("sentinel.finding", new { }));                              // unknown: silently dropped
            socket.Push(Brain.Event("voice.state", new VoiceStatePayload("speaking", false)));
            socket.Push(Brain.Event("reply.done", new ReplyDonePayload("t", false)));
        };

        await Run(client, cts);

        Assert.Equal(["voice.state", "reply.done"], received.Select(e => e.Topic));
        Assert.Equal(2, errors.Count);
    }

    [Fact]
    public async Task SendCommand_WhenDisconnected_Throws()
    {
        var client = new BbpClient(Options(), Brain.Validator, new FakeFactory(_ => null));
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            client.SendCommandAsync("voice.state", new VoiceStatePayload("idle", false)));
    }
}

public class CommandTests
{
    [Fact]
    public async Task Commands_AreValidatedSent_AndErrorsComeBackAsResponses()
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var socket = new FakeSocket
        {
            OnSend = (msg, s) =>
            {
                var kind = (string)msg["kind"]!;
                if (kind == "hello")
                    s.Push(Brain.Welcome());
                else if (kind == "command")
                    s.Push(Envelope.Create("response", "error",
                        new ErrorPayload(false, new ErrorInfo("E_NOT_ALLOWED", "the microphone is muted")),
                        corr: (string)msg["id"]!));
            },
        };
        var opts = new BbpClientOptions { TokenProvider = () => "tok", PingInterval = TimeSpan.FromMilliseconds(4321) };
        var client = new BbpClient(opts, Brain.Validator, new FakeFactory(n => n == 1 ? socket : null),
            (d, ct) => d == opts.PingInterval ? Task.Delay(d, ct) : Task.CompletedTask);
        string? sentId = null;
        Envelope? response = null;
        client.Welcomed += _ => Task.Run(async () =>
            sentId = await client.SendCommandAsync(Topics.UiHotkey, new HotkeyPayload("push_to_talk")));
        client.ResponseReceived += r => { response = r; cts.Cancel(); };

        await client.RunAsync(cts.Token);

        var command = socket.Sent.Single(m => (string)m["kind"]! == "command");
        Brain.Validator.Validate(command);
        Assert.Equal("ui.hotkey", (string)command["topic"]!);
        Assert.Equal("push_to_talk", (string)command["payload"]!["name"]!);
        Assert.NotNull(response);
        Assert.Equal(sentId, response!.Corr);
        Assert.Equal("E_NOT_ALLOWED", response.PayloadAs<ErrorPayload>().Error.Code);
    }

    [Theory]
    [InlineData("input.text", "")]
    [InlineData("voice.control", "dance")]
    public async Task InvalidCommands_AreRejectedBeforeSending(string topic, string value)
    {
        object payload = topic == "input.text" ? new InputTextPayload(value) : new VoiceControlPayload(value);
        var client = new BbpClient(new BbpClientOptions(), Brain.Validator, new FakeFactory(_ => null));
        var e = await Assert.ThrowsAsync<BbpException>(() => client.SendCommandAsync(topic, payload));
        Assert.Equal("E_BAD_PAYLOAD", e.Code);
    }
}
