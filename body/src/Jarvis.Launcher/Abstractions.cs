namespace Jarvis.Launcher;

/// <summary>A program to run hidden (no console window). Output goes to
/// <c>%LOCALAPPDATA%\JARVIS\&lt;LogName&gt;.log</c> when LogName is set.</summary>
public sealed record ProcessSpec(
    string FileName,
    IReadOnlyList<string> Arguments,
    string WorkingDirectory,
    IReadOnlyDictionary<string, string>? Environment = null,
    string? LogName = null,
    bool RedirectInput = false,
    bool EndsWithLauncher = false);

public sealed record RunResult(int ExitCode, string Output, bool TimedOut);

/// <summary>A child process the launcher started.</summary>
public interface IChildProcess
{
    int Id { get; }

    /// <summary>Completes with the exit code when the process ends.</summary>
    Task<int> Exited { get; }

    bool HasExited { get; }

    /// <summary>Write a line to its stdin (only if started with RedirectInput).</summary>
    void SendLine(string line);

    void CloseInput();

    /// <summary>Kill it and everything it started.</summary>
    void KillTree();
}

public interface IProcessHost
{
    /// <summary>Start hidden. Throws if it can't (missing file, access denied).</summary>
    IChildProcess Start(ProcessSpec spec);

    /// <summary>Run to completion and capture stdout; killed after <paramref name="timeout"/>.</summary>
    Task<RunResult> RunAsync(ProcessSpec spec, TimeSpan timeout, CancellationToken ct);

    bool FileExists(string path);

    /// <summary>Full path of an executable found on PATH, or null.</summary>
    string? FindOnPath(string exe);
}

public interface INetwork
{
    /// <summary>Something accepts TCP connections on host:port.</summary>
    Task<bool> IsListeningAsync(string host, int port, CancellationToken ct);

    /// <summary>The models Ollama has pulled, or null when it doesn't answer.</summary>
    Task<IReadOnlyList<string>?> OllamaModelsAsync(Uri baseUrl, CancellationToken ct);
}

public interface ILauncherClock
{
    DateTimeOffset Now { get; }
    Task Delay(TimeSpan delay, CancellationToken ct);
}

public sealed class SystemClock : ILauncherClock
{
    public static SystemClock Instance { get; } = new();
    public DateTimeOffset Now => DateTimeOffset.Now;
    public Task Delay(TimeSpan delay, CancellationToken ct) => Task.Delay(delay, ct);
}
