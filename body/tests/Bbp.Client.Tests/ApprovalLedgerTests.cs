using System.Text.Json.Nodes;

namespace Bbp.Client.Tests;

/// <summary>Amendment 20: the Body's own record of what you approved.</summary>
public class ApprovalLedgerTests
{
    private static readonly ConfirmAction Send =
        new("desktop.uia.invoke", "discord", new UiElementRef("Send", "Button"));

    private static JsonObject ActionJson(ConfirmAction a)
    {
        var element = new JsonObject();
        if (a.Element.Name is not null) element["name"] = a.Element.Name;
        if (a.Element.ControlType is not null) element["control_type"] = a.Element.ControlType;
        if (a.Element.AutomationId is not null) element["automation_id"] = a.Element.AutomationId;
        var json = new JsonObject { ["topic"] = a.Topic, ["app"] = a.App, ["element"] = element };
        if (a.Text is not null) json["text"] = a.Text;
        return json;
    }

    private static Envelope Request(string id, ConfirmAction? action) =>
        Brain.Event("confirm.request", new JsonObject
        {
            ["confirm_id"] = id, ["prompt"] = "About to press 'Send' in Discord. Shall I proceed, Sir?",
            ["risk"] = "high", ["timeout_s"] = 15, ["detail"] = "Discord › Button 'Send'",
            ["action"] = action is null ? null : ActionJson(action),
        });

    private static Envelope Resolved(string id, bool approved) =>
        Brain.Event("confirm.resolved", new ConfirmResolvedPayload(id, approved, "body"));

    [Fact]
    public async Task ApprovedOnce_ForExactlyThatAction()
    {
        var ledger = new ApprovalLedger();
        ledger.Apply(Request("c1", Send));
        ledger.Apply(Resolved("c1", true));
        Assert.Equal("that approval was for something else",
            await ledger.CheckAsync("c1", Send with { Element = new UiElementRef("Delete", "Button") }));
        Assert.Null(await ledger.CheckAsync("c1", Send with { App = "Discord" }));       // app id: case-insensitive
        Assert.Equal("that approval was already used", await ledger.CheckAsync("c1", Send));
    }

    [Fact]
    public async Task ConfirmationsWithoutAnAction_ApproveNoDesktopRequest()
    {
        var ledger = new ApprovalLedger();
        ledger.Apply(Request("c1", null));                // e.g. write_file
        ledger.Apply(Resolved("c1", true));
        Assert.Equal("that approval was for something else", await ledger.CheckAsync("c1", Send));
    }

    [Fact]
    public async Task TextMustMatchExactly()
    {
        var typing = new ConfirmAction("desktop.uia.set_text", "notepad", new UiElementRef("Search", "Edit"), "hello");
        var ledger = new ApprovalLedger();
        ledger.Apply(Request("c1", typing));
        ledger.Apply(Resolved("c1", true));
        Assert.NotNull(await ledger.CheckAsync("c1", typing with { Text = "hello " }));
        Assert.Null(await ledger.CheckAsync("c1", typing));
    }

    [Fact]
    public async Task ResolutionWithoutTheRequest_IsIgnored()
    {
        var ledger = new ApprovalLedger();
        ledger.Apply(Resolved("c1", true));
        Assert.Equal("this Body never saw that confirmation", await ledger.CheckAsync("c1", Send));
    }

    [Fact]
    public async Task WelcomeSnapshot_RecordsPendingActions()
    {
        var ledger = new ApprovalLedger();
        ledger.ApplyWelcome(new WelcomePayload("s", "3.0", Brain.EmptySnapshot with
        {
            PendingConfirmations = [new PendingConfirmation("c9", "Press Send?", "high", 8, "Discord › Button 'Send'", Send)],
        }));
        ledger.Apply(Resolved("c9", true));
        Assert.Null(await ledger.CheckAsync("c9", Send));
    }

    [Fact]
    public void ConfirmRequestWithAction_MatchesTheSchema_AndTheDialogGetsTheText()
    {
        var typing = new ConfirmAction("desktop.uia.set_text", "notepad", new UiElementRef("Search", "Edit"), "line 1\nline 2");
        var message = Request("c1", typing);
        Brain.Validator.Validate(message.ToNode());
        var tracker = new ConfirmationTracker();
        ConfirmationDialog? opened = null;
        tracker.Opened += d => opened = d;
        tracker.Apply(message);
        Assert.Equal("line 1\nline 2", opened!.Text);
        Assert.True(opened.HasText);

        tracker.Apply(Request("c2", Send));
        Assert.False(tracker.Open.Single(d => d.ConfirmId == "c2").HasText);
    }
}
