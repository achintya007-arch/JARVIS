using System.IO;
using System.Windows;
using Bbp.Client;

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
            ClientVersion = "0.2.0",
            Capabilities = ["hud", "hotkey"],
        }, validator);

        // Client events arrive on worker threads; the view model lives on the UI thread.
        client.StateChanged += (state, detail) =>
        {
            BodyLog.Write($"connection: {state} {detail}");
            Dispatcher.BeginInvoke(() => vm.OnConnection(state, detail));
        };
        client.Welcomed += w => Dispatcher.BeginInvoke(() => vm.OnWelcome(w));
        client.EventReceived += m => Dispatcher.BeginInvoke(() => vm.OnEvent(m));
        client.ResponseReceived += OnResponse;
        client.ProtocolError += err => BodyLog.Write("protocol: " + err);

        var window = _window = new MainWindow { DataContext = vm };
        window.TextSubmitted += text =>
        {
            vm.OnTyped(text);
            Send(Topics.InputText, new InputTextPayload(text));
        };

        var tray = _tray = new TrayIcon();
        tray.MuteToggled += mute => Send(Topics.VoiceControl, new VoiceControlPayload(mute ? "mute" : "unmute"));
        tray.ShowHideRequested += ToggleHud;
        tray.ReconnectRequested += client.Reconnect;
        tray.QuitRequested += Shutdown;
        vm.PropertyChanged += (_, _) => tray.Update(vm.TrayStatus, window.IsVisible);
        window.IsVisibleChanged += (_, _) => tray.Update(vm.TrayStatus, window.IsVisible);

        window.SourceInitialized += (_, _) => RegisterHotkey(settings.Hotkey);
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
        Dispatcher.BeginInvoke(() => _vm!.ShowNotice(Capitalize(error.Message) + "."));
    }

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
        _hotkey?.Dispose();
        _tray?.Dispose();
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
