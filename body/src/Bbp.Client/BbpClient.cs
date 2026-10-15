using System.Text.Json;
using System.Text.Json.Nodes;

namespace Bbp.Client;

public enum ConnectionState
{
    Disconnected,
    Connecting,
    Connected,
    AuthFailed,
}

public sealed class BbpClientOptions
{
    public Uri Url { get; init; } = new("ws://127.0.0.1:8765/bbp");

    /// <summary>Called before every connection attempt; null means "no token yet".</summary>
    public Func<string?> TokenProvider { get; init; } = () => null;

    public string ClientName { get; init; } = "win-shell";
    public string ClientVersion { get; init; } = "0.1.0";
    public IReadOnlyList<string> Capabilities { get; init; } = ["hud"];

    /// <summary>Spec: the Body pings every 5 s.</summary>
    public TimeSpan PingInterval { get; init; } = TimeSpan.FromSeconds(5);

    /// <summary>Spec: three missed pongs means disconnected.</summary>
    public int MissedPongLimit { get; init; } = 3;

    /// <summary>How long to wait for welcome after hello.</summary>
    public TimeSpan WelcomeTimeout { get; init; } = TimeSpan.FromSeconds(10);
}

/// <summary>
/// The Body's end of the Brain–Body Protocol: connect → hello → welcome, then
/// ping every 5 s and dispatch validated events, reconnecting with the spec
/// backoff whenever the link drops. Events are raised on a background thread —
/// UI code must marshal to its own thread.
/// </summary>
public sealed class BbpClient
{
    private readonly BbpClientOptions _options;
    private readonly SchemaValidator _validator;
    private readonly IBbpSocketFactory _factory;
    private readonly Func<TimeSpan, CancellationToken, Task> _delay;
    private readonly ReconnectPolicy _backoff = new();
    private readonly SemaphoreSlim _sendLock = new(1, 1);
    private IBbpSocket? _socket;
    private int _outstandingPings;

    public BbpClient(
        BbpClientOptions options,
        SchemaValidator validator,
        IBbpSocketFactory? factory = null,
        Func<TimeSpan, CancellationToken, Task>? delay = null)
    {
        _options = options;
        _validator = validator;
        _factory = factory ?? new WebSocketFactory();
        _delay = delay ?? Task.Delay;
    }

    public ConnectionState State { get; private set; } = ConnectionState.Disconnected;

    /// <summary>(state, human-readable detail)</summary>
    public event Action<ConnectionState, string>? StateChanged;

    public event Action<WelcomePayload>? Welcomed;

    /// <summary>Every validated event from the Brain.</summary>
    public event Action<Envelope>? EventReceived;

    /// <summary>Responses to our commands (including topic "error").</summary>
    public event Action<Envelope>? ResponseReceived;

    /// <summary>A message from the Brain that failed validation (dropped).</summary>
    public event Action<string>? ProtocolError;

    /// <summary>Run until cancelled: connect, serve the session, back off, repeat.</summary>
    public async Task RunAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            var detail = "";
            var authFailed = false;
            var token = _options.TokenProvider();
            if (token is null)
            {
                detail = "no token yet (start the Brain with bbp.enabled)";
            }
            else
            {
                SetState(ConnectionState.Connecting, _options.Url.ToString());
                try
                {
                    await using var socket = await _factory.ConnectAsync(_options.Url, ct).ConfigureAwait(false);
                    var welcomed = await RunSessionAsync(socket, token, ct).ConfigureAwait(false);
                    if (welcomed)
                        _backoff.Reset();
                    authFailed = socket.CloseStatus == Protocol.CloseCodes.Auth;
                    detail = socket.CloseStatus is { } code
                        ? $"closed {code} {socket.CloseDescription}".TrimEnd()
                        : "connection lost";
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    break;
                }
                catch (Exception e)
                {
                    detail = e.Message;
                }
            }

            if (ct.IsCancellationRequested)
                break;
            var wait = _backoff.NextDelay();
            SetState(authFailed ? ConnectionState.AuthFailed : ConnectionState.Disconnected,
                     $"{detail} — retry in {wait.TotalSeconds:0.#} s");
            try
            {
                await _delay(wait, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
        SetState(ConnectionState.Disconnected, "stopped");
    }

    /// <summary>Send a command; returns its id (responses carry it as corr).
    /// Throws if not connected or the message doesn't match the schemas.</summary>
    public async Task<string> SendCommandAsync(string topic, object payload, CancellationToken ct = default)
    {
        var message = Envelope.Create(Protocol.Kinds.Command, topic, payload);
        _validator.Validate(message);
        var socket = _socket ?? throw new InvalidOperationException("not connected to the Brain");
        await SendAsync(socket, message, ct).ConfigureAwait(false);
        return message.Id;
    }

    // ── Session ─────────────────────────────────────────────────────────────

    private async Task<bool> RunSessionAsync(IBbpSocket socket, string token, CancellationToken ct)
    {
        using var session = CancellationTokenSource.CreateLinkedTokenSource(ct);
        await SendAsync(socket, Envelope.Create(Protocol.Kinds.Hello, Protocol.Kinds.Hello,
            new HelloPayload(_options.ClientName, _options.ClientVersion, token, _options.Capabilities)), ct)
            .ConfigureAwait(false);

        var welcome = await ReceiveWelcomeAsync(socket, ct).ConfigureAwait(false);
        if (welcome is null)
            return false;

        _outstandingPings = 0;
        _socket = socket;
        SetState(ConnectionState.Connected, $"brain {welcome.BrainVersion}");
        Welcomed?.Invoke(welcome);

        var pinger = PingLoopAsync(socket, session);
        try
        {
            while (true)
            {
                string? raw;
                try
                {
                    raw = await socket.ReceiveAsync(session.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                {
                    break;      // heartbeat lost: the ping loop cancelled the session
                }
                if (raw is null)
                    break;
                Dispatch(raw);
            }
        }
        finally
        {
            _socket = null;
            await session.CancelAsync().ConfigureAwait(false);
            try { await pinger.ConfigureAwait(false); }
            catch (OperationCanceledException) { }
        }
        return true;
    }

    private async Task<WelcomePayload?> ReceiveWelcomeAsync(IBbpSocket socket, CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(_options.WelcomeTimeout);
        string? raw;
        try
        {
            raw = await socket.ReceiveAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new TimeoutException("no welcome from the Brain");
        }
        if (raw is null)
            return null;    // closed (bad token → 4401)
        var message = Parse(raw) ?? throw new BbpException("E_BAD_PAYLOAD", "invalid welcome");
        if (message.Kind != Protocol.Kinds.Welcome)
            throw new BbpException("E_BAD_PAYLOAD", $"expected welcome, got {message.Kind}");
        return message.PayloadAs<WelcomePayload>();
    }

    private async Task PingLoopAsync(IBbpSocket socket, CancellationTokenSource session)
    {
        await Task.Yield();     // never run the loop inline in the session's receive path
        while (!session.IsCancellationRequested)
        {
            await _delay(_options.PingInterval, session.Token).ConfigureAwait(false);
            if (Volatile.Read(ref _outstandingPings) >= _options.MissedPongLimit)
            {
                ProtocolError?.Invoke($"{_options.MissedPongLimit} pongs missed — reconnecting");
                await session.CancelAsync().ConfigureAwait(false);
                return;
            }
            Interlocked.Increment(ref _outstandingPings);
            await SendAsync(socket, Envelope.Create(Protocol.Kinds.Ping, Protocol.Kinds.Ping), session.Token)
                .ConfigureAwait(false);
        }
    }

    private void Dispatch(string raw)
    {
        var message = Parse(raw);
        if (message is null)
            return;
        switch (message.Kind)
        {
            case Protocol.Kinds.Pong:
                Volatile.Write(ref _outstandingPings, 0);
                break;
            case Protocol.Kinds.Event:
                EventReceived?.Invoke(message);
                break;
            case Protocol.Kinds.Response:
                ResponseReceived?.Invoke(message);
                break;
            // request (milestone 3+), stray session messages: ignored
        }
    }

    /// <summary>Parse + validate; null (and ProtocolError) when invalid.
    /// Unknown event topics are dropped silently, as the spec requires.</summary>
    private Envelope? Parse(string raw)
    {
        JsonNode? node;
        try
        {
            node = JsonNode.Parse(raw);
        }
        catch (JsonException e)
        {
            ProtocolError?.Invoke($"not JSON: {e.Message}");
            return null;
        }
        try
        {
            _validator.Validate(node);
            return Envelope.FromNode(node!);
        }
        catch (BbpException e) when (e.Code == "E_UNKNOWN_TOPIC")
        {
            return null;
        }
        catch (Exception e) when (e is BbpException or JsonException)
        {
            ProtocolError?.Invoke(e.Message);
            return null;
        }
    }

    private async Task SendAsync(IBbpSocket socket, Envelope message, CancellationToken ct)
    {
        await _sendLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            await socket.SendAsync(message.ToJson(), ct).ConfigureAwait(false);
        }
        finally
        {
            _sendLock.Release();
        }
    }

    private void SetState(ConnectionState state, string detail)
    {
        State = state;
        StateChanged?.Invoke(state, detail);
    }
}
