using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace Bbp.Client;

/// <summary>Protocol constants (docs/protocol/BBP_v0.1.md).</summary>
public static class Protocol
{
    public const int Version = 1;

    public static class Kinds
    {
        public const string Event = "event";
        public const string Command = "command";
        public const string Request = "request";
        public const string Response = "response";
        public const string Hello = "hello";
        public const string Welcome = "welcome";
        public const string Ping = "ping";
        public const string Pong = "pong";

        /// <summary>Session kinds carry topic == kind and have schemas under protocol/session/.</summary>
        public static bool IsSession(string kind) =>
            kind is Hello or Welcome or Ping or Pong;
    }

    public static class CloseCodes
    {
        public const int Auth = 4401;
        public const int HeartbeatTimeout = 4408;
    }
}

/// <summary>JSON settings shared by every message: snake_case payload fields
/// (turn_id, brain_version, …) and explicit nulls (corr is always present).</summary>
public static class BbpJson
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        WriteIndented = false,
    };
}

/// <summary>One BBP message. Every message, in either direction, has exactly
/// these seven fields.</summary>
public sealed record Envelope
{
    [JsonPropertyName("v")] public int V { get; init; } = Protocol.Version;
    [JsonPropertyName("id")] public string Id { get; init; } = "";
    [JsonPropertyName("kind")] public string Kind { get; init; } = "";
    [JsonPropertyName("topic")] public string Topic { get; init; } = "";
    [JsonPropertyName("ts")] public string Ts { get; init; } = "";
    [JsonPropertyName("corr")] public string? Corr { get; init; }
    [JsonPropertyName("payload")] public JsonObject Payload { get; init; } = new();

    /// <summary>Build a message with a fresh id and timestamp. For session kinds pass topic == kind.</summary>
    public static Envelope Create(string kind, string topic, object? payload = null, string? corr = null) => new()
    {
        Id = NewId(),
        Kind = kind,
        Topic = topic,
        Ts = Now(),
        Corr = corr,
        Payload = payload switch
        {
            null => new JsonObject(),
            JsonObject obj => obj,
            _ => JsonSerializer.SerializeToNode(payload, BbpJson.Options) as JsonObject
                 ?? throw new ArgumentException("payload must serialize to a JSON object", nameof(payload)),
        },
    };

    public static string NewId() => Guid.NewGuid().ToString("N");

    /// <summary>ISO 8601 with milliseconds and the local UTC offset, e.g. 2026-10-15T09:30:01.123+05:30.</summary>
    public static string Now() =>
        DateTimeOffset.Now.ToString("yyyy-MM-dd'T'HH:mm:ss.fffzzz", CultureInfo.InvariantCulture);

    public T PayloadAs<T>() =>
        Payload.Deserialize<T>(BbpJson.Options)
        ?? throw new BbpException("E_BAD_PAYLOAD", $"payload of {Topic} is null");

    public string ToJson() => JsonSerializer.Serialize(this, BbpJson.Options);

    public JsonNode ToNode() => JsonSerializer.SerializeToNode(this, BbpJson.Options)!;

    /// <summary>Deserialize an already-validated message.</summary>
    public static Envelope FromNode(JsonNode node) =>
        node.Deserialize<Envelope>(BbpJson.Options)
        ?? throw new BbpException("E_BAD_PAYLOAD", "message is null");
}

/// <summary>A protocol violation with a spec error code (E_BAD_PAYLOAD, E_UNKNOWN_TOPIC, …).</summary>
public sealed class BbpException(string code, string message) : Exception($"{code}: {message}")
{
    public string Code { get; } = code;
    public string Detail { get; } = message;
}
