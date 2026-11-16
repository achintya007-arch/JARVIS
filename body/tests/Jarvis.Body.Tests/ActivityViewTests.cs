using Bbp.Client;

namespace Jarvis.Body.Tests;

/// <summary>How tool calls and the agent status read on the HUD (milestone 5).</summary>
public class ActivityViewTests
{
    [Theory]
    [InlineData(ToolOutcome.Running, "•", "Opening Calculator…", ActivityTone.Running)]
    [InlineData(ToolOutcome.Ok, "✓", "Opening Calculator", ActivityTone.Ok)]
    [InlineData(ToolOutcome.Denied, "–", "Opening Calculator · declined", ActivityTone.Muted)]
    [InlineData(ToolOutcome.Cancelled, "–", "Opening Calculator · stopped", ActivityTone.Muted)]
    [InlineData(ToolOutcome.Error, "✗", "Opening Calculator · failed", ActivityTone.Failed)]
    public void Lines(ToolOutcome outcome, string mark, string text, ActivityTone tone)
    {
        var view = ActivityView.For(new ActivityLine("c1", "Opening Calculator", outcome));
        Assert.Equal((mark, text, tone), (view.Mark, view.Text, view.Tone));
    }

    [Fact]
    public void EqualLines_AreStillDistinctItems()
    {
        // WPF keys automation peers by item equality; a rebuilt list must not
        // hand UI Automation the previous list's row.
        var line = new ActivityLine("c1", "Searching the web", ToolOutcome.Running);
        Assert.NotEqual(ActivityView.For(line), ActivityView.For(line));
    }

    [Fact]
    public void Status_ModelAndLoad()
    {
        Assert.Equal("qwen2.5:7b · RAM 61% · VRAM 40%",
                     ActivityView.Status(new AgentSnapshot("n", "i", "qwen2.5:7b", new LoadSnapshot(61.2, 40.4))));
        Assert.Equal("qwen2.5:7b · RAM 61%",
                     ActivityView.Status(new AgentSnapshot("n", "i", "qwen2.5:7b", new LoadSnapshot(61.2, null))));
        Assert.Equal("", ActivityView.Status(new AgentSnapshot("unknown", "unknown", "unknown", new LoadSnapshot(null, null))));
        Assert.Equal("", ActivityView.Status(null));
    }
}
