using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace Bbp.Client;

public enum ConnectionState
{
    Disconnected,
    Connecting,
    Connected,
    AuthFailed,
}

/// <summary>Answers one Brain → Body request. Return the `result` object
/// (serialized snake_case); throw <see cref="BbpException"/> to refuse with a
/// spec error code (E_NOT_ALLOWED, E_NOT_FOUND, …).</summary>
public delegate Task<object> RequestHandler(Envelope request, CancellationToken ct);

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

    /// <summary>After a 4401, how often to check whether the token file changed.</summary>
    public TimeSpan AuthRetryPollInterval { get; init; } = TimeSpan.FromSeconds(3);

    /// <summary>Budget for one request handler; the Brain gives up at 5 s.</summary>
    public TimeSpan RequestBudget { get; init; } = TimeSpan.FromSeconds(4.5);

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
    private readonly SemaphoreSlim _reconnectSignal = new(0, 1);
    private readonly Dictionary<string, RequestHandler> _requestHandlers = new();
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

    /// <summary>A welcomed session ended: the close code, or null if the
    /// connection was lost (1000 = the Brain shut down cleanly).</summary>
    public event Action<int?>? SessionEnded;

    /// <summary>Every validated event from the Brain.</summary>
    public event Action<Envelope>? EventReceived;

    /// <summary>Responses to our commands (including topic "error").</summary>
    public event Action<Envelope>? ResponseReceived;

    /// <summary>A message from the Brain that failed validation (dropped).</summary>
    public event Action<string>? ProtocolError;

    /// <summary>(topic, outcome, milliseconds) after each request is answered — for the log.</summary>
    public event Action<string, string, double>? RequestHandled;

    /// <summary>Answer Brain requests on <paramref name="topic"/>. Register before RunAsync.</summary>
    public void HandleRequests(string topic, RequestHandler handler) => _requestHandlers[topic] = handler;

    /// <summary>
    /// Run until cancelled: connect, serve the session, then react to how it
    /// ended (spec close-code table):
    ///   1000 / other codes / network drop → reconnect with normal backoff;
    ///   4401 (bad token) → stop retrying until the token file changes or
    ///        <see cref="Reconnect"/> is called;
    ///   4408 (idle timeout) → reconnect immediately once, then normal backoff.
    /// </summary>
    public async Task RunAsync(CancellationToken ct)
    {
        var immediateUsed = false;
        try
        {
            while (!ct.IsCancellationRequested)
            {
                var token = _options.TokenProvider();
                var attempt = token is null
                    ? new Attempt(false, null, "no token yet (start the Brain with bbp.enabled)")
                    : await AttemptAsync(token, ct).ConfigureAwait(false);
                ct.ThrowIfCancellationRequested();

                if (attempt.Welcomed)
                {
                    _backoff.Reset();
                    immediateUsed = false;
                    SessionEnded?.Invoke(attempt.CloseCode);
                }

                if (attempt.CloseCode == Protocol.CloseCodes.Auth)
                {
                    SetState(ConnectionState.AuthFailed, "auth failed, check token");
                    await WaitForAuthRetryAsync(token!, ct).ConfigureAwait(false);
                    _backoff.Reset();
                    continue;
                }

                if (attempt.CloseCode == Protocol.CloseCodes.HeartbeatTimeout && !immediateUsed)
                {
                    immediateUsed = true;
                    SetState(ConnectionState.Disconnected, "idle timeout (4408), reconnecting now");
                    continue;
                }

                var wait = _backoff.NextDelay();
                SetState(ConnectionState.Disconnected, $"{attempt.Detail} — retry in {wait.TotalSeconds:0.#} s");
                await WaitOrReconnectAsync(wait, ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // stopping
        }
        SetState(ConnectionState.Disconnected, "stopped");
    }

    /// <summary>Retry now instead of waiting: after an auth failure (e.g. once
    /// the token is fixed) or during a backoff wait. No effect while connected.</summary>
    public void Reconnect()
    {
        if (State == ConnectionState.Connected)
            return;
        try { _reconnectSignal.Release(); }
        catch (SemaphoreFullException) { }
    }

    private readonly record struct Attempt(bool Welcomed, int? CloseCode, string Detail);

    private async Task<Attempt> AttemptAsync(string token, CancellationToken ct)
    {
        SetState(ConnectionState.Connecting, _options.Url.ToString());
        try
        {
            await using var socket = await _factory.ConnectAsync(_options.Url, ct).ConfigureAwait(false);
            var welcomed = await RunSessionAsync(socket, token, ct).ConfigureAwait(false);
            var detail = socket.CloseStatus is { } code
                ? $"closed {code} {socket.CloseDescription}".TrimEnd()
                : "connection lost";
            return new Attempt(welcomed, socket.CloseStatus, detail);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e)
        {
            return new Attempt(false, null, e.Message);
        }
    }

    /// <summary>After 4401: don't hammer the Brain with a token it rejected. Wake
    /// up when the token file holds a different token, or on Reconnect().</summary>
    private async Task WaitForAuthRetryAsync(string rejectedToken, CancellationToken ct)
    {
        while (true)
        {
            if (await WaitOrReconnectAsync(_options.AuthRetryPollInterval, ct).ConfigureAwait(false))
                return;
            var current = _options.TokenProvider();
            if (current is not null && current != rejectedToken)
                return;
        }
    }

    /// <summary>Wait for <paramref name="delay"/>; true if Reconnect() cut it short.</summary>
    private async Task<bool> WaitOrReconnectAsync(TimeSpan delay, CancellationToken ct)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var signal = _reconnectSignal.WaitAsync(linked.Token);
        var timer = _delay(delay, linked.Token);
        var first = await Task.WhenAny(signal, timer).ConfigureAwait(false);
        await linked.CancelAsync().ConfigureAwait(false);
        ct.ThrowIfCancellationRequested();
        return first == signal && signal.IsCompletedSuccessfully;
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
        JsonNode? node;
        try
        {
            node = JsonNode.Parse(raw);
        }
        catch (JsonException e)
        {
            ProtocolError?.Invoke($"not JSON: {e.Message}");
            return;
        }
        // A request always gets an answer, even an invalid one, so it is
        // handled before (and apart from) the generic drop-if-invalid path.
        if (node is JsonObject obj && obj["kind"] is JsonValue k && k.GetValueKind() == JsonValueKind.String
            && (string)k! == Protocol.Kinds.Request)
        {
            _ = HandleRequestAsync(obj);
            return;
        }
        var message = Validate(node);
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
        return Validate(node);
    }

    private Envelope? Validate(JsonNode? node)
    {
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

    // ── Requests (Brain → Body), amendment 14 ───────────────────────────────

    private async Task HandleRequestAsync(JsonObject node)
    {
        var started = System.Diagnostics.Stopwatch.StartNew();
        var id = node["id"] is JsonValue v && v.GetValueKind() == JsonValueKind.String ? (string)v! : null;
        var topic = node["topic"] is JsonValue t && t.GetValueKind() == JsonValueKind.String ? (string)t! : "?";
        Envelope reply;
        try
        {
            _validator.ValidateEnvelope(node);
            var request = Envelope.FromNode(node);
            if (!_requestHandlers.TryGetValue(request.Topic, out var handler))
                throw new BbpException("E_UNKNOWN_TOPIC", $"the Body doesn't handle '{request.Topic}'");
            _validator.ValidatePayload(request.Kind, request.Topic, request.Payload);

            using var budget = new CancellationTokenSource(_options.RequestBudget);
            var result = await Task.Run(() => handler(request, budget.Token), budget.Token).ConfigureAwait(false);
            reply = Envelope.Create(Protocol.Kinds.Response, request.Topic, new RequestOk(true, result), corr: request.Id);
            try
            {
                _validator.Validate(reply);         // never send a result the schema rejects
            }
            catch (BbpException e)
            {
                throw new BbpException("E_INTERNAL", $"invalid result: {e.Detail}");
            }
        }
        catch (Exception e)
        {
            if (id is null)
            {
                ProtocolError?.Invoke($"unanswerable request (no id): {e.Message}");
                return;
            }
            var (code, message) = e switch
            {
                BbpException b => (b.Code, b.Detail),
                OperationCanceledException => ("E_TIMEOUT", "the Body ran out of time"),
                _ => ("E_INTERNAL", e.Message),
            };
            reply = ErrorReply(id, code, message);
        }

        RequestHandled?.Invoke(topic, reply.Topic == Topics.Error
            ? reply.PayloadAs<ErrorPayload>().Error.Code : "ok", started.Elapsed.TotalMilliseconds);
        var socket = _socket;
        if (socket is null)
            return;                                 // disconnected meanwhile: the Brain times out
        try
        {
            await SendAsync(socket, reply, CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception e)
        {
            ProtocolError?.Invoke($"couldn't send response: {e.Message}");
        }
    }

    private static Envelope ErrorReply(string requestId, string code, string message) =>
        Envelope.Create(Protocol.Kinds.Response, Topics.Error,
            new ErrorPayload(false, new ErrorInfo(code, message)), corr: requestId);

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

/// <summary>Successful response payload: { ok: true, result }.</summary>
public sealed record RequestOk(bool Ok, [property: JsonPropertyName("result")] object Result);
