using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Media;
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
    private ConnectionState _connection = ConnectionState.Disconnected;
    private string _statusDetail = "starting";

    public event PropertyChangedEventHandler? PropertyChanged;

    public Brush StatusBrush => _connection switch
    {
        ConnectionState.Connected => Green,
        ConnectionState.Connecting => Amber,
        ConnectionState.AuthFailed => Red,
        _ => Gray,
    };

    public string StatusText => _connection switch
    {
        ConnectionState.Connected => $"Connected · {_state.Model}",
        ConnectionState.Connecting => "Connecting…",
        ConnectionState.AuthFailed => "Token rejected · " + _statusDetail,
        _ => "Offline · " + _statusDetail,
    };

    public bool IsConnected => _connection == ConnectionState.Connected;

    public string VoiceState => !IsConnected ? "" : _state.Muted ? $"{_state.VoiceState} · muted" : _state.VoiceState;

    public Brush VoiceBrush => _state.VoiceState switch
    {
        "listening" => Green,
        "processing" => Amber,
        "speaking" => Brushes.DeepSkyBlue,
        "interrupted" => Red,
        _ => Gray,
    };

    public string Transcript => _state.Transcript.Length == 0 ? "" : $"“{_state.Transcript}”";

    public string Reply => _state.ReplyInterrupted ? _state.Reply + " …(interrupted)" : _state.Reply;

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

    private void RaiseAll() => Raise(string.Empty);     // "" = every property changed

    private void Raise([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));

    private static Brush Freeze(SolidColorBrush b)
    {
        b.Freeze();
        return b;
    }
}
