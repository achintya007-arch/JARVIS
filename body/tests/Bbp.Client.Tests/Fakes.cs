using System.Text.Json.Nodes;
using System.Threading.Channels;

namespace Bbp.Client.Tests;

/// <summary>In-memory socket: the test plays the Brain.</summary>
internal sealed class FakeSocket : IBbpSocket
{
    private readonly Channel<string?> _inbound = Channel.CreateUnbounded<string?>();
    private readonly List<JsonObject> _sent = [];

    /// <summary>Called for every message the client sends (the "Brain" reacting).</summary>
    public Action<JsonObject, FakeSocket>? OnSend { get; init; }

    public int? CloseStatus { get; private set; }
    public string? CloseDescription { get; private set; }

    public IReadOnlyList<JsonObject> Sent
    {
        get { lock (_sent) return [.. _sent]; }
    }

    public Task SendAsync(string text, CancellationToken ct)
    {
        var node = JsonNode.Parse(text)!.AsObject();
        lock (_sent) _sent.Add(node);
        OnSend?.Invoke(node, this);
        return Task.CompletedTask;
    }

    public async Task<string?> ReceiveAsync(CancellationToken ct) =>
        await _inbound.Reader.ReadAsync(ct);

    public void Push(Envelope message) => _inbound.Writer.TryWrite(message.ToJson());

    public void PushRaw(string raw) => _inbound.Writer.TryWrite(raw);

    /// <summary>The Brain closes the connection with a code.</summary>
    public void ServerClose(int code, string reason = "")
    {
        CloseStatus = code;
        CloseDescription = reason;
        _inbound.Writer.TryWrite(null);
    }

    /// <summary>The TCP connection dies: no close frame, no close code.</summary>
    public void Drop() => _inbound.Writer.TryWrite(null);

    public Task CloseAsync(int code, string reason, CancellationToken ct)
    {
        CloseStatus ??= code;
        _inbound.Writer.TryWrite(null);
        return Task.CompletedTask;
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

/// <summary>Hands out sockets per attempt; a null entry means "connection refused".</summary>
internal sealed class FakeFactory(Func<int, FakeSocket?> perAttempt) : IBbpSocketFactory
{
    private int _attempts;

    public int Attempts => Volatile.Read(ref _attempts);

    public Task<IBbpSocket> ConnectAsync(Uri uri, CancellationToken ct)
    {
        var attempt = Interlocked.Increment(ref _attempts);
        var socket = perAttempt(attempt);
        return socket is null
            ? Task.FromException<IBbpSocket>(new IOException("connection refused"))
            : Task.FromResult<IBbpSocket>(socket);
    }
}

internal static class Brain
{
    public static readonly Snapshot EmptySnapshot = new(
        new VoiceStatePayload("idle", false),
        new AgentSnapshot("neutral", "unknown", "test-model", new LoadSnapshot(50.0, null)),
        [],
        []);

    public static Envelope Welcome(Snapshot? snapshot = null) =>
        Envelope.Create("welcome", "welcome", new WelcomePayload("s1", "3.0", snapshot ?? EmptySnapshot));

    public static Envelope Event(string topic, object payload) => Envelope.Create("event", topic, payload);

    /// <summary>A well-behaved Brain: welcomes the hello and answers every ping.</summary>
    public static FakeSocket Socket(bool answerPings = true) => new()
    {
        OnSend = (msg, sock) =>
        {
            var kind = (string)msg["kind"]!;
            if (kind == "hello")
                sock.Push(Welcome());
            else if (kind == "ping" && answerPings)
                sock.Push(Envelope.Create("pong", "pong", corr: (string)msg["id"]!));
        },
    };

    public static SchemaValidator Validator { get; } = new();
}
