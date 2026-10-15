using System.IO;
using System.Text.Json;
using Bbp.Client;

namespace Jarvis.Body;

/// <summary>
/// Optional body.settings.json next to the executable (see
/// body.settings.example.json). Everything has a working default: the Brain's
/// default address, and the token found by searching parent folders for
/// jarvis_v1/data/bbp_token.
/// </summary>
public sealed record BodySettings
{
    public string Url { get; init; } = "ws://127.0.0.1:8765/bbp";
    public string? TokenPath { get; init; }

    /// <summary>Global push-to-talk hotkey, e.g. "Ctrl+Alt+J" (Ctrl/Alt/Win + key).</summary>
    public string Hotkey { get; init; } = "Ctrl+Alt+J";

    public static BodySettings Load()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "body.settings.json");
        if (!File.Exists(path))
            return new BodySettings();
        try
        {
            return JsonSerializer.Deserialize<BodySettings>(File.ReadAllText(path), BbpJson.Options)
                   ?? new BodySettings();
        }
        catch (JsonException)
        {
            return new BodySettings();
        }
    }

    /// <summary>Re-resolved on every connection attempt.</summary>
    public string? ReadToken() =>
        TokenLocator.ReadToken(TokenLocator.FindPath(TokenPath, AppContext.BaseDirectory));
}
