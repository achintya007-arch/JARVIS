namespace Jarvis.Launcher;

/// <summary>What the launcher needs from body.settings.json.</summary>
public sealed record LauncherSettings
{
    /// <summary>The jarvis_v1 folder (holds core_v3, config.yaml, venv).</summary>
    public required string JarvisRoot { get; init; }

    /// <summary>Python to run the Brain with; default: the venv inside JarvisRoot.</summary>
    public string? PythonPath { get; init; }

    /// <summary>"wake" (say "hey jarvis") or "text" (type in the HUD).</summary>
    public string BrainMode { get; init; } = "wake";

    /// <summary>The BBP port the Body connects to; the Brain is told to listen there.</summary>
    public int BbpPort { get; init; } = 8765;

    /// <summary>ollama.exe; default: on PATH, then %LOCALAPPDATA%\Programs\Ollama.</summary>
    public string? OllamaPath { get; init; }

    /// <summary>PhoneBoxAgent.exe; default: %LOCALAPPDATA%\PhoneBox\app.</summary>
    public string? PhoneBoxPath { get; init; }

    public static readonly string[] Modes = ["wake", "text"];

    /// <summary>A jarvis_v1 folder: it has core_v3\__main__.py.</summary>
    public static bool LooksLikeJarvisRoot(string? dir) =>
        !string.IsNullOrWhiteSpace(dir) && File.Exists(Path.Combine(dir, "core_v3", "__main__.py"));

    /// <summary>The jarvis_v1 folder above (or beside) <paramref name="start"/>, e.g.
    /// when the Body runs from body\src\Jarvis.Body\bin\… inside the repo.</summary>
    public static string? FindJarvisRoot(string start)
    {
        for (var dir = new DirectoryInfo(start); dir is not null; dir = dir.Parent)
        {
            if (LooksLikeJarvisRoot(dir.FullName))
                return dir.FullName;
            var child = Path.Combine(dir.FullName, "jarvis_v1");
            if (LooksLikeJarvisRoot(child))
                return child;
        }
        return null;
    }
}
