using System.Collections.Concurrent;

namespace Jarvis.Launcher.Tests;

/// <summary>A child process that lives until it exits or is killed.</summary>
internal sealed class FakeChild(int id, ProcessSpec spec) : IChildProcess
{
    private readonly TaskCompletionSource<int> _exit = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public ProcessSpec Spec { get; } = spec;
    public int Id { get; } = id;
    public Task<int> Exited => _exit.Task;
    public bool HasExited => _exit.Task.IsCompleted;
    public List<string> Input { get; } = [];
    public bool InputClosed { get; private set; }
    public bool Killed { get; private set; }

    /// <summary>What it does when told "shutdown" (the Brain: close BBP 1000, exit 0).</summary>
    public Action<FakeChild>? OnShutdown { get; set; }

    public void SendLine(string line)
    {
        Input.Add(line);
        if (line == "shutdown")
            OnShutdown?.Invoke(this);
    }

    public void CloseInput() => InputClosed = true;

    public void KillTree()
    {
        Killed = true;
        Exit(-1);
    }

    /// <summary>Called as it exits (before anything awaiting it runs).</summary>
    public Action<FakeChild>? Exiting { get; set; }

    public void Exit(int code)
    {
        if (!_exit.Task.IsCompleted)
            Exiting?.Invoke(this);
        _exit.TrySetResult(code);
    }
}

internal sealed class FakeHost : IProcessHost
{
    private int _pid = 1000;

    public ConcurrentQueue<FakeChild> Started { get; } = new();

    /// <summary>Children in the order they exited.</summary>
    public ConcurrentQueue<FakeChild> ExitOrder { get; } = new();
    public List<ProcessSpec> Runs { get; } = [];
    public HashSet<string> Files { get; } = new(StringComparer.OrdinalIgnoreCase);
    public Dictionary<string, string> OnPath { get; } = new(StringComparer.OrdinalIgnoreCase);
    public string PreflightOutput { get; set; } = Report();
    public bool PreflightHangs { get; set; }

    /// <summary>Preflight runs until cancelled (a slow first import).</summary>
    public bool PreflightBlocks { get; set; }

    /// <summary>Called for each started process (e.g. Ollama comes up).</summary>
    public Action<FakeChild>? OnStart { get; set; }

    public IEnumerable<FakeChild> Named(string exeName) =>
        Started.Where(c => Path.GetFileName(c.Spec.FileName).Equals(exeName, StringComparison.OrdinalIgnoreCase));

    public IEnumerable<FakeChild> Brains => Started.Where(c => c.Spec.Arguments.SequenceEqual(["-m", "core_v3"]));

    public IChildProcess Start(ProcessSpec spec)
    {
        var child = new FakeChild(Interlocked.Increment(ref _pid), spec);
        child.Exiting = ExitOrder.Enqueue;
        Started.Enqueue(child);
        OnStart?.Invoke(child);
        return child;
    }

    public async Task<RunResult> RunAsync(ProcessSpec spec, TimeSpan timeout, CancellationToken ct)
    {
        lock (Runs) Runs.Add(spec);
        if (PreflightBlocks)
            await Task.Delay(Timeout.Infinite, ct);
        return await Task.FromResult(PreflightHangs ? new RunResult(-1, "", true) : new RunResult(0, PreflightOutput, false));
    }

    public bool FileExists(string path) => Files.Contains(path);

    public string? FindOnPath(string exe) => OnPath.GetValueOrDefault(exe);

    public static string Report(bool phonebox = false, string required = "\"qwen\", \"nomic-embed-text\"",
                                string optional = "\"qwen-small\"", string warnings = "") =>
        "{\"mode\": \"wake\", \"ollama_url\": \"http://localhost:11434\", " +
        $"\"models\": {{\"required\": [{required}], \"optional\": [{optional}]}}, " +
        "\"bbp\": {\"enabled\": true, \"port\": 8765}, " +
        $"\"phonebox\": {{\"enabled\": {(phonebox ? "true" : "false")}, \"url\": \"http://127.0.0.1:8780\"}}, " +
        $"\"warnings\": [{warnings}]}}";
}

internal sealed class FakeNet : INetwork
{
    public ConcurrentDictionary<int, bool> Listening { get; } = new();

    /// <summary>null: Ollama doesn't answer.</summary>
    public List<string>? Models { get; set; } = ["qwen", "nomic-embed-text:latest", "qwen-small"];

    public Task<bool> IsListeningAsync(string host, int port, CancellationToken ct) =>
        Task.FromResult(Listening.GetValueOrDefault(port));

    public Task<IReadOnlyList<string>?> OllamaModelsAsync(Uri baseUrl, CancellationToken ct) =>
        Task.FromResult<IReadOnlyList<string>?>(Models?.ToList());
}

/// <summary>Time that moves only when the launcher waits.</summary>
internal sealed class FakeClock : ILauncherClock
{
    private readonly object _gate = new();
    private DateTimeOffset _now = new(2026, 11, 16, 9, 0, 0, TimeSpan.Zero);

    public List<TimeSpan> Waits { get; } = [];

    /// <summary>When set, waits of 2 s or more (restart backoff) block on it.</summary>
    public TaskCompletionSource? HoldLongWaits { get; set; }

    public DateTimeOffset Now
    {
        get { lock (_gate) return _now; }
    }

    public void Advance(TimeSpan by)
    {
        lock (_gate) _now += by;
    }

    public async Task Delay(TimeSpan delay, CancellationToken ct)
    {
        lock (_gate)
        {
            Waits.Add(delay);
            _now += delay;
        }
        if (delay >= TimeSpan.FromSeconds(2) && HoldLongWaits is { } hold)
            await hold.Task;
        await Task.Yield();
    }
}
