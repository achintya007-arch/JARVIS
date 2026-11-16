using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Threading;
using Bbp.Client;
using Jarvis.Desktop;
using Jarvis.Launcher;
using BrainLauncher = Jarvis.Launcher.Launcher;

namespace Jarvis.Body;

public partial class App : Application
{
    private readonly CancellationTokenSource _cts = new();
    private Task? _clientTask;
    private BbpClient? _client;
    private HudViewModel? _vm;
    private MainWindow? _window;
    private TrayIcon? _tray;
    private GlobalHotkey? _hotkey;
    private FlaUiReader? _uiReader;
    private FlaUiActor? _uiActor;

    // Launcher: starts the Brain (and Ollama, PhoneBox) when none is running.
    private const string InstanceName = @"Local\JARVIS.Body";
    private const string ShowSignalName = @"Local\JARVIS.Body.Show";
    private const string QuitSignalName = @"Local\JARVIS.Body.Quit";
    private Mutex? _instance;
    private EventWaitHandle? _showSignal;
    private EventWaitHandle? _quitSignal;
    private BrainLauncher? _launcher;
    private readonly StartupEntry _startup = new();
    private bool _quitting;
    private bool _launcherStopped;

    // Confirmations (milestone 3): one dialog per pending confirm.request.
    private readonly ConfirmationTracker _confirmations = new();
    private readonly Dictionary<string, ConfirmWindow> _dialogs = new();
    private readonly ConcurrentDictionary<string, string> _answerCommands = new();   // command id → confirm_id
    private DispatcherTimer? _dialogTimer;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ShutdownMode = ShutdownMode.OnExplicitShutdown;      // hiding the HUD must not quit
        var quitOther = e.Args.Any(a => a.Equals("--quit", StringComparison.OrdinalIgnoreCase));
        if (!ClaimSingleInstance(quitOther) || quitOther)
        {
            Shutdown(0);                                     // the running Body shows its HUD (or quits) instead
            return;
        }
        var settings = BodySettings.Load();
        var jarvisRoot = ResolveJarvisRoot(settings);
        settings = settings with { JarvisRoot = jarvisRoot ?? settings.JarvisRoot };
        BodyLog.Write($"Body starting (url={settings.Url}, hotkey={settings.Hotkey}, jarvis_root={jarvisRoot ?? "unset"}, " +
                      $"autostart_brain={settings.AutostartBrain}, mode={settings.BrainMode})");

        SchemaValidator validator;
        try
        {
            validator = new SchemaValidator();
        }
        catch (FileNotFoundException ex)
        {
            BodyLog.Write(ex.Message);
            MessageBox.Show(ex.Message, "JARVIS Body", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
            return;
        }

        var vm = _vm = new HudViewModel();
        var client = _client = new BbpClient(new BbpClientOptions
        {
            Url = new Uri(settings.Url),
            TokenProvider = settings.ReadToken,
            ClientName = "win-shell",
            ClientVersion = "0.6.0",
            Capabilities = ["hud", "hotkey", .. DesktopService.Capabilities],
        }, validator);

        // Desktop control (BBP desktop.*), gated by the Body's own allowlist.
        var (allowlist, allowlistProblem) = DesktopAllowlist.Load(
            Path.Combine(AppContext.BaseDirectory, DesktopAllowlist.FileName));
        BodyLog.Write(allowlistProblem ?? $"desktop allowlist: {allowlist.Apps.Count} apps");
        _uiReader = new FlaUiReader();
        _uiActor = new FlaUiActor();
        var (risky, riskyProblem) = RiskyNames.Load(Path.Combine(AppContext.BaseDirectory, RiskyNames.FileName));
        if (riskyProblem is not null)
            BodyLog.Write(riskyProblem);
        new DesktopService(allowlist, new Win32WindowSystem(), new ProcessLauncher(), new CoreAudioVolume(),
                           _uiReader, BodyLog.Write, _uiActor, new ApprovalLedger(), risky).Register(client);
        client.RequestHandled += (topic, outcome, ms) => BodyLog.Write($"request {topic}: {outcome} in {ms:0} ms");

        // Client events arrive on worker threads; the view model lives on the UI thread.
        client.StateChanged += (state, detail) =>
        {
            BodyLog.Write($"connection: {state} {detail}");
            Dispatcher.BeginInvoke(() =>
            {
                vm.OnConnection(state, detail);
                if (state != ConnectionState.Connected)
                    _confirmations.Disconnected();      // can't answer from here any more
            });
        };
        client.Welcomed += w => Dispatcher.BeginInvoke(() =>
        {
            vm.OnWelcome(w);
            _confirmations.ApplyWelcome(w);             // reopen what is still pending
        });
        client.EventReceived += m => Dispatcher.BeginInvoke(() =>
        {
            if (!_confirmations.Apply(m))
                vm.OnEvent(m);
        });
        client.ResponseReceived += OnResponse;
        _confirmations.Opened += OpenDialog;
        _confirmations.Closed += CloseDialog;
        _dialogTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(250) };
        _dialogTimer.Tick += (_, _) =>
        {
            _confirmations.ExpireOverdue();
            foreach (var dialog in _dialogs.Values)
                dialog.Model.Tick();
        };
        _dialogTimer.Start();
        client.ProtocolError += err => BodyLog.Write("protocol: " + err);

        var window = _window = new MainWindow { DataContext = vm };
        window.TextSubmitted += text =>
        {
            vm.OnTyped(text);
            Send(Topics.InputText, new InputTextPayload(text));
        };

        var tray = _tray = new TrayIcon();
        vm.NotificationArrived += note =>
        {
            BodyLog.Write($"notify {note.Level}: {note.Title}");
            tray.ShowNotification(note.Title, note.Body, note.Level);
        };
        tray.MuteToggled += mute => Send(Topics.VoiceControl, new VoiceControlPayload(mute ? "mute" : "unmute"));
        tray.ShowHideRequested += ToggleHud;
        tray.ReconnectRequested += client.Reconnect;
        tray.QuitRequested += Quit;
        window.QuitRequested += Quit;
        SetUpLauncher(settings, jarvisRoot, client, vm, tray);
        vm.PropertyChanged += (_, _) => tray.Update(vm.TrayStatus, window.IsVisible);
        window.IsVisibleChanged += (_, _) => tray.Update(vm.TrayStatus, window.IsVisible);

        window.SourceInitialized += (_, _) => RegisterHotkey(settings.Hotkey);
        if (allowlistProblem is not null)
            vm.ShowNotice($"Desktop control is limited: {allowlistProblem}.", 20);
        window.Show();
        _clientTask = Task.Run(() => client.RunAsync(_cts.Token));
        if (_launcher is { } launcher && settings.AutostartBrain)
            RunLauncher("start", () => launcher.StartAsync());
    }

    // ── Launcher ────────────────────────────────────────────────────────────

    /// <summary>One Body per user session. A second launch (the shortcut
    /// clicked again) tells the first to show its HUD, then exits;
    /// <c>Jarvis.Body.exe --quit</c> tells it to quit the way the tray's Quit
    /// does (install.ps1 and uninstall.ps1 use this). With no Body running,
    /// --quit does nothing.</summary>
    private bool ClaimSingleInstance(bool quitOther)
    {
        _instance = new Mutex(initiallyOwned: !quitOther, InstanceName, out var first);
        if (!first || quitOther)
        {
            if (!first)
            {
                try
                {
                    using var signal = EventWaitHandle.OpenExisting(quitOther ? QuitSignalName : ShowSignalName);
                    signal.Set();
                }
                catch (WaitHandleCannotBeOpenedException)
                {
                    // the other one is starting or stopping
                }
            }
            _instance.Dispose();
            _instance = null;
            return false;
        }
        _showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, ShowSignalName);
        _quitSignal = new EventWaitHandle(false, EventResetMode.AutoReset, QuitSignalName);
        WaitHandle[] signals = [_showSignal, _quitSignal];
        new Thread(() =>
        {
            while (!_cts.IsCancellationRequested)
            {
                var which = WaitHandle.WaitAny(signals);
                if (_cts.IsCancellationRequested)
                    break;
                Dispatcher.BeginInvoke(which == 0 ? ShowHud : Quit);
            }
        }) { IsBackground = true, Name = "instance-signals" }.Start();
        return true;
    }

    /// <summary>jarvis_root from the settings, else found from the exe's folder
    /// (running from the repo), else asked once with a folder picker. Saved.</summary>
    private static string? ResolveJarvisRoot(BodySettings settings)
    {
        if (LauncherSettings.LooksLikeJarvisRoot(settings.JarvisRoot))
            return Path.GetFullPath(settings.JarvisRoot!);
        var root = LauncherSettings.FindJarvisRoot(AppContext.BaseDirectory);
        if (root is null && settings.AutostartBrain)
            root = AskForJarvisRoot(settings.JarvisRoot);
        if (root is not null)
        {
            try
            {
                BodySettings.SaveValue("jarvis_root", root);
                BodyLog.Write($"jarvis_root saved: {root}");
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                BodyLog.Write($"couldn't save jarvis_root to {BodySettings.DefaultPath}: {e.Message}");
            }
        }
        return root;
    }

    private static string? AskForJarvisRoot(string? configured)
    {
        var intro = configured is null
            ? "JARVIS needs to know where its Brain is. Pick the jarvis_v1 folder (or the JARVIS folder that contains it)."
            : $"jarvis_root in body.settings.json is {configured}, which isn't the jarvis_v1 folder. Pick it again.";
        if (MessageBox.Show(intro, "JARVIS", MessageBoxButton.OKCancel, MessageBoxImage.Information) != MessageBoxResult.OK)
            return null;
        while (true)
        {
            var dialog = new Microsoft.Win32.OpenFolderDialog { Title = "Pick the jarvis_v1 folder" };
            if (dialog.ShowDialog() != true)
                return null;
            foreach (var candidate in new[] { dialog.FolderName, Path.Combine(dialog.FolderName, "jarvis_v1") })
                if (LauncherSettings.LooksLikeJarvisRoot(candidate))
                    return Path.GetFullPath(candidate);
            if (MessageBox.Show($"{dialog.FolderName} isn't jarvis_v1 (there's no core_v3\\__main__.py in it). Try another folder?",
                                "JARVIS", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes)
                return null;
        }
    }

    private void SetUpLauncher(BodySettings settings, string? jarvisRoot, BbpClient client, HudViewModel vm, TrayIcon tray)
    {
        var exe = Environment.ProcessPath;
        tray.SetStartWithWindows(exe is not null && _startup.IsEnabledFor(exe));
        tray.StartWithWindowsToggled += on => SetStartWithWindows(on, exe);
        tray.OpenLogsRequested += OpenLogsFolder;
        if (jarvisRoot is null)
        {
            tray.SetBrain("Brain: jarvis_root not set", canRestart: false);
            if (settings.AutostartBrain)
                vm.ShowNotice("JARVIS can't start its Brain: set \"jarvis_root\" in body.settings.json (the jarvis_v1 folder).", 30);
            return;
        }
        var launcher = _launcher = new BrainLauncher(settings.ToLauncher(jarvisRoot), new ProcessHost(LogsFolder),
                                                     new Network(), log: BodyLog.Write);
        launcher.StateChanged += state =>
        {
            BodyLog.Write($"brain: {state.Status} {state.Detail}");
            Dispatcher.BeginInvoke(() =>
            {
                vm.OnBrain(state);
                if (!_quitting)
                    tray.SetBrain(BrainView.TrayLine(state), state.Status is not BrainStatus.Stopping);
            });
        };
        launcher.Notice += notice =>
        {
            var level = BrainView.Level(notice.Level);
            BodyLog.Write($"launcher notice {level}: {notice.Title}: {notice.Body}");
            Dispatcher.BeginInvoke(() =>
            {
                tray.ShowNotification(notice.Title, notice.Body, level);
                vm.ShowNotice($"{notice.Title}: {notice.Body}", NotificationView.ShowFor(level).TotalSeconds);
            });
        };
        launcher.BrainListening += client.Reconnect;
        client.Welcomed += _ => launcher.LinkUp();
        client.SessionEnded += launcher.LinkClosed;
        tray.RestartBrainRequested += () => RunLauncher("restart", launcher.RestartBrainAsync);
        tray.SetBrain(BrainView.TrayLine(launcher.State), canRestart: true);
    }

    /// <summary>Launcher work runs off the UI thread; failures are logged, never thrown.</summary>
    private static void RunLauncher(string what, Func<Task> work) => _ = Task.Run(async () =>
    {
        try
        {
            await work();
        }
        catch (Exception e)
        {
            BodyLog.Write($"launcher {what} failed: {e}");
        }
    });

    private static string LogsFolder => Path.GetDirectoryName(BodyLog.FilePath)!;

    private static void OpenLogsFolder()
    {
        Directory.CreateDirectory(LogsFolder);
        Process.Start(new ProcessStartInfo("explorer.exe", $"\"{LogsFolder}\"") { UseShellExecute = true });
    }

    private void SetStartWithWindows(bool on, string? exe)
    {
        try
        {
            if (exe is null)
                throw new InvalidOperationException("the Body's own path is unknown");
            if (on)
                _startup.Enable(exe);
            else
                _startup.Disable();
            BodyLog.Write($"start with Windows: {(on ? "on" : "off")} ({exe})");
        }
        catch (Exception e)
        {
            BodyLog.Write($"start with Windows: couldn't change it: {e.Message}");
            _vm!.ShowNotice($"Couldn't change Start with Windows: {e.Message}", 12);
        }
        _tray!.SetStartWithWindows(exe is not null && _startup.IsEnabledFor(exe));
    }

    /// <summary>Tray → Quit: stop the Brain we started (clean close 1000, kill
    /// only if it hangs), then PhoneBox and Ollama if we started them, then exit.</summary>
    private async void Quit()
    {
        if (_quitting)
            return;
        _quitting = true;
        BodyLog.Write("quit requested");
        _tray?.SetQuitting();
        if (_launcher is { } launcher)
        {
            if (launcher.OwnsBrain)
                _vm?.ShowNotice("Shutting JARVIS down…", 30);
            var stop = Task.Run(launcher.ShutdownAsync);
            if (await Task.WhenAny(stop, Task.Delay(TimeSpan.FromSeconds(30))) != stop)
                BodyLog.Write("launcher shutdown took over 30 s; exiting anyway");
            _launcherStopped = stop.IsCompletedSuccessfully;
        }
        Shutdown();
    }

    /// <summary>Windows is logging off or shutting down: stop what we started, briefly.</summary>
    protected override void OnSessionEnding(SessionEndingCancelEventArgs e)
    {
        BodyLog.Write($"session ending ({e.ReasonSessionEnding})");
        StopLauncherNow(TimeSpan.FromSeconds(8));
        base.OnSessionEnding(e);
    }

    private void StopLauncherNow(TimeSpan limit)
    {
        if (_launcher is null || _launcherStopped)
            return;
        try
        {
            // Off the UI thread: its awaits must not need the dispatcher we're blocking.
            _launcherStopped = Task.Run(_launcher.ShutdownAsync).Wait(limit);
        }
        catch (AggregateException e)
        {
            BodyLog.Write($"launcher shutdown failed: {e.InnerException?.Message}");
        }
    }

    private void ShowHud()
    {
        if (_window is null)
            return;
        _window.Show();
        if (_window.WindowState == WindowState.Minimized)
            _window.WindowState = WindowState.Normal;
        _window.Activate();
    }

    /// <summary>Push-to-talk. If the combination is taken (or invalid) the Body
    /// logs it, says so once on the HUD, and runs without a hotkey.</summary>
    private void RegisterHotkey(string text)
    {
        if (!HotkeyGesture.TryParse(text, out var gesture, out var parseError))
        {
            BodyLog.Write($"hotkey '{text}' is invalid: {parseError}");
            _vm!.ShowNotice($"Push-to-talk is off: hotkey \"{text}\" is invalid ({parseError}). Fix \"hotkey\" in body.settings.json.", 20);
            return;
        }
        var hotkey = new GlobalHotkey(gesture);
        if (!hotkey.TryRegister(_window!.Handle, out var error))
        {
            BodyLog.Write($"hotkey {gesture.Display} could not be registered: {error}");
            _vm!.ShowNotice($"Push-to-talk is off: {gesture.Display} is already used by another app. " +
                            "Pick another \"hotkey\" in body.settings.json.", 20);
            return;
        }
        hotkey.Pressed += () => Send(Topics.UiHotkey, new HotkeyPayload("push_to_talk"));
        _window.WindowMessage += hotkey.HandleMessage;
        _hotkey = hotkey;
        BodyLog.Write($"hotkey {gesture.Display} registered (push-to-talk)");
    }

    private void Send(string topic, object payload)
    {
        var client = _client!;
        _ = Task.Run(async () =>
        {
            try
            {
                await client.SendCommandAsync(topic, payload);
            }
            catch (InvalidOperationException)
            {
                await Dispatcher.BeginInvoke(() => _vm!.ShowNotice("Not connected to JARVIS."));
            }
            catch (Exception e)
            {
                BodyLog.Write($"send {topic} failed: {e.Message}");
                await Dispatcher.BeginInvoke(() => _vm!.ShowNotice($"Couldn't send: {e.Message}"));
            }
        });
    }

    private void OnResponse(Envelope response)
    {
        if (response.Topic != Topics.Error)
            return;
        var error = response.PayloadAs<ErrorPayload>().Error;
        BodyLog.Write($"command refused: {error.Code} {error.Message}");
        var answeredConfirm = response.Corr is { } corr && _answerCommands.TryRemove(corr, out var cid) ? cid : null;
        Dispatcher.BeginInvoke(() =>
        {
            if (answeredConfirm is not null)
                _confirmations.Dismiss(answeredConfirm);    // already answered elsewhere or timed out
            _vm!.ShowNotice(Capitalize(error.Message) + ".");
        });
    }

    // ── Confirmation dialogs ────────────────────────────────────────────────

    private void OpenDialog(ConfirmationDialog dialog)
    {
        BodyLog.Write($"confirmation {Short(dialog.ConfirmId)} shown ({dialog.Risk} risk, {dialog.TimeoutS:0.#} s)");
        var window = new ConfirmWindow(new ConfirmDialogModel(dialog, () => _confirmations.Now));
        window.Answered += payload => SendAnswer(window, payload);
        _dialogs[dialog.ConfirmId] = window;
        window.Show();
    }

    private void CloseDialog(string confirmId, DialogClose why, ConfirmResolvedPayload? resolved)
    {
        BodyLog.Write(resolved is null
            ? $"confirmation {Short(confirmId)} closed: {why}"
            : $"confirmation {Short(confirmId)} {(resolved.Approved ? "approved" : "denied")} by {resolved.By}");
        if (_dialogs.Remove(confirmId, out var window))
            window.Close();
        if (why == DialogClose.Disconnected)
            _vm!.ShowNotice("Lost the link to JARVIS while it was asking you to approve something. It won't go ahead.", 12);
    }

    private void SendAnswer(ConfirmWindow window, ConfirmResponsePayload payload)
    {
        var client = _client!;
        _ = Task.Run(async () =>
        {
            try
            {
                var id = await client.SendCommandAsync(Topics.ConfirmResponse, payload);
                _answerCommands[id] = payload.ConfirmId;
                BodyLog.Write($"confirmation {Short(payload.ConfirmId)}: sent {(payload.Approved ? "approve" : "deny")}");
            }
            catch (Exception e)
            {
                BodyLog.Write($"confirmation {Short(payload.ConfirmId)}: send failed: {e.Message}");
                var message = e is InvalidOperationException ? "Not connected to JARVIS." : $"Couldn't send: {e.Message}";
                await Dispatcher.BeginInvoke(() => window.Model.AnswerFailed(message));
            }
        });
    }

    private static string Short(string id) => id.Length > 8 ? id[..8] : id;

    private void ToggleHud()
    {
        if (_window!.IsVisible)
            _window.Hide();
        else
        {
            _window.Show();
            _window.Activate();
        }
    }

    private static string Capitalize(string s) => s.Length == 0 ? s : char.ToUpperInvariant(s[0]) + s[1..];

    protected override void OnExit(ExitEventArgs e)
    {
        StopLauncherNow(TimeSpan.FromSeconds(15));          // e.g. Shutdown() from somewhere other than Quit
        _cts.Cancel();
        _showSignal?.Set();                                 // let the signal thread see the cancel
        _dialogTimer?.Stop();
        _hotkey?.Dispose();
        _tray?.Dispose();
        _uiReader?.Dispose();
        _uiActor?.Dispose();
        try
        {
            _clientTask?.Wait(TimeSpan.FromSeconds(2));
        }
        catch (AggregateException)
        {
            // shutting down anyway
        }
        if (_instance is not null)                          // not the short-lived second launch / --quit
            BodyLog.Write("Body stopped");
        base.OnExit(e);
    }
}
