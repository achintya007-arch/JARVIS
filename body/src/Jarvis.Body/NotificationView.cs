using Forms = System.Windows.Forms;

namespace Jarvis.Body;

/// <summary>How each notify level is shown (BBP amendment 18).</summary>
public static class NotificationView
{
    /// <summary>How long the HUD card stays: alerts longest.</summary>
    public static TimeSpan ShowFor(string level) => level switch
    {
        "alert" => TimeSpan.FromSeconds(30),
        "warn" => TimeSpan.FromSeconds(15),
        _ => TimeSpan.FromSeconds(10),
    };

    public static Forms.ToolTipIcon TrayIcon(string level) => level switch
    {
        "alert" => Forms.ToolTipIcon.Error,
        "warn" => Forms.ToolTipIcon.Warning,
        _ => Forms.ToolTipIcon.Info,
    };

    /// <summary>Windows needs non-empty text for a notification balloon.</summary>
    public static string TrayText(string title, string body) =>
        string.IsNullOrWhiteSpace(body) ? title : body;
}
