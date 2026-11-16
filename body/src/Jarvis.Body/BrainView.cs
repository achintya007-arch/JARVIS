using Jarvis.Launcher;

namespace Jarvis.Body;

/// <summary>How the launcher's Brain state reads in the tray menu and on the HUD.</summary>
public static class BrainView
{
    /// <summary>The tray menu's status line.</summary>
    public static string TrayLine(LauncherState state) => state.Status switch
    {
        BrainStatus.Idle => "Brain: not started",
        BrainStatus.Checking => "Brain: checking…",
        BrainStatus.StartingOllama => "Brain: starting Ollama…",
        BrainStatus.Starting => "Brain: starting…",
        BrainStatus.Running => "Brain: running (started by JARVIS)",
        BrainStatus.Attached => "Brain: running (started by hand)",
        BrainStatus.Restarting => "Brain: crashed, restarting…",
        BrainStatus.Failed => "Brain: stopped (see notification)",
        BrainStatus.Stopping => "Brain: stopping…",
        _ => "Brain: stopped",
    };

    /// <summary>What the HUD's status line says while it isn't connected;
    /// empty when the connection detail says enough.</summary>
    public static string HudDetail(LauncherState state) => state.Status switch
    {
        BrainStatus.Checking or BrainStatus.StartingOllama or BrainStatus.Starting
            or BrainStatus.Restarting or BrainStatus.Stopping => state.Detail,
        BrainStatus.Failed => "JARVIS didn't start: " + state.Detail,
        _ => "",
    };

    public static string Level(NoticeLevel level) => level switch
    {
        NoticeLevel.Alert => "alert",
        NoticeLevel.Warn => "warn",
        _ => "info",
    };
}
