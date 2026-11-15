using System.Text.Json.Nodes;

namespace Bbp.Client.Tests;

/// <summary>Milestone 3: which confirmations need a dialog, and the new messages.</summary>
public class ConfirmationTrackerTests
{
    private static readonly DateTimeOffset T0 = new(2026, 11, 16, 9, 0, 0, TimeSpan.Zero);

    private sealed class Clock
    {
        public DateTimeOffset Now { get; set; } = T0;
    }

    private static Envelope Request(string id, string risk = "high", double timeout = 15, string? detail = "git status")
    {
        var payload = new JsonObject
        {
            ["confirm_id"] = id, ["prompt"] = "About to run the command git status. Shall I proceed, Sir?",
            ["risk"] = risk, ["timeout_s"] = timeout,
        };
        if (detail is not null)
            payload["detail"] = detail;
        return Brain.Event("confirm.request", payload);
    }

    private static Envelope Resolved(string id, bool approved, string by) =>
        Brain.Event("confirm.resolved", new ConfirmResolvedPayload(id, approved, by));

    private static (ConfirmationTracker, Clock, List<ConfirmationDialog>, List<(string, DialogClose, ConfirmResolvedPayload?)>) Make()
    {
        var clock = new Clock();
        var t = new ConfirmationTracker(() => clock.Now);
        var opened = new List<ConfirmationDialog>();
        var closed = new List<(string, DialogClose, ConfirmResolvedPayload?)>();
        t.Opened += opened.Add;
        t.Closed += (id, why, r) => closed.Add((id, why, r));
        return (t, clock, opened, closed);
    }

    [Fact]
    public void Request_OpensADialog_WithDetailAndDeadline()
    {
        var (t, _, opened, _) = Make();
        Assert.True(t.Apply(Request("c1")));
        var d = Assert.Single(opened);
        Assert.Equal("git status", d.Detail);
        Assert.True(d.HighRisk);
        Assert.Equal(T0.AddSeconds(15), d.Deadline);
        Assert.Single(t.Open);
    }

    [Fact]
    public void Request_WithoutDetail_HasEmptyDetail()
    {
        var (t, _, opened, _) = Make();
        t.Apply(Request("c1", risk: "low", detail: null));
        Assert.Equal("", opened[0].Detail);
        Assert.False(opened[0].HighRisk);
    }

    [Fact]
    public void SameRequestTwice_OpensOneDialog()
    {
        var (t, _, opened, _) = Make();
        t.Apply(Request("c1"));
        t.Apply(Request("c1"));
        Assert.Single(opened);
    }

    [Fact]
    public void Resolved_ClosesIt_WithWhoAnswered()
    {
        var (t, _, _, closed) = Make();
        t.Apply(Request("c1"));
        Assert.True(t.Apply(Resolved("c1", false, "voice")));
        var (id, why, r) = Assert.Single(closed);
        Assert.Equal(("c1", DialogClose.Resolved, "voice", false), (id, why, r!.By, r.Approved));
        Assert.Empty(t.Open);
    }

    [Fact]
    public void ResolvedForAnUnknownId_ChangesNothing()
    {
        var (t, _, _, closed) = Make();
        t.Apply(Resolved("nope", true, "body"));
        Assert.Empty(closed);
    }

    [Fact]
    public void Welcome_ReopensPending_AndClosesWhatIsGone()
    {
        var (t, _, opened, closed) = Make();
        t.Apply(Request("old"));
        t.ApplyWelcome(new WelcomePayload("s", "3.0", Brain.EmptySnapshot with
        {
            PendingConfirmations = [new PendingConfirmation("c2", "Close Spotify?", "high", 9.5, "Spotify")],
        }));
        Assert.Contains(closed, c => c.Item1 == "old");
        var d = Assert.Single(t.Open);
        Assert.Equal(("c2", "Spotify", T0.AddSeconds(9.5)), (d.ConfirmId, d.Detail, d.Deadline));
        Assert.Equal(2, opened.Count);
    }

    [Fact]
    public void Disconnect_ClosesEverything()
    {
        var (t, _, _, closed) = Make();
        t.Apply(Request("c1"));
        t.Apply(Request("c2"));
        t.Disconnected();
        Assert.Equal(2, closed.Count);
        Assert.All(closed, c => Assert.Equal(DialogClose.Disconnected, c.Item2));
        Assert.Empty(t.Open);
    }

    [Fact]
    public void Overdue_ClosesAfterTheGrace_NotBefore()
    {
        var (t, clock, _, closed) = Make();
        t.Apply(Request("c1", timeout: 10));
        clock.Now = T0.AddSeconds(10.5);
        t.ExpireOverdue();
        Assert.Empty(closed);                                   // the Brain's timeout usually comes first
        clock.Now = T0.AddSeconds(10) + ConfirmationTracker.ExpiryGrace;
        t.ExpireOverdue();
        Assert.Equal(DialogClose.Expired, Assert.Single(closed).Item2);
    }

    [Fact]
    public void Dismiss_Closes()
    {
        var (t, _, _, closed) = Make();
        t.Apply(Request("c1"));
        t.Dismiss("c1");
        Assert.Single(closed);
    }

    [Fact]
    public void Countdown()
    {
        var d = new ConfirmationDialog("c", "p", "", "low", 10, T0.AddSeconds(10));
        Assert.Equal(1.0, d.FractionLeft(T0));
        Assert.Equal(0.25, d.FractionLeft(T0.AddSeconds(7.5)), 3);
        Assert.Equal(TimeSpan.Zero, d.Remaining(T0.AddSeconds(30)));
        Assert.Equal(0.0, d.FractionLeft(T0.AddSeconds(30)));
    }

    [Fact]
    public void OtherTopics_AreNotOurs()
    {
        var (t, _, _, _) = Make();
        Assert.False(t.Apply(Brain.Event("voice.state", new VoiceStatePayload("idle", false))));
    }
}

public class Milestone3MessageTests
{
    private static SchemaValidator V => Brain.Validator;

    [Fact]
    public void Schemas_Exist()
    {
        foreach (var topic in new[] { "notify", "confirm.request", "confirm.resolved" })
            Assert.True(V.HasSchema("event", topic), topic);
        foreach (var topic in new[] { "confirm.response", "notify.action" })
            Assert.True(V.HasSchema("command", topic), topic);
    }

    [Fact]
    public void WhatTheBodySends_Validates()
    {
        V.Validate(Envelope.Create("command", Topics.ConfirmResponse, new ConfirmResponsePayload("c1", true)));
        V.Validate(Envelope.Create("command", Topics.NotifyAction, new NotifyActionPayload("n1", "snooze")));
    }

    [Fact]
    public void BadAnswer_IsRejected()
    {
        var bad = Envelope.Create("command", Topics.ConfirmResponse,
            new JsonObject { ["confirm_id"] = "c1", ["approved"] = "yes" });
        Assert.Equal("E_BAD_PAYLOAD", Assert.Throws<BbpException>(() => V.Validate(bad)).Code);
    }

    [Fact]
    public void BrainEvents_Validate_AndDeserialize()
    {
        var note = Brain.Event("notify", new NotifyPayload("n1", "alert", "Resource pressure", "Sir, VRAM pressure.", []));
        V.Validate(note);
        V.Validate(Brain.Event("confirm.resolved", new ConfirmResolvedPayload("c1", true, "body")));
        Assert.Equal("Resource pressure", note.PayloadAs<NotifyPayload>().Title);
    }

    [Fact]
    public void Notify_UpdatesTheHudState()
    {
        var s = new BodyState();
        Assert.True(s.Apply(Brain.Event("notify", new NotifyPayload("n1", "warn", "Timer", "Your tea timer is done.", []))));
        Assert.Equal("Your tea timer is done.", s.LastNotification!.Body);
    }

    [Fact]
    public async Task ConfirmRequest_ReachesTheBody_AndTheAnswerGoesOut()
    {
        var socket = Brain.Socket();
        var opts = new BbpClientOptions { TokenProvider = () => "tok", PingInterval = TimeSpan.FromSeconds(30) };
        var client = new BbpClient(opts, Brain.Validator, new FakeFactory(n => n == 1 ? socket : null),
                                   delay: (d, ct) => Task.Delay(TimeSpan.FromMilliseconds(Math.Min(d.TotalMilliseconds, 50)), ct));
        var events = new List<Envelope>();
        var gotRequest = new TaskCompletionSource();
        client.EventReceived += e =>
        {
            lock (events) events.Add(e);
            if (e.Topic == Topics.ConfirmRequest)
                gotRequest.TrySetResult();
        };
        var connected = new TaskCompletionSource();
        client.StateChanged += (st, _) => { if (st == ConnectionState.Connected) connected.TrySetResult(); };
        using var cts = new CancellationTokenSource();
        var run = client.RunAsync(cts.Token);
        await connected.Task.WaitAsync(TimeSpan.FromSeconds(5));

        socket.Push(Brain.Event("confirm.request", new JsonObject
        {
            ["confirm_id"] = "c1", ["prompt"] = "Close Spotify?", ["risk"] = "high", ["timeout_s"] = 15, ["detail"] = "Spotify",
        }));
        await gotRequest.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var id = await client.SendCommandAsync(Topics.ConfirmResponse, new ConfirmResponsePayload("c1", false));

        var sent = socket.Sent.Single(m => (string?)m["id"] == id);
        Assert.Equal(("command", "confirm.response", "c1", false),
            ((string)sent["kind"]!, (string)sent["topic"]!, (string)sent["payload"]!["confirm_id"]!, (bool)sent["payload"]!["approved"]!));
        cts.Cancel();
        await run;
    }
}
