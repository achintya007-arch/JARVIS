namespace Jarvis.Launcher.Tests;

public sealed class LauncherTests : IDisposable
{
    private const int Port = 8765;
    private readonly string _root;

    public LauncherTests()
    {
        _root = Path.Combine(Path.GetTempPath(), "jarvis-launcher-tests", Guid.NewGuid().ToString("n"), "jarvis_v1");
        Directory.CreateDirectory(Path.Combine(_root, "core_v3"));
        File.WriteAllText(Path.Combine(_root, "core_v3", "__main__.py"), "");
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(Path.GetDirectoryName(_root)!, recursive: true);
        }
        catch (IOException)
        {
        }
    }

    private sealed class Rig
    {
        public FakeHost Host { get; } = new();
        public FakeNet Net { get; } = new();
        public FakeClock Clock { get; } = new();
        public List<LauncherNotice> Notices { get; } = [];
        public List<LauncherState> States { get; } = [];
        public List<string> Log { get; } = [];
        public Launcher L { get; }

        /// <summary>How a started Brain behaves: comes up on the port, and on
        /// "shutdown" closes BBP with 1000 and exits 0.</summary>
        public bool BrainComesUp { get; set; } = true;
        public bool BrainIgnoresShutdown { get; set; }

        public Rig(string root, LauncherSettings? settings = null, LauncherTimings? timings = null)
        {
            Host.Files.Add(Path.Combine(root, "venv", "Scripts", "python.exe"));
            Host.OnStart = child =>
            {
                if (!child.Spec.Arguments.SequenceEqual(["-m", "core_v3"]))
                    return;
                if (BrainComesUp)
                {
                    Net.Listening[Port] = true;
                    L!.LinkUp();
                }
                child.OnShutdown = c =>
                {
                    if (BrainIgnoresShutdown)
                        return;
                    L!.LinkClosed(1000);
                    Net.Listening[Port] = false;
                    c.Exit(0);
                };
            };
            L = new Launcher(settings ?? new LauncherSettings { JarvisRoot = root }, Host, Net, Clock, timings, Log.Add);
            L.Notice += n => { lock (Notices) Notices.Add(n); };
            L.StateChanged += s => { lock (States) States.Add(s); };
        }

        public FakeChild Brain => Host.Brains.Last();

        public void CrashBrain(int code = 1)
        {
            Net.Listening[Port] = false;
            L.LinkClosed(null);
            Brain.Exit(code);
        }

        public bool Said(string text)
        {
            lock (Notices) return Notices.Any(n => n.Title.Contains(text) || n.Body.Contains(text));
        }
    }

    private static async Task Until(Func<bool> condition, string what)
    {
        for (var i = 0; i < 500 && !condition(); i++)
            await Task.Delay(10);
        Assert.True(condition(), what);
    }

    // ── attach vs start ─────────────────────────────────────────────────────

    [Fact]
    public async Task PortAlreadyInUse_AttachesInsteadOfStarting_AndQuitLeavesItRunning()
    {
        var rig = new Rig(_root);
        rig.Net.Listening[Port] = true;                             // you started a Brain by hand
        var listening = 0;
        rig.L.BrainListening += () => listening++;
        await rig.L.StartAsync();

        Assert.Equal(BrainStatus.Attached, rig.L.State.Status);
        Assert.False(rig.L.State.Owned);
        Assert.Empty(rig.Host.Brains);
        Assert.Equal(1, listening);

        await rig.L.RestartBrainAsync();                            // still running: hands off
        Assert.Empty(rig.Host.Brains);
        Assert.True(rig.Said("started by hand"));

        await rig.L.ShutdownAsync();
        Assert.Equal(BrainStatus.Stopped, rig.L.State.Status);
        Assert.Empty(rig.Host.Started);                             // nothing started, nothing killed
    }

    [Fact]
    public async Task NothingRunning_StartsTheBrainHidden_Managed_OnTheBodysPort()
    {
        var rig = new Rig(_root, new LauncherSettings { JarvisRoot = _root, BbpPort = Port, BrainMode = "text" });
        await rig.L.StartAsync();

        Assert.Equal(BrainStatus.Running, rig.L.State.Status);
        Assert.True(rig.L.State.Owned && rig.L.OwnsBrain);
        var spec = Assert.Single(rig.Host.Brains).Spec;
        Assert.Equal(Path.Combine(_root, "venv", "Scripts", "python.exe"), spec.FileName);
        Assert.Equal(_root, spec.WorkingDirectory);
        Assert.Equal("brain", spec.LogName);
        Assert.True(spec.RedirectInput);
        Assert.False(spec.EndsWithLauncher);                        // it stops itself on stdin EOF, cleanly
        Assert.Equal("1", spec.Environment!["JARVIS_MANAGED"]);
        Assert.Equal("8765", spec.Environment["JARVIS_BBP_PORT"]);
        Assert.Equal("text", spec.Environment["JARVIS_MODE"]);
        Assert.Equal(["-m", "tools.preflight"], Assert.Single(rig.Host.Runs).Arguments);
        Assert.Empty(rig.Host.Named("ollama.exe"));                 // it was already answering
    }

    [Fact]
    public async Task AnUnknownMode_FallsBackToWake()
    {
        var rig = new Rig(_root, new LauncherSettings { JarvisRoot = _root, BrainMode = "shout" });
        await rig.L.StartAsync();
        Assert.Equal("wake", rig.Brain.Spec.Environment!["JARVIS_MODE"]);
    }

    // ── preflight failures ──────────────────────────────────────────────────

    [Fact]
    public async Task OllamaNotInstalled_SaysSo_AndStartsNoBrain()
    {
        var rig = new Rig(_root);
        rig.Net.Models = null;
        await rig.L.StartAsync();
        Assert.Equal(BrainStatus.Failed, rig.L.State.Status);
        Assert.Contains("Ollama isn't running", rig.L.State.Detail);
        Assert.Empty(rig.Host.Started);
        Assert.Contains(rig.Notices, n => n.Level == NoticeLevel.Alert && n.Title == "JARVIS didn't start");
    }

    [Fact]
    public async Task OllamaInstalledButStopped_IsStarted_AndStoppedOnQuit_AfterTheBrain()
    {
        var rig = new Rig(_root);
        var ollama = Path.Combine(_root, "ollama.exe");
        rig.Host.OnPath["ollama.exe"] = ollama;
        rig.Net.Models = null;
        var inner = rig.Host.OnStart!;
        rig.Host.OnStart = child =>
        {
            if (child.Spec.FileName == ollama)
                rig.Net.Models = ["qwen", "nomic-embed-text", "qwen-small"];    // it comes up
            inner(child);
        };
        await rig.L.StartAsync();

        var serve = Assert.Single(rig.Host.Named("ollama.exe"));
        Assert.Equal(["serve"], serve.Spec.Arguments);
        Assert.Equal("ollama", serve.Spec.LogName);
        Assert.True(serve.Spec.EndsWithLauncher);                   // ends with the Body, even if it crashes
        Assert.Equal("localhost:11434", serve.Spec.Environment!["OLLAMA_HOST"]);
        Assert.True(rig.L.StartedOllama);
        Assert.Equal(BrainStatus.Running, rig.L.State.Status);

        var brain = rig.Brain;
        await rig.L.ShutdownAsync();
        Assert.False(brain.Killed);                                 // asked, not killed
        Assert.True(serve.Killed);                                  // ours, so stopped
        Assert.Equal([brain, serve], rig.Host.ExitOrder);           // the Brain first
    }

    [Fact]
    public async Task OllamaStartedButNeverAnswers_IsKilled_AndNoBrainStarts()
    {
        var rig = new Rig(_root);
        rig.Host.OnPath["ollama.exe"] = @"C:\x\ollama.exe";
        rig.Net.Models = null;
        await rig.L.StartAsync();
        var serve = Assert.Single(rig.Host.Named("ollama.exe"));
        Assert.True(serve.Killed);
        Assert.Contains("didn't answer within 20 s", rig.L.State.Detail);
        Assert.Empty(rig.Host.Brains);
    }

    [Fact]
    public async Task AnOllamaYouWereRunning_IsNeverStopped()
    {
        var rig = new Rig(_root);
        rig.Host.OnPath["ollama.exe"] = @"C:\x\ollama.exe";
        await rig.L.StartAsync();                                   // Models answer: yours
        await rig.L.ShutdownAsync();
        Assert.Empty(rig.Host.Named("ollama.exe"));
        Assert.False(rig.L.StartedOllama);
    }

    [Fact]
    public async Task ARequiredModelMissing_StopsBeforeTheBrain_WithThePullCommand()
    {
        var rig = new Rig(_root);
        rig.Net.Models = ["qwen-small"];
        await rig.L.StartAsync();
        Assert.Equal(BrainStatus.Failed, rig.L.State.Status);
        Assert.Contains("ollama pull qwen; ollama pull nomic-embed-text", rig.L.State.Detail);
        Assert.Empty(rig.Host.Brains);
    }

    [Fact]
    public async Task AMissingFallbackModel_OnlyWarns()
    {
        var rig = new Rig(_root);
        rig.Net.Models = ["qwen", "nomic-embed-text"];
        await rig.L.StartAsync();
        Assert.Equal(BrainStatus.Running, rig.L.State.Status);
        Assert.Contains(rig.Notices, n => n.Level == NoticeLevel.Warn && n.Body.Contains("ollama pull qwen-small"));
    }

    [Theory]
    [InlineData("{\"error\": \"config.yaml can't be read: bad yaml\"}", "Config.yaml can't be read: bad yaml")]
    [InlineData("Traceback (most recent call last): ...", "printed nothing JARVIS understands")]
    public async Task APreflightThatCantReadTheConfig_StopsBeforeTheBrain(string output, string said)
    {
        var rig = new Rig(_root);
        rig.Host.PreflightOutput = output;
        await rig.L.StartAsync();
        Assert.Equal(BrainStatus.Failed, rig.L.State.Status);
        Assert.Contains(said, rig.L.State.Detail);
        Assert.Empty(rig.Host.Started);
    }

    [Fact]
    public async Task PreflightWarnings_AreShownOnce()
    {
        var rig = new Rig(_root);
        rig.Host.PreflightOutput = FakeHost.Report(warnings: "\"voice packages missing (piper)\"");
        await rig.L.StartAsync();
        rig.CrashBrain();
        await Until(() => rig.Host.Brains.Count() == 2 && rig.L.State.Status == BrainStatus.Running, "restarted");
        Assert.Single(rig.Notices, n => n.Body.StartsWith("Voice packages missing"));
    }

    [Fact]
    public async Task NotAJarvisFolder_OrNoPython_StopsBeforeAnything()
    {
        var bad = new Rig(_root, new LauncherSettings { JarvisRoot = Path.Combine(_root, "nope") });
        await bad.L.StartAsync();
        Assert.Contains("isn't the jarvis_v1 folder", bad.L.State.Detail);
        Assert.Empty(bad.Host.Runs);

        var noPython = new Rig(_root);
        noPython.Host.Files.Clear();
        await noPython.L.StartAsync();
        Assert.Contains("no venv", noPython.L.State.Detail);
        Assert.Empty(noPython.Host.Started);

        var given = new Rig(_root, new LauncherSettings { JarvisRoot = _root, PythonPath = @"C:\py\python.exe" });
        await given.L.StartAsync();
        Assert.Contains("\"python_path\"", given.L.State.Detail);
    }

    [Fact]
    public void Python_FallsBackToPath_WhenThereIsNoVenv()
    {
        var rig = new Rig(_root);
        rig.Host.Files.Clear();
        rig.Host.OnPath["python.exe"] = @"C:\Python310\python.exe";
        Assert.Equal(@"C:\Python310\python.exe", rig.L.ResolvePython().Path);
        Assert.Contains(rig.Log, l => l.Contains("voice packages may be missing"));
    }

    // ── crash restart ───────────────────────────────────────────────────────

    [Fact]
    public async Task ACrash_IsRestartedWithBackoff_ThreeTimesInFiveMinutes_ThenItStops()
    {
        var rig = new Rig(_root);
        await rig.L.StartAsync();
        for (var crash = 1; crash <= 3; crash++)
        {
            rig.CrashBrain(code: 7);
            await Until(() => rig.Host.Brains.Count() == crash + 1 && rig.L.State.Status == BrainStatus.Running,
                        $"restart {crash}");
            Assert.True(rig.Said($"restart {crash} of 3"));
        }
        Assert.Equal([TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(15)],
                     rig.Clock.Waits.Where(w => w >= TimeSpan.FromSeconds(2)));

        rig.CrashBrain();                                           // the 4th inside 5 minutes
        await Until(() => rig.L.State.Status == BrainStatus.Failed, "gave up");
        Assert.Equal(4, rig.Host.Brains.Count());
        Assert.Contains(rig.Notices, n => n.Level == NoticeLevel.Alert && n.Title == "JARVIS keeps stopping");
        Assert.False(rig.L.OwnsBrain);

        await rig.L.RestartBrainAsync();                            // tray: start again, counting afresh
        Assert.Equal(5, rig.Host.Brains.Count());
        Assert.Equal(BrainStatus.Running, rig.L.State.Status);
    }

    [Fact]
    public async Task CrashesFarApart_KeepBeingRestarted()
    {
        var rig = new Rig(_root);
        await rig.L.StartAsync();
        for (var crash = 1; crash <= 5; crash++)
        {
            rig.Clock.Advance(TimeSpan.FromMinutes(6));
            rig.CrashBrain();
            await Until(() => rig.Host.Brains.Count() == crash + 1 && rig.L.State.Status == BrainStatus.Running,
                        $"restart {crash}");
        }
        Assert.DoesNotContain(rig.Notices, n => n.Level == NoticeLevel.Alert);
    }

    [Fact]
    public async Task QuitDuringTheRestartWait_StartsNothingNew()
    {
        var rig = new Rig(_root);
        await rig.L.StartAsync();
        rig.Clock.HoldLongWaits = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        rig.CrashBrain();
        await Until(() => rig.L.State.Status == BrainStatus.Restarting, "waiting to restart");
        await rig.L.ShutdownAsync();
        rig.Clock.HoldLongWaits.SetResult();                        // the wait ends after quit
        await Task.Delay(100);
        Assert.Single(rig.Host.Brains);
        Assert.Equal(BrainStatus.Stopped, rig.L.State.Status);
    }

    [Fact]
    public async Task QuitDuringASlowPreflight_CutsTheStartShort()
    {
        var rig = new Rig(_root);
        rig.Host.PreflightBlocks = true;
        var start = Task.Run(() => rig.L.StartAsync());
        await Until(() => { lock (rig.Host.Runs) return rig.Host.Runs.Count == 1; }, "preflight running");
        var quit = rig.L.ShutdownAsync();
        Assert.Same(quit, await Task.WhenAny(quit, Task.Delay(5000)));
        await start;
        Assert.Empty(rig.Host.Brains);
        Assert.Equal(BrainStatus.Stopped, rig.L.State.Status);
        Assert.Contains(rig.Log, l => l.Contains("cut short by quit"));
    }

    // ── quit ────────────────────────────────────────────────────────────────

    [Fact]
    public async Task Quit_AsksTheBrainToShutDown_WaitsForTheCleanClose_DoesNotKill()
    {
        var rig = new Rig(_root);
        await rig.L.StartAsync();
        var brain = rig.Brain;
        await rig.L.ShutdownAsync();
        Assert.Equal(["shutdown"], brain.Input);
        Assert.True(brain.InputClosed);
        Assert.False(brain.Killed);
        Assert.Contains(rig.Log, l => l.Contains("shut down cleanly (BBP closed 1000)"));
        Assert.Equal(BrainStatus.Stopped, rig.L.State.Status);
    }

    [Fact]
    public async Task Quit_KillsABrainThatHangs()
    {
        var rig = new Rig(_root) { BrainIgnoresShutdown = true };
        await rig.L.StartAsync();
        var brain = rig.Brain;
        await rig.L.ShutdownAsync();
        Assert.True(brain.Killed);
        Assert.Contains(rig.Log, l => l.Contains("killed it"));
        Assert.Contains(TimeSpan.FromSeconds(5), rig.Clock.Waits);   // it was given time first
    }

    [Fact]
    public async Task RestartBrain_StopsOursCleanly_ThenStartsANewOne()
    {
        var rig = new Rig(_root);
        await rig.L.StartAsync();
        var first = rig.Brain;
        await rig.L.RestartBrainAsync();
        Assert.Equal(["shutdown"], first.Input);
        Assert.False(first.Killed);
        Assert.Equal(2, rig.Host.Brains.Count());
        Assert.Equal(BrainStatus.Running, rig.L.State.Status);
        await Task.Delay(50);
        Assert.Equal(2, rig.Host.Brains.Count());                   // its exit wasn't taken for a crash
    }

    [Fact]
    public async Task AnAttachedBrainThatWentAway_RestartBrainStartsOurOwn()
    {
        var rig = new Rig(_root);
        rig.Net.Listening[Port] = true;
        await rig.L.StartAsync();
        rig.Net.Listening[Port] = false;                            // you stopped yours
        await rig.L.RestartBrainAsync();
        Assert.Single(rig.Host.Brains);
        Assert.True(rig.L.OwnsBrain);
    }

    // ── PhoneBox ────────────────────────────────────────────────────────────

    [Fact]
    public async Task PhoneBoxEnabled_NotRunning_IsStartedOnItsPort_AndStoppedOnQuit()
    {
        var exe = Path.Combine(_root, "PhoneBoxAgent.exe");
        var rig = new Rig(_root, new LauncherSettings { JarvisRoot = _root, PhoneBoxPath = exe });
        rig.Host.Files.Add(exe);
        rig.Host.PreflightOutput = FakeHost.Report(phonebox: true);
        await rig.L.StartAsync();
        var phonebox = Assert.Single(rig.Host.Named("PhoneBoxAgent.exe"));
        Assert.Equal(["--port", "8780"], phonebox.Spec.Arguments);
        Assert.Equal("phonebox", phonebox.Spec.LogName);
        Assert.True(phonebox.Spec.EndsWithLauncher);
        await rig.L.ShutdownAsync();
        Assert.True(phonebox.Killed);
    }

    [Fact]
    public async Task PhoneBoxAlreadyRunning_OrDisabled_IsLeftAlone()
    {
        var exe = Path.Combine(_root, "PhoneBoxAgent.exe");
        var running = new Rig(_root, new LauncherSettings { JarvisRoot = _root, PhoneBoxPath = exe });
        running.Host.Files.Add(exe);
        running.Host.PreflightOutput = FakeHost.Report(phonebox: true);
        running.Net.Listening[8780] = true;
        await running.L.StartAsync();
        Assert.Empty(running.Host.Named("PhoneBoxAgent.exe"));

        var disabled = new Rig(_root, new LauncherSettings { JarvisRoot = _root, PhoneBoxPath = exe });
        disabled.Host.Files.Add(exe);
        await disabled.L.StartAsync();
        Assert.Empty(disabled.Host.Named("PhoneBoxAgent.exe"));
    }

    [Fact]
    public async Task PhoneBoxEnabled_ButNotInstalled_OnlyWarns()
    {
        var rig = new Rig(_root, new LauncherSettings { JarvisRoot = _root, PhoneBoxPath = @"C:\none\PhoneBoxAgent.exe" });
        rig.Host.PreflightOutput = FakeHost.Report(phonebox: true);
        await rig.L.StartAsync();
        Assert.True(rig.Said("PhoneBox not started"));
        Assert.Equal(BrainStatus.Running, rig.L.State.Status);
    }

    [Fact]
    public async Task ACrashedPhoneBox_IsRestarted()
    {
        var exe = Path.Combine(_root, "PhoneBoxAgent.exe");
        var rig = new Rig(_root, new LauncherSettings { JarvisRoot = _root, PhoneBoxPath = exe });
        rig.Host.Files.Add(exe);
        rig.Host.PreflightOutput = FakeHost.Report(phonebox: true);
        await rig.L.StartAsync();
        rig.Host.Named("PhoneBoxAgent.exe").Single().Exit(3);
        await Until(() => rig.Host.Named("PhoneBoxAgent.exe").Count() == 2, "PhoneBox restarted");
    }
}
