using Bbp.Client;
using Drawing = System.Drawing;
using Forms = System.Windows.Forms;

namespace Jarvis.Body;

/// <summary>What the tray icon shows at a glance.</summary>
public enum TrayStatus
{
    Connected,
    Muted,
    Offline,
    AuthFailed,
}

public static class TrayVisual
{
    public static TrayStatus For(ConnectionState state, bool muted) => state switch
    {
        ConnectionState.Connected => muted ? TrayStatus.Muted : TrayStatus.Connected,
        ConnectionState.AuthFailed => TrayStatus.AuthFailed,
        _ => TrayStatus.Offline,
    };

    public static string Tooltip(TrayStatus status) => status switch
    {
        TrayStatus.Connected => "JARVIS: connected",
        TrayStatus.Muted => "JARVIS: connected, microphone muted",
        TrayStatus.AuthFailed => "JARVIS: auth failed, check token",
        _ => "JARVIS: offline (reconnecting)",
    };
}

/// <summary>
/// Notification-area icon: a coloured dot for the connection (green connected,
/// amber with a slash when muted, grey offline, red auth failed) and a menu
/// for mute/unmute, show/hide HUD, reconnect and quit.
/// </summary>
public sealed class TrayIcon : IDisposable
{
    private readonly Forms.NotifyIcon _icon;
    private readonly Forms.ToolStripMenuItem _header;
    private readonly Forms.ToolStripMenuItem _mute;
    private readonly Forms.ToolStripMenuItem _showHide;
    private readonly Forms.ToolStripMenuItem _reconnect;
    private readonly Dictionary<TrayStatus, Drawing.Icon> _icons;
    private bool _muted;

    public TrayIcon()
    {
        _icons = Enum.GetValues<TrayStatus>().ToDictionary(s => s, Draw);
        _header = new Forms.ToolStripMenuItem("JARVIS") { Enabled = false };
        _mute = new Forms.ToolStripMenuItem("Mute microphone", null, (_, _) => MuteToggled?.Invoke(!_muted));
        _showHide = new Forms.ToolStripMenuItem("Hide HUD", null, (_, _) => ShowHideRequested?.Invoke());
        _reconnect = new Forms.ToolStripMenuItem("Reconnect", null, (_, _) => ReconnectRequested?.Invoke());
        var quit = new Forms.ToolStripMenuItem("Quit", null, (_, _) => QuitRequested?.Invoke());

        var menu = new Forms.ContextMenuStrip();
        menu.Items.AddRange([_header, new Forms.ToolStripSeparator(), _mute, _showHide, _reconnect,
                             new Forms.ToolStripSeparator(), quit]);
        _icon = new Forms.NotifyIcon { ContextMenuStrip = menu, Visible = true };
        _icon.DoubleClick += (_, _) => ShowHideRequested?.Invoke();
        Update(TrayStatus.Offline, hudVisible: true);
    }

    /// <summary>true = mute, false = unmute.</summary>
    public event Action<bool>? MuteToggled;
    public event Action? ShowHideRequested;
    public event Action? ReconnectRequested;
    public event Action? QuitRequested;

    public void Update(TrayStatus status, bool hudVisible)
    {
        _muted = status == TrayStatus.Muted;
        _icon.Icon = _icons[status];
        _icon.Text = TrayVisual.Tooltip(status);
        _header.Text = TrayVisual.Tooltip(status);
        _mute.Text = _muted ? "Unmute microphone" : "Mute microphone";
        _mute.Enabled = status is TrayStatus.Connected or TrayStatus.Muted;
        _reconnect.Enabled = status is TrayStatus.Offline or TrayStatus.AuthFailed;
        _showHide.Text = hudVisible ? "Hide HUD" : "Show HUD";
    }

    public void Dispose()
    {
        _icon.Visible = false;
        _icon.Dispose();
        foreach (var icon in _icons.Values)
            icon.Dispose();
    }

    private static Drawing.Icon Draw(TrayStatus status)
    {
        var color = status switch
        {
            TrayStatus.Connected => Drawing.Color.FromArgb(0x3D, 0xDC, 0x84),
            TrayStatus.Muted => Drawing.Color.FromArgb(0xF5, 0xB7, 0x31),
            TrayStatus.AuthFailed => Drawing.Color.FromArgb(0xF2, 0x5C, 0x54),
            _ => Drawing.Color.FromArgb(0x8A, 0x93, 0x9B),
        };
        using var bmp = new Drawing.Bitmap(32, 32);
        using (var g = Drawing.Graphics.FromImage(bmp))
        {
            g.SmoothingMode = Drawing.Drawing2D.SmoothingMode.AntiAlias;
            using var fill = new Drawing.SolidBrush(color);
            g.FillEllipse(fill, 3, 3, 26, 26);
            using var pen = new Drawing.Pen(Drawing.Color.White, 4);
            if (status == TrayStatus.Muted)
                g.DrawLine(pen, 9, 23, 23, 9);                     // slash: mic off
            else if (status == TrayStatus.AuthFailed)
            {
                g.DrawLine(pen, 16, 8, 16, 18);                    // "!"
                g.DrawLine(pen, 16, 21, 16, 24);
            }
        }
        // Icon.FromHandle doesn't own the HICON; clone so Dispose frees it.
        var handle = bmp.GetHicon();
        using var temp = Drawing.Icon.FromHandle(handle);
        var icon = (Drawing.Icon)temp.Clone();
        DestroyIcon(handle);
        return icon;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr handle);
}
