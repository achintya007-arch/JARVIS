using System.Text.Json;
using System.Text.Json.Serialization;

namespace Jarvis.Launcher;

/// <summary>What <c>python -m tools.preflight</c> reports from config.yaml.</summary>
public sealed record BrainConfigInfo
{
    public string Mode { get; init; } = "wake";
    public string OllamaUrl { get; init; } = "http://localhost:11434";
    public ModelLists Models { get; init; } = new();
    public PhoneBoxInfo Phonebox { get; init; } = new();
    public IReadOnlyList<string> Warnings { get; init; } = [];

    public sealed record ModelLists
    {
        public IReadOnlyList<string> Required { get; init; } = [];
        public IReadOnlyList<string> Optional { get; init; } = [];
    }

    public sealed record PhoneBoxInfo
    {
        public bool Enabled { get; init; }
        public string Url { get; init; } = "http://127.0.0.1:8780";
    }

    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Skip,
    };

    /// <summary>The report, or the problem with it.</summary>
    public static (BrainConfigInfo? Info, string? Error) Parse(string output)
    {
        var line = output.Split('\n').Select(l => l.Trim()).LastOrDefault(l => l.StartsWith('{'));
        if (line is null)
            return (null, "the preflight printed nothing JARVIS understands");
        try
        {
            using var doc = JsonDocument.Parse(line);
            if (doc.RootElement.TryGetProperty("error", out var error))
                return (null, error.GetString());
            return (doc.RootElement.Deserialize<BrainConfigInfo>(Json), null);
        }
        catch (JsonException e)
        {
            return (null, $"the preflight's answer isn't valid JSON ({e.Message})");
        }
    }

    /// <summary>Is <paramref name="model"/> among what Ollama has? "name" matches "name:latest".</summary>
    public static bool Has(IEnumerable<string> pulled, string model) =>
        pulled.Any(p => string.Equals(p, model, StringComparison.OrdinalIgnoreCase)
                        || (!model.Contains(':') && string.Equals(p, model + ":latest", StringComparison.OrdinalIgnoreCase)));
}
