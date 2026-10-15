using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Media;
using System.Windows.Threading;
using Bbp.Client;

namespace Jarvis.Body;

/// <summary>Binds BodyState + connection status to the HUD. All methods run on
/// the UI thread (App marshals client events through the Dispatcher).</summary>
public sealed class HudViewModel : INotifyPropertyChanged
{
    private static readonly Brush Green = Freeze(new SolidColorBrush(Color.FromRgb(0x3D, 0xDC, 0x84)));
    private static readonly Brush Amber = Freeze(new SolidColorBrush(Color.FromRgb(0xF5, 0xB7, 0x31)));
    private static readonly Brush Red = Freeze(new SolidColorBrush(Color.FromRgb(0xF2, 0x5C, 0x54)));
    private static readonly Brush Gray = Freeze(new SolidColorBrush(Color.FromRgb(0x8A, 0x93, 0x9B)));

    private readonly BodyState _state = new();
    private readonly DispatcherTimer _noticeTimer = new();
    private ConnectionState _connection = ConnectionState.Disconnected;
    private string _statusDetail = "starting";
    private string _notice = "";

    public HudViewModel()
    {
        _noticeTimer.Tick += (_, _) => ClearNotice();
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    public Brush StatusBrush => _connection switch
    {
        ConnectionState.Connected => _state.Muted ? Amber : Green,
        ConnectionState.Connecting => Amber,
        ConnectionState.AuthFailed => Red,
        _ => Gray,
    };

    public string StatusText => _connection switch
    {
        ConnectionState.Connected => $"Connected · {_state.Model}",
        ConnectionState.Connecting => "Connecting…",
        ConnectionState.AuthFailed => "Auth failed, check token (right-click the tray icon to retry)",
        _ => "Offline · " + _statusDetail,
    };

    public bool IsConnected => _connection == ConnectionState.Connected;

    public bool Muted => IsConnected && _state.Muted;

    public TrayStatus TrayStatus => TrayVisual.For(_connection, _state.Muted);

    public string VoiceState => !IsConnected ? "" : _state.Muted ? $"{_state.VoiceState} · muted" : _state.VoiceState;

    public Brush VoiceBrush => _state.VoiceState switch
    {
        "listening" => Green,
        "processing" => Amber,
        "speaking" => Brushes.DeepSkyBlue,
        "interrupted" => Red,
        _ => Gray,
    };

    public string Transcript => _state.Transcript.Length == 0 ? ""
        : _state.TranscriptTyped ? $"› {_state.Transcript}" : $"“{_state.Transcript}”";

    public string Reply => _state.ReplyInterrupted ? _state.Reply + " …(interrupted)" : _state.Reply;

    /// <summary>A short message under the reply (refused command, hotkey problem…).</summary>
    public string Notice => _notice;

    public void OnConnection(ConnectionState state, string detail)
    {
        _connection = state;
        _statusDetail = detail;
        RaiseAll();
    }

    public void OnWelcome(WelcomePayload welcome)
    {
        _state.ApplyWelcome(welcome);
        RaiseAll();
    }

    public void OnEvent(Envelope message)
    {
        if (_state.Apply(message))
            RaiseAll();
    }

    public void OnTyped(string text)
    {
        _state.ApplyLocalInput(text);
        RaiseAll();
    }

    /// <summary>Show a notice; it clears itself after <paramref name="seconds"/>.</summary>
    public void ShowNotice(string text, double seconds = 8)
    {
        _notice = text;
        _noticeTimer.Stop();
        _noticeTimer.Interval = TimeSpan.FromSeconds(seconds);
        _noticeTimer.Start();
        Raise(nameof(Notice));
    }

    public void ClearNotice()
    {
        _noticeTimer.Stop();
        _notice = "";
        Raise(nameof(Notice));
    }

    private void RaiseAll() => Raise(string.Empty);     // "" = every property changed

    private void Raise([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

    private static Brush Freeze(SolidColorBrush b)
    {
        b.Freeze();
        return b;
    }
}
