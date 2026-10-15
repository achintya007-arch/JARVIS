using System.Collections.Concurrent;
using System.Text.Json.Nodes;
using Json.Schema;

namespace Bbp.Client;

/// <summary>
/// Validates messages against the shared JSON Schemas in protocol/ — the same
/// files the Brain validates with. Two steps, as in protocol/README.md: the
/// envelope, then the payload against session/&lt;kind&gt; (session kinds) or
/// topics/&lt;topic&gt;. A topic without a schema file is unknown.
/// </summary>
public sealed class SchemaValidator
{
    private readonly string _dir;
    private readonly ConcurrentDictionary<string, JsonSchema?> _cache = new();

    public SchemaValidator(string? protocolDir = null)
    {
        _dir = protocolDir ?? DefaultDirectory();
        if (!File.Exists(Path.Combine(_dir, "envelope.schema.json")))
            throw new FileNotFoundException($"BBP schemas not found in {_dir}");
    }

    public string Directory => _dir;

    /// <summary>protocol/ next to the binaries (copied there at build time).</summary>
    public static string DefaultDirectory() => Path.Combine(AppContext.BaseDirectory, "protocol");

    public bool HasSchema(string kind, string topic) => Load(PayloadSchemaPath(kind, topic)) is not null;

    /// <summary>Full check. Throws <see cref="BbpException"/> with E_BAD_PAYLOAD or E_UNKNOWN_TOPIC.</summary>
    public void Validate(JsonNode? message)
    {
        ValidateEnvelope(message);
        var obj = message!.AsObject();
        ValidatePayload((string)obj["kind"]!, (string)obj["topic"]!, obj["payload"]);
    }

    public void Validate(Envelope message) => Validate(message.ToNode());

    public void ValidateEnvelope(JsonNode? message) =>
        Check(Load("envelope.schema.json")!, message, "envelope");

    public void ValidatePayload(string kind, string topic, JsonNode? payload)
    {
        var schema = Load(PayloadSchemaPath(kind, topic))
                     ?? throw new BbpException("E_UNKNOWN_TOPIC", $"no schema for topic '{topic}'");
        Check(schema, payload, $"payload[{topic}]");
    }

    private static string PayloadSchemaPath(string kind, string topic) =>
        Protocol.Kinds.IsSession(kind)
            ? Path.Combine("session", kind + ".schema.json")
            : Path.Combine("topics", topic + ".schema.json");

    private JsonSchema? Load(string relativePath) =>
        _cache.GetOrAdd(relativePath, rel =>
        {
            var path = Path.Combine(_dir, rel);
            return File.Exists(path) ? JsonSchema.FromFile(path) : null;
        });

    private static void Check(JsonSchema schema, JsonNode? instance, string where)
    {
        // A fresh options object per call keeps each evaluation's schema registry
        // private, so schemas sharing $id roots never collide.
        var result = schema.Evaluate(instance, new EvaluationOptions { OutputFormat = OutputFormat.List });
        if (result.IsValid)
            return;
        var failed = result.Details.FirstOrDefault(d => d.HasErrors) ?? result;
        var error = failed.Errors?.Values.FirstOrDefault() ?? "invalid";
        var location = failed.InstanceLocation.ToString();
        throw new BbpException("E_BAD_PAYLOAD", $"{where} {(location.Length == 0 ? "(root)" : location)}: {error}");
    }
}
