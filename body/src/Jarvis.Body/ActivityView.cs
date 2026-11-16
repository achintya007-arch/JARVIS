using Bbp.Client;

namespace Jarvis.Body;

/// <summary>One line of the HUD's activity list, ready to bind. A class, not a
/// record: WPF's automation peers are keyed by item equality, and a value-equal
/// line in a rebuilt list would point UI Automation (screen readers) at the
/// discarded row, so the line would vanish from the accessibility tree.</summary>
public sealed class ActivityLineView(string mark, string text, ActivityTone tone)
{
    public string Mark { get; } = mark;
    public string Text { get; } = text;
    public ActivityTone Tone { get; } = tone;
}

public enum ActivityTone { Running, Ok, Muted, Failed }

/// <summary>How tool calls and the agent status read on the HUD (amendment 23).
/// Strings only, so it is unit-tested without a window.</summary>
public static class ActivityView
{
    public static ActivityLineView For(ActivityLine line) => line.Outcome switch
    {
        ToolOutcome.Running => new("•", line.Summary + "…", ActivityTone.Running),
        ToolOutcome.Ok => new("✓", line.Summary, ActivityTone.Ok),
        ToolOutcome.Denied => new("–", line.Summary + " · declined", ActivityTone.Muted),
        ToolOutcome.Cancelled => new("–", line.Summary + " · stopped", ActivityTone.Muted),
        _ => new("✗", line.Summary + " · failed", ActivityTone.Failed),
    };

    /// <summary>"qwen2.5:7b · RAM 61% · VRAM 40%"; parts that are unknown are left out.</summary>
    public static string Status(AgentSnapshot? agent)
    {
        if (agent is null)
            return "";
        var parts = new List<string>();
        if (agent.Model.Length > 0 && agent.Model != "unknown")
            parts.Add(agent.Model);
        if (agent.Load?.RamPct is { } ram)
            parts.Add($"RAM {ram:F0}%");
        if (agent.Load?.VramPct is { } vram)
            parts.Add($"VRAM {vram:F0}%");
        return string.Join(" · ", parts);
    }
}
