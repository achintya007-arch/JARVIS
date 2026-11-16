using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using Bbp.Client;
using Jarvis.Launcher;

namespace Jarvis.Body;

/// <summary>
/// Optional body.settings.json next to the executable (see
/// body.settings.example.json). Everything has a working default: the Brain's
/// default address, the token found under jarvis_root (or by searching parent
/// folders for jarvis_v1/data/bbp_token), and a Brain started by the Body.
/// </summary>
public sealed record BodySettings
{
    public const string FileName = "body.settings.json";

    public string Url { get; init; } = "ws://127.0.0.1:8765/bbp";
    public string? TokenPath { get; init; }

    /// <summary>Global push-to-talk hotkey, e.g. "Ctrl+Alt+J" (Ctrl/Alt/Win + key).</summary>
    public string Hotkey { get; init; } = "Ctrl+Alt+J";

    // ── Launcher ────────────────────────────────────────────────────────────

    /// <summary>The jarvis_v1 folder. Found from the exe's folder when the Body
    /// runs from the repo; otherwise asked once with a folder picker and saved.</summary>
    public string? JarvisRoot { get; init; }

    /// <summary>Default: jarvis_root\venv\Scripts\python.exe.</summary>
    public string? PythonPath { get; init; }

    /// <summary>"wake" (voice, wake word) or "text" (typed in the HUD).</summary>
    public string BrainMode { get; init; } = "wake";

    /// <summary>Start the Brain (and Ollama, PhoneBox) when none is running.</summary>
    public bool AutostartBrain { get; init; } = true;

    public string? OllamaPath { get; init; }
    public string? PhoneboxPath { get; init; }

    public static string DefaultPath => Path.Combine(AppContext.BaseDirectory, FileName);

    public static BodySettings Load(string? path = null)
    {
        path ??= DefaultPath;
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

    /// <summary>Set one key in the settings file, keeping everything else in it
    /// (including keys this version doesn't know).</summary>
    public static void SaveValue(string key, string? value, string? path = null)
    {
        path ??= DefaultPath;
        JsonObject root;
        try
        {
            root = File.Exists(path) ? JsonNode.Parse(File.ReadAllText(path)) as JsonObject ?? [] : [];
        }
        catch (JsonException)
        {
            root = [];
        }
        root[key] = value;
        File.WriteAllText(path, root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + Environment.NewLine);
    }

    /// <summary>The BBP port the Brain should listen on (from <see cref="Url"/>).</summary>
    public int Port => Uri.TryCreate(Url, UriKind.Absolute, out var uri) && uri.Port > 0 ? uri.Port : 8765;

    public LauncherSettings ToLauncher(string jarvisRoot) => new()
    {
        JarvisRoot = jarvisRoot,
        PythonPath = PythonPath,
        BrainMode = BrainMode,
        BbpPort = Port,
        OllamaPath = OllamaPath,
        PhoneBoxPath = PhoneboxPath,
    };

    /// <summary>Re-resolved on every connection attempt: token_path, then the
    /// jarvis_root token, then the usual search from the exe's folder.</summary>
    public string? ReadToken() => TokenLocator.ReadToken(TokenFile());

    public string? TokenFile()
    {
        if (string.IsNullOrWhiteSpace(TokenPath) && !string.IsNullOrWhiteSpace(JarvisRoot))
        {
            var underRoot = Path.Combine(JarvisRoot, "data", "bbp_token");
            if (File.Exists(underRoot))
                return underRoot;
        }
        return TokenLocator.FindPath(TokenPath, AppContext.BaseDirectory);
    }
}
