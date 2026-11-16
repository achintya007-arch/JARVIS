namespace Jarvis.Launcher;

public enum BrainStatus { Idle, Checking, StartingOllama, Starting, Running, Attached, Restarting, Failed, Stopping, Stopped }

/// <summary>Where the Brain is, for the tray and HUD. <c>Owned</c>: the Body started it.</summary>
public sealed record LauncherState(BrainStatus Status, string Detail, bool Owned);

public enum NoticeLevel { Info, Warn, Alert }

public sealed record LauncherNotice(NoticeLevel Level, string Title, string Body);

public sealed record LauncherTimings
{
    public TimeSpan Poll { get; init; } = TimeSpan.FromMilliseconds(500);
    public TimeSpan PreflightTimeout { get; init; } = TimeSpan.FromSeconds(60);
    public TimeSpan OllamaStartWait { get; init; } = TimeSpan.FromSeconds(20);
    public TimeSpan BrainStartWait { get; init; } = TimeSpan.FromSeconds(120);

    /// <summary>After "shutdown": how long to wait for the Brain to close BBP with 1000.</summary>
    public TimeSpan CloseWait { get; init; } = TimeSpan.FromSeconds(5);

    /// <summary>Then: how long to wait for the process to exit before killing it.</summary>
    public TimeSpan ExitWait { get; init; } = TimeSpan.FromSeconds(5);
}

/// <summary>
/// Starts JARVIS from the Body:
/// <list type="number">
/// <item>A Brain already listening on the BBP port (you started one by hand) is
/// used as it is and never stopped from here.</item>
/// <item>Otherwise: read config.yaml through <c>python -m tools.preflight</c>, make
/// sure Ollama answers (starting <c>ollama serve</c> if it's installed but not
/// running) and has the required models, then start the Brain hidden, with
/// <c>JARVIS_MANAGED=1</c> so its stdin is this launcher's control pipe.</item>
/// <item>PhoneBox, when config.yaml enables it and it isn't running, is started too.</item>
/// </list>
/// A Brain or PhoneBox that stops unexpectedly is restarted with backoff
/// (<see cref="RestartPolicy"/>: 3 restarts in 5 minutes, then it stays stopped
/// and says so). On quit, only what this launcher started is stopped: the Brain
/// is asked to shut down, given time to close BBP with 1000 and exit, and killed
/// only if it hangs; PhoneBox and Ollama are then stopped if it started them.
/// </summary>
public sealed class Launcher
{
    private readonly LauncherSettings _s;
    private readonly IProcessHost _host;
    private readonly INetwork _net;
    private readonly ILauncherClock _clock;
    private readonly LauncherTimings _t;
    private readonly Action<string> _log;
    private readonly RestartPolicy _brainPolicy;
    private readonly RestartPolicy _phonePolicy;
    private readonly SemaphoreSlim _gate = new(1, 1);

    /// <summary>Cancelled by quit: a start in progress (preflight, waiting for
    /// Ollama or the Brain) stops at once instead of finishing first.</summary>
    private readonly CancellationTokenSource _quit = new();

    private IChildProcess? _brain;
    private IChildProcess? _ollama;
    private IChildProcess? _phonebox;
    private bool _attached;
    private bool _stopping;
    private volatile bool _quitRequested;
    private volatile bool _linked;
    private int _gen;
    private int _phoneGen;
    private bool _warned;
    private TaskCompletionSource? _closed;

    public Launcher(LauncherSettings settings, IProcessHost host, INetwork network, ILauncherClock? clock = null,
                    LauncherTimings? timings = null, Action<string>? log = null,
                    RestartPolicy? brainPolicy = null, RestartPolicy? phoneBoxPolicy = null)
    {
        _s = settings;
        _host = host;
        _net = network;
        _clock = clock ?? SystemClock.Instance;
        _t = timings ?? new LauncherTimings();
        _log = log ?? (_ => { });
        _brainPolicy = brainPolicy ?? new RestartPolicy();
        _phonePolicy = phoneBoxPolicy ?? new RestartPolicy();
    }

    public LauncherState State { get; private set; } = new(BrainStatus.Idle, "", false);

    public event Action<LauncherState>? StateChanged;
    public event Action<LauncherNotice>? Notice;

    /// <summary>The Brain now listens on the BBP port: connect without waiting for backoff.</summary>
    public event Action? BrainListening;

    public bool OwnsBrain => _brain is not null;
    public bool Attached => _attached;
    public bool StartedOllama => _ollama is not null;
    public bool StartedPhoneBox => _phonebox is not null;

    /// <summary>The Body's BBP session is up (welcome received).</summary>
    public void LinkUp() => _linked = true;

    /// <summary>The Body's BBP session ended with this close code (null: dropped).</summary>
    public void LinkClosed(int? code)
    {
        _linked = false;
        if (code == 1000)
            _closed?.TrySetResult();
    }

    // ── Start ─────────────────────────────────────────────────────────────────

    public async Task StartAsync(CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct);
        try
        {
            if (_stopping)
                return;
            Set(BrainStatus.Checking, "Looking for a running JARVIS", false);
            if (await _net.IsListeningAsync("127.0.0.1", _s.BbpPort, ct))
            {
                _attached = true;
                _log($"launcher: port {_s.BbpPort} is in use: using the JARVIS already running (it won't be stopped from here)");
                Set(BrainStatus.Attached, "Using the JARVIS that was already running", false);
                BrainListening?.Invoke();
                if (await ReadConfigAsync(ct, quiet: true) is { } info)
                    await EnsurePhoneBoxAsync(info, ct);
                return;
            }
            await StartBrainCoreAsync(ct);
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>Tray "Restart brain": also "start again" after it gave up or failed.</summary>
    public async Task RestartBrainAsync()
    {
        await _gate.WaitAsync();
        try
        {
            if (_stopping)
                return;
            if (_attached)
            {
                if (await _net.IsListeningAsync("127.0.0.1", _s.BbpPort, CancellationToken.None))
                {
                    Raise(NoticeLevel.Info, "JARVIS was started by hand",
                          "So the HUD won't stop it. Stop it where you started it, then choose Restart brain to start one from here.");
                    return;
                }
                _attached = false;                    // it's gone: from now on, the HUD runs its own
            }
            if (_brain is not null)
                await StopBrainCoreAsync();
            _gen++;
            _brainPolicy.Reset();
            await StartBrainCoreAsync(CancellationToken.None);
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task StartBrainCoreAsync(CancellationToken ct)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, _quit.Token);
        try
        {
            await StartBrainStepsAsync(linked.Token);
        }
        catch (OperationCanceledException) when (_quit.IsCancellationRequested)
        {
            _log("launcher: start cut short by quit");
        }
    }

    private async Task StartBrainStepsAsync(CancellationToken ct)
    {
        var info = await ReadConfigAsync(ct);
        if (info is null)
            return;
        if (!_warned)
        {
            _warned = true;
            foreach (var warning in info.Warnings)
                Raise(NoticeLevel.Warn, "JARVIS preflight", Capitalize(warning));
        }
        if (await EnsureOllamaAsync(info, ct) is { } ollamaProblem)
        {
            Fail("JARVIS didn't start", ollamaProblem);
            return;
        }
        if (await MissingModelsAsync(info, ct) is { } modelProblem)
        {
            Fail("JARVIS didn't start", modelProblem);
            return;
        }
        await EnsurePhoneBoxAsync(info, ct);
        await SpawnBrainAsync(ct);
    }

    private async Task<BrainConfigInfo?> ReadConfigAsync(CancellationToken ct, bool quiet = false)
    {
        void Problem(string title, string detail)
        {
            if (quiet)
                _log($"launcher: {title}: {detail}");
            else
                Fail(title, detail);
        }

        if (!LauncherSettings.LooksLikeJarvisRoot(_s.JarvisRoot))
        {
            Problem("JARVIS isn't set up",
                    $"{_s.JarvisRoot} isn't the jarvis_v1 folder (there's no core_v3\\__main__.py). Set \"jarvis_root\" in body.settings.json.");
            return null;
        }
        var (python, pythonProblem) = ResolvePython();
        if (python is null)
        {
            Problem("Python not found", pythonProblem!);
            return null;
        }
        RunResult result;
        try
        {
            result = await _host.RunAsync(new ProcessSpec(python, ["-m", "tools.preflight"], _s.JarvisRoot, BrainEnvironment()),
                                          _t.PreflightTimeout, ct);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            Problem("Python couldn't run", $"{python}: {e.Message}");
            return null;
        }
        if (result.TimedOut)
        {
            Problem("JARVIS's preflight hung", $"python -m tools.preflight didn't finish in {_t.PreflightTimeout.TotalSeconds:0} s.");
            return null;
        }
        var (info, error) = BrainConfigInfo.Parse(result.Output);
        if (info is null)
            Problem("JARVIS's settings have a problem", Capitalize(error ?? "unknown error") + " (config.yaml in " + _s.JarvisRoot + ").");
        return info;
    }

    /// <summary>The python to use: python_path, else the venv, else python on PATH.</summary>
    internal (string? Path, string? Problem) ResolvePython()
    {
        if (!string.IsNullOrWhiteSpace(_s.PythonPath))
            return _host.FileExists(_s.PythonPath)
                ? (_s.PythonPath, null)
                : (null, $"\"python_path\" in body.settings.json is {_s.PythonPath}, which doesn't exist.");
        var venv = Path.Combine(_s.JarvisRoot, "venv", "Scripts", "python.exe");
        if (_host.FileExists(venv))
            return (venv, null);
        if (_host.FindOnPath("python.exe") is { } system)
        {
            _log($"launcher: no venv at {venv}; using {system} (voice packages may be missing)");
            return (system, null);
        }
        return (null, $"There's no venv at {venv} and no python on PATH. Create the venv (see the README) or set \"python_path\" in body.settings.json.");
    }

    private Dictionary<string, string> BrainEnvironment() => new()
    {
        ["JARVIS_MANAGED"] = "1",
        ["JARVIS_BBP_PORT"] = _s.BbpPort.ToString(System.Globalization.CultureInfo.InvariantCulture),
        ["JARVIS_MODE"] = LauncherSettings.Modes.Contains(_s.BrainMode) ? _s.BrainMode : "wake",
        ["PYTHONUNBUFFERED"] = "1",
        ["PYTHONIOENCODING"] = "utf-8",
    };

    // ── Ollama ────────────────────────────────────────────────────────────────

    private async Task<string?> EnsureOllamaAsync(BrainConfigInfo info, CancellationToken ct)
    {
        if (!Uri.TryCreate(info.OllamaUrl, UriKind.Absolute, out var url))
            return $"llm.base_url in config.yaml (\"{info.OllamaUrl}\") isn't a URL.";
        if (await _net.OllamaModelsAsync(url, ct) is not null)
            return null;                                          // running: yours or ours
        if (!url.IsLoopback)
            return $"Ollama at {url} isn't answering, and it isn't on this PC, so JARVIS can't start it.";
        var exe = FindOllama();
        if (exe is null)
            return "Ollama isn't running, and it isn't installed where JARVIS looks (PATH, %LOCALAPPDATA%\\Programs\\Ollama). " +
                   "Install it from ollama.com, or set \"ollama_path\" in body.settings.json.";

        Set(BrainStatus.StartingOllama, "Starting Ollama", false);
        Kill(_ollama);
        try
        {
            _ollama = _host.Start(new ProcessSpec(exe, ["serve"], Path.GetDirectoryName(exe)!,
                                                  new Dictionary<string, string> { ["OLLAMA_HOST"] = $"{url.Host}:{url.Port}" },
                                                  "ollama", EndsWithLauncher: true));
        }
        catch (Exception e)
        {
            _ollama = null;
            return $"Ollama couldn't be started ({exe}): {e.Message}";
        }
        _log($"launcher: started ollama serve (pid {_ollama.Id})");
        var deadline = _clock.Now + _t.OllamaStartWait;
        while (_clock.Now < deadline && !_quitRequested)
        {
            if (_ollama.HasExited)
            {
                _ollama = null;
                return "Ollama started but stopped again straight away (see ollama.log in the logs folder).";
            }
            await _clock.Delay(_t.Poll, ct);
            if (await _net.OllamaModelsAsync(url, ct) is not null)
                return null;
        }
        Kill(_ollama);
        _ollama = null;
        return $"Ollama didn't answer within {_t.OllamaStartWait.TotalSeconds:0} s of starting (see ollama.log in the logs folder).";
    }

    private string? FindOllama()
    {
        if (!string.IsNullOrWhiteSpace(_s.OllamaPath))
            return _host.FileExists(_s.OllamaPath) ? _s.OllamaPath : null;
        if (_host.FindOnPath("ollama.exe") is { } onPath)
            return onPath;
        var installed = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                     "Programs", "Ollama", "ollama.exe");
        return _host.FileExists(installed) ? installed : null;
    }

    private async Task<string?> MissingModelsAsync(BrainConfigInfo info, CancellationToken ct)
    {
        var pulled = await _net.OllamaModelsAsync(new Uri(info.OllamaUrl), ct) ?? [];
        foreach (var model in info.Models.Optional.Where(m => !BrainConfigInfo.Has(pulled, m)))
            Raise(NoticeLevel.Warn, "Fallback model missing",
                  $"{model} isn't pulled, so JARVIS can't switch to a lighter model when memory runs low. Pull it with: ollama pull {model}");
        var missing = info.Models.Required.Where(m => !BrainConfigInfo.Has(pulled, m)).ToList();
        return missing.Count == 0
            ? null
            : $"Ollama is missing {string.Join(" and ", missing)}. Pull {(missing.Count == 1 ? "it" : "them")} with: " +
              string.Join("; ", missing.Select(m => "ollama pull " + m)) + ".";
    }

    // ── Brain ─────────────────────────────────────────────────────────────────

    private async Task SpawnBrainAsync(CancellationToken ct)
    {
        var (python, _) = ResolvePython();
        IChildProcess proc;
        try
        {
            proc = _host.Start(new ProcessSpec(python!, ["-m", "core_v3"], _s.JarvisRoot, BrainEnvironment(), "brain",
                                               RedirectInput: true));
        }
        catch (Exception e)
        {
            Fail("JARVIS didn't start", $"Python couldn't be started: {e.Message}");
            return;
        }
        _brain = proc;
        var gen = ++_gen;
        _log($"launcher: started the Brain (pid {proc.Id}, {BrainEnvironment()["JARVIS_MODE"]} mode, port {_s.BbpPort})");
        Set(BrainStatus.Starting, "Starting JARVIS", true);
        _ = MonitorBrainAsync(proc, gen);

        var deadline = _clock.Now + _t.BrainStartWait;
        while (!proc.HasExited && !_quitRequested)
        {
            if (await _net.IsListeningAsync("127.0.0.1", _s.BbpPort, ct))
            {
                Set(BrainStatus.Running, "JARVIS is running", true);
                BrainListening?.Invoke();
                return;
            }
            if (_clock.Now >= deadline)
            {
                Set(BrainStatus.Running, "JARVIS is running but slow to start; see brain.log", true);
                Raise(NoticeLevel.Warn, "JARVIS is slow to start",
                      $"It hasn't opened port {_s.BbpPort} after {_t.BrainStartWait.TotalSeconds:0} s. Details are in brain.log.");
                return;
            }
            await _clock.Delay(_t.Poll, ct);
        }
    }

    private async Task MonitorBrainAsync(IChildProcess proc, int gen)
    {
        int code;
        try
        {
            code = await proc.Exited;
        }
        catch (Exception)
        {
            code = -1;
        }
        TimeSpan wait;
        await _gate.WaitAsync();
        try
        {
            if (gen != _gen || _stopping)
                return;                                           // we stopped it
            _brain = null;
            _log($"launcher: the Brain exited unexpectedly (exit code {code})");
            if (_brainPolicy.Next(_clock.Now) is not { } next)
            {
                var why = $"It stopped unexpectedly {_brainPolicy.MaxRestarts + 1} times in {_brainPolicy.Window.TotalMinutes:0} minutes, " +
                          "so it won't be restarted again. Look at brain.log (tray: Open logs folder), then choose Restart brain.";
                Set(BrainStatus.Failed, why, false);
                Raise(NoticeLevel.Alert, "JARVIS keeps stopping", why);
                return;
            }
            wait = next;
            var detail = $"Exit code {code}. Restarting in {wait.TotalSeconds:0} s (restart {_brainPolicy.Recent} of {_brainPolicy.MaxRestarts}); details in brain.log.";
            Set(BrainStatus.Restarting, "JARVIS stopped unexpectedly. " + detail, false);
            Raise(NoticeLevel.Warn, "JARVIS stopped unexpectedly", detail);
        }
        finally
        {
            _gate.Release();
        }

        await _clock.Delay(wait, CancellationToken.None);
        await _gate.WaitAsync();
        try
        {
            if (gen == _gen && !_stopping)
                await StartBrainCoreAsync(CancellationToken.None);
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>Ask the Brain to shut down; wait for BBP to close with 1000 and the
    /// process to exit; kill it only if it hangs. True when it exited by itself.</summary>
    private async Task<bool> StopBrainCoreAsync()
    {
        var proc = _brain!;
        _brain = null;
        _gen++;                                                   // its exit is expected now
        Set(BrainStatus.Stopping, "Stopping JARVIS", true);
        var closed = _closed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var linked = _linked;
        try
        {
            proc.SendLine("shutdown");
            proc.CloseInput();
        }
        catch (Exception e) when (e is IOException or InvalidOperationException)
        {
            // already gone
        }
        if (linked)
            await Task.WhenAny(closed.Task, proc.Exited, _clock.Delay(_t.CloseWait, CancellationToken.None));
        var exited = proc.HasExited
                     || await Task.WhenAny(proc.Exited, _clock.Delay(_t.ExitWait, CancellationToken.None)) == proc.Exited;
        _closed = null;
        if (exited)
        {
            _log($"launcher: the Brain shut down cleanly{(closed.Task.IsCompleted ? " (BBP closed 1000)" : "")}");
            return true;
        }
        _log($"launcher: the Brain was still running {(_t.CloseWait + _t.ExitWait).TotalSeconds:0} s after being asked to stop; killed it");
        Kill(proc);
        return false;
    }

    // ── PhoneBox ──────────────────────────────────────────────────────────────

    private async Task EnsurePhoneBoxAsync(BrainConfigInfo info, CancellationToken ct)
    {
        if (!info.Phonebox.Enabled || _phonebox is { HasExited: false } || _quitRequested)
            return;
        if (!Uri.TryCreate(info.Phonebox.Url, UriKind.Absolute, out var url) || !url.IsLoopback)
        {
            _log($"launcher: phonebox.url {info.Phonebox.Url} isn't on this PC; not starting PhoneBox");
            return;
        }
        if (await _net.IsListeningAsync(LoopbackHost(url), url.Port, ct))
        {
            _log("launcher: PhoneBox is already running; leaving it alone");
            return;
        }
        var exe = FindPhoneBox();
        if (exe is null)
        {
            Raise(NoticeLevel.Warn, "PhoneBox not started",
                  "phonebox is enabled in config.yaml, but PhoneBoxAgent.exe isn't in %LOCALAPPDATA%\\PhoneBox\\app. " +
                  "Set \"phonebox_path\" in body.settings.json, or start PhoneBox yourself.");
            return;
        }
        StartPhoneBox(exe, url.Port);
    }

    private void StartPhoneBox(string exe, int port)
    {
        try
        {
            _phonebox = _host.Start(new ProcessSpec(exe, ["--port", port.ToString(System.Globalization.CultureInfo.InvariantCulture)],
                                                    Path.GetDirectoryName(exe)!, null, "phonebox", EndsWithLauncher: true));
        }
        catch (Exception e)
        {
            _phonebox = null;
            Raise(NoticeLevel.Warn, "PhoneBox not started", $"{exe}: {e.Message}");
            return;
        }
        _log($"launcher: started PhoneBox (pid {_phonebox.Id}, port {port})");
        _ = MonitorPhoneBoxAsync(_phonebox, ++_phoneGen, exe, port);
    }

    private async Task MonitorPhoneBoxAsync(IChildProcess proc, int gen, string exe, int port)
    {
        var code = await proc.Exited.ContinueWith(t => t.IsCompletedSuccessfully ? t.Result : -1, TaskScheduler.Default);
        TimeSpan wait;
        await _gate.WaitAsync();
        try
        {
            if (gen != _phoneGen || _stopping)
                return;
            _phonebox = null;
            _log($"launcher: PhoneBox exited unexpectedly (exit code {code})");
            if (_phonePolicy.Next(_clock.Now) is not { } next)
            {
                Raise(NoticeLevel.Warn, "PhoneBox keeps stopping",
                      "It won't be restarted again until JARVIS starts next time. Details are in phonebox.log.");
                return;
            }
            wait = next;
        }
        finally
        {
            _gate.Release();
        }
        await _clock.Delay(wait, CancellationToken.None);
        await _gate.WaitAsync();
        try
        {
            if (gen == _phoneGen && !_stopping)
                StartPhoneBox(exe, port);
        }
        finally
        {
            _gate.Release();
        }
    }

    private string? FindPhoneBox()
    {
        if (!string.IsNullOrWhiteSpace(_s.PhoneBoxPath))
            return _host.FileExists(_s.PhoneBoxPath) ? _s.PhoneBoxPath : null;
        var installed = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                     "PhoneBox", "app", "PhoneBoxAgent.exe");
        return _host.FileExists(installed) ? installed : null;
    }

    // ── Quit ──────────────────────────────────────────────────────────────────

    /// <summary>Stop what this launcher started (Brain first), never what it found running.</summary>
    public async Task ShutdownAsync()
    {
        _quitRequested = true;
        _quit.Cancel();
        await _gate.WaitAsync();
        try
        {
            if (_stopping)
                return;
            _stopping = true;
            _gen++;
            _phoneGen++;
            if (_brain is not null)
                await StopBrainCoreAsync();
            if (_phonebox is not null)
            {
                _log("launcher: stopping PhoneBox (started by JARVIS)");
                Kill(_phonebox);
                _phonebox = null;
            }
            if (_ollama is not null)
            {
                _log("launcher: stopping Ollama (started by JARVIS)");
                Kill(_ollama);
                _ollama = null;
            }
            Set(BrainStatus.Stopped, "Stopped", false);
        }
        finally
        {
            _gate.Release();
        }
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private void Kill(IChildProcess? proc)
    {
        if (proc is null || proc.HasExited)
            return;
        try
        {
            proc.KillTree();
        }
        catch (Exception e)
        {
            _log($"launcher: couldn't kill pid {proc.Id}: {e.Message}");
        }
    }

    private static string LoopbackHost(Uri url) => url.Host is "localhost" ? "127.0.0.1" : url.Host;

    private void Set(BrainStatus status, string detail, bool owned)
    {
        State = new LauncherState(status, detail, owned);
        StateChanged?.Invoke(State);
    }

    private void Fail(string title, string detail)
    {
        _log($"launcher: {title}: {detail}");
        Set(BrainStatus.Failed, detail, false);
        Raise(NoticeLevel.Alert, title, detail);
    }

    private void Raise(NoticeLevel level, string title, string body) => Notice?.Invoke(new LauncherNotice(level, title, body));

    private static string Capitalize(string s) => s.Length == 0 ? s : char.ToUpperInvariant(s[0]) + s[1..];
}
