using System.Text.Json.Nodes;

namespace Bbp.Client.Tests;

/// <summary>Milestone 5 (amendment 23): the HUD's activity lines and the agent status.</summary>
public class ToolActivityTests
{
    private static readonly DateTime T0 = new(2026, 11, 16, 9, 0, 0, DateTimeKind.Utc);

    private static (BodyState, Action<double>) Make()
    {
        var now = T0;
        return (new BodyState(() => now), seconds => now = T0.AddSeconds(seconds));
    }

    private static Envelope Started(string id, string summary) =>
        Brain.Event("tool.started", new ToolStartedPayload(id, "open_app", summary));

    private static Envelope Finished(string id, string summary, string? status, bool? ok = null) =>
        Brain.Event("tool.finished", new ToolFinishedPayload(id, ok ?? status == "ok", summary, status));

    [Fact]
    public void StartedThenFinished_HoldsTheTickTwoSeconds()
    {
        var (s, at) = Make();
        Assert.True(s.Apply(Started("c1", "Opening Calculator")));
        Assert.Equal([new ActivityLine("c1", "Opening Calculator", ToolOutcome.Running)], s.Activity.Lines);

        at(1.0);
        Assert.True(s.Apply(Finished("c1", "Opening Calculator", "ok")));
        Assert.Equal(ToolOutcome.Ok, s.Activity.Lines.Single().Outcome);

        at(2.9);
        Assert.False(s.Activity.Prune());
        at(3.0);
        Assert.True(s.Activity.Prune());
        Assert.True(s.Activity.IsEmpty);
    }

    [Theory]
    [InlineData("ok", true, ToolOutcome.Ok)]
    [InlineData("denied", false, ToolOutcome.Denied)]
    [InlineData("error", false, ToolOutcome.Error)]
    [InlineData("cancelled", false, ToolOutcome.Cancelled)]
    [InlineData(null, true, ToolOutcome.Ok)]           // an older Brain without status
    [InlineData(null, false, ToolOutcome.Error)]
    [InlineData("new-thing", false, ToolOutcome.Error)]
    public void Outcomes(string? status, bool ok, ToolOutcome expected) =>
        Assert.Equal(expected, ToolActivity.OutcomeOf(new ToolFinishedPayload("c", ok, "x", status)));

    [Fact]
    public void ARunningLineThatNeverFinishes_ClearsAfterThirtySeconds()
    {
        var (s, at) = Make();
        s.Apply(Started("c1", "Searching the web"));
        at(29.9);
        Assert.False(s.Activity.Prune());
        at(30);
        Assert.True(s.Activity.Prune());
        Assert.Empty(s.Activity.Lines);
    }

    [Fact]
    public void EachCallHasItsOwnLine()
    {
        var (s, _) = Make();
        s.Apply(Started("c1", "Opening Calculator"));
        s.Apply(Started("c2", "Pressing 'Seven' in Calculator"));
        s.Apply(Finished("c1", "Opening Calculator", "ok"));
        Assert.Equal(
            [new ActivityLine("c1", "Opening Calculator", ToolOutcome.Ok),
             new ActivityLine("c2", "Pressing 'Seven' in Calculator", ToolOutcome.Running)],
            s.Activity.Lines);
    }

    [Fact]
    public void Version_ChangesOnlyWithTheLines()
    {
        var (s, at) = Make();
        var v0 = s.Activity.Version;
        s.Apply(Brain.Event("voice.state", new VoiceStatePayload("processing", false)));
        Assert.Equal(v0, s.Activity.Version);
        s.Apply(Started("c1", "Opening Calculator"));
        var v1 = s.Activity.Version;
        Assert.NotEqual(v0, v1);
        Assert.False(s.Activity.Prune());
        Assert.Equal(v1, s.Activity.Version);
        s.Apply(Finished("c1", "Opening Calculator", "ok"));
        at(2);
        Assert.True(s.Activity.Prune());
        Assert.True(s.Activity.Version > v1);
    }

    [Fact]
    public void AFinishWithoutAStart_IsStillShown()
    {
        var (s, _) = Make();
        s.Apply(Finished("c9", "Writing notes.txt", "denied", ok: false));
        Assert.Equal(new ActivityLine("c9", "Writing notes.txt", ToolOutcome.Denied), s.Activity.Lines.Single());
    }

    [Fact]
    public void TheListIsCapped_DroppingFinishedLinesFirst()
    {
        var (s, _) = Make();
        s.Apply(Started("done", "Done already"));
        s.Apply(Finished("done", "Done already", "ok"));
        for (var i = 0; i < ToolActivity.MaxLines; i++)
            s.Apply(Started($"r{i}", $"Running {i}"));
        Assert.Equal(ToolActivity.MaxLines, s.Activity.Lines.Count);
        Assert.All(s.Activity.Lines, l => Assert.Equal(ToolOutcome.Running, l.Outcome));
    }

    [Fact]
    public void Welcome_RestoresRunningTools_AndKeepsTheirAge()
    {
        var (s, at) = Make();
        s.Apply(Started("gone", "Something old"));
        s.ApplyWelcome(new WelcomePayload("s", "3.0", Brain.EmptySnapshot with
        {
            RunningTools = [new RunningTool("c1", "search", "Searching the web", 25.0)],
        }));
        Assert.Equal([new ActivityLine("c1", "Searching the web", ToolOutcome.Running)], s.Activity.Lines);
        at(5.0);                                         // 30 s after it started on the Brain
        Assert.True(s.Activity.Prune());
    }

    [Fact]
    public void Welcome_FromAnOlderBrain_HasNoRunningTools()
    {
        var (s, _) = Make();
        s.ApplyWelcome(new WelcomePayload("s", "3.0", Brain.EmptySnapshot));
        Assert.True(s.Activity.IsEmpty);
    }

    [Fact]
    public void AgentState_UpdatesModelAndFallback()
    {
        var (s, _) = Make();
        s.ApplyWelcome(new WelcomePayload("s", "3.0", Brain.EmptySnapshot));
        Assert.False(s.Agent!.Fallback);
        Assert.True(s.Apply(Brain.Event("agent.state",
            new AgentSnapshot("neutral", "chat", "qwen2.5:3b", new LoadSnapshot(91.0, 97.5), Fallback: true))));
        Assert.Equal("qwen2.5:3b", s.Model);
        Assert.True(s.Agent!.Fallback);
        Assert.Equal(91.0, s.Agent.Load.RamPct);
    }

    [Fact]
    public void AgentState_WithoutFallback_FromAnOlderBrain()
    {
        var (s, _) = Make();
        var n = Brain.Event("agent.state", new AgentSnapshot("neutral", "chat", "m", new LoadSnapshot(null, null)));
        var node = n.ToNode().AsObject();
        node["payload"]!.AsObject().Remove("fallback");
        Brain.Validator.Validate(node);
        Assert.True(s.Apply(Envelope.FromNode(node)));
        Assert.False(s.Agent!.Fallback);
    }

    [Fact]
    public void TheNewTopics_Validate()
    {
        var v = Brain.Validator;
        v.Validate(Started("c1", "Opening Calculator"));
        v.Validate(Finished("c1", "Opening Calculator", "denied", ok: false));
        v.Validate(Finished("c1", "Opening Calculator", null, ok: true));       // status left out
        v.Validate(Brain.Event("agent.state",
            new AgentSnapshot("neutral", "chat", "m", new LoadSnapshot(40, null), Fallback: true)));
        v.Validate(Brain.Welcome(Brain.EmptySnapshot with
        {
            RunningTools = [new RunningTool("c1", "search", "Searching the web", 3.2)],
        }));
    }

    [Fact]
    public void ABadToolPayload_IsRejected()
    {
        var node = Started("c1", new string('x', 81)).ToNode().AsObject();
        Assert.Equal("E_BAD_PAYLOAD", Assert.Throws<BbpException>(() => Brain.Validator.Validate(node)).Code);

        var bad = Finished("c1", "x", "ok").ToNode().AsObject();
        bad["payload"]!["status"] = JsonValue.Create("exploded");
        Assert.Equal("E_BAD_PAYLOAD", Assert.Throws<BbpException>(() => Brain.Validator.Validate(bad)).Code);
    }
}
