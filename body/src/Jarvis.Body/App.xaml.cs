using System.Collections.Concurrent;
using System.IO;
using System.Windows;
using System.Windows.Threading;
using Bbp.Client;
using Jarvis.Desktop;

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

    // Confirmations (milestone 3): one dialog per pending confirm.request.
    private readonly ConfirmationTracker _confirmations = new();
    private readonly Dictionary<string, ConfirmWindow> _dialogs = new();
    private readonly ConcurrentDictionary<string, string> _answerCommands = new();   // command id → confirm_id
    private DispatcherTimer? _dialogTimer;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ShutdownMode = ShutdownMode.OnExplicitShutdown;      // hiding the HUD must not quit
        var settings = BodySettings.Load();
        BodyLog.Write($"Body starting (url={settings.Url}, hotkey={settings.Hotkey})");

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
            ClientVersion = "0.5.0",
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
        tray.QuitRequested += Shutdown;
        vm.PropertyChanged += (_, _) => tray.Update(vm.TrayStatus, window.IsVisible);
        window.IsVisibleChanged += (_, _) => tray.Update(vm.TrayStatus, window.IsVisible);

        window.SourceInitialized += (_, _) => RegisterHotkey(settings.Hotkey);
        if (allowlistProblem is not null)
            vm.ShowNotice($"Desktop control is limited: {allowlistProblem}.", 20);
        window.Show();
        _clientTask = Task.Run(() => client.RunAsync(_cts.Token));
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
        _cts.Cancel();
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
        BodyLog.Write("Body stopped");
        base.OnExit(e);
    }
}
