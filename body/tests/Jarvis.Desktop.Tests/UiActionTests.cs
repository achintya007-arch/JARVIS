using System.Text.Json;
using System.Text.Json.Nodes;
using Bbp.Client;

namespace Jarvis.Desktop.Tests;

/// <summary>A fake element desktop.uia.invoke / set_text acts on.</summary>
internal sealed class FakeTarget(string type, string name, string id = "", bool password = false,
                                 bool enabled = true, bool readOnly = false, string pattern = "invoke") : IUiTarget
{
    public List<string> Pressed { get; } = [];
    public string? Typed { get; private set; }
    public string Name => name;
    public string ControlType => type;
    public string AutomationId => id;
    public bool IsPassword => password;
    public bool IsEnabled => enabled;
    public bool IsReadOnly => readOnly;
    public IReadOnlyList<IUiElement> Children() => [];

    public string Press()
    {
        if (pattern == "none")
            throw new NotSupportedException("it can't be pressed without moving the mouse");
        Pressed.Add(pattern);
        return pattern;
    }

    public void SetText(string text) => Typed = text;
}

internal sealed class FakeActor(params FakeTarget[] targets) : IUiActor
{
    public List<(IReadOnlyList<IntPtr> Windows, UiElementRef Query)> Queries { get; } = [];

    public IReadOnlyList<IUiTarget> Find(IReadOnlyList<IntPtr> windows, UiElementRef query, int max = 10)
    {
        Queries.Add((windows, query));
        if (query.ControlType == "Bogus")
            throw new ArgumentException("'Bogus' isn't a UI Automation control type");
        return targets.Where(t =>
                (query.Name is null || string.Equals(t.Name, query.Name, StringComparison.OrdinalIgnoreCase))
                && (query.AutomationId is null || t.AutomationId == query.AutomationId)
                && (query.ControlType is null || t.ControlType == query.ControlType))
            .Take(max).Cast<IUiTarget>().ToList();
    }
}

public class RiskyNamesTests
{
    private static readonly string PolicyPath = Path.Combine(AppContext.BaseDirectory, RiskyNames.FileName);

    private static RiskyNames Shipped()
    {
        var (names, problem) = RiskyNames.Load(PolicyPath);
        Assert.Null(problem);
        return names;
    }

    public static TheoryData<string> Examples(string kind)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(PolicyPath));
        var data = new TheoryData<string>();
        foreach (var e in doc.RootElement.GetProperty("examples").GetProperty(kind).EnumerateArray())
            data.Add(e.GetString()!);
        return data;
    }

    [Theory]
    [MemberData(nameof(Examples), "risky")]
    public void SharedExamples_Risky(string label) => Assert.True(Shipped().IsRisky(label), label);

    [Theory]
    [MemberData(nameof(Examples), "safe")]
    public void SharedExamples_Safe(string label) => Assert.False(Shipped().IsRisky(label), label);

    [Fact]
    public void MissingOrBrokenList_MakesEverythingRisky()
    {
        var (names, problem) = RiskyNames.Load(Path.Combine(AppContext.BaseDirectory, "no-such-file.json"));
        Assert.NotNull(problem);
        Assert.True(names.IsRisky("Seven"));
    }

    [Fact]
    public void AnyOfSeveralTexts()
    {
        Assert.True(Shipped().IsRisky("→", "SendButton"));
        Assert.False(Shipped().IsRisky(null, "", "  "));
    }
}

public class UiActionTests
{
    private static readonly WindowRef Calc = new(401, "Calculator", "applicationframehost.exe");
    private static readonly WindowRef Notepad = new(101, "notes.txt - Notepad", "notepad.exe");
    private static readonly WindowRef Chrome = new(202, "Inbox - Google Chrome", "chrome.exe");
    private static readonly DateTimeOffset T0 = new(2026, 11, 16, 9, 0, 0, TimeSpan.Zero);

    private sealed class Rig
    {
        public required DesktopService Svc { get; init; }
        public required FakeActor Actor { get; init; }
        public required ApprovalLedger Approvals { get; init; }
        public DateTimeOffset Now { get; set; } = T0;
        private int _n;

        /// <summary>A confirmation for exactly this request, as the Brain sends it, answered as given.</summary>
        public string Approve(string topic, object payload, bool approved = true, bool resolve = true)
        {
            var id = $"c{++_n}";
            var p = Schemas.Payload(payload);
            var action = new JsonObject { ["topic"] = topic, ["app"] = p["app"]!.DeepClone(), ["element"] = p["element"]!.DeepClone() };
            if (p["text"] is { } text)
                action["text"] = text.DeepClone();
            Approvals.Apply(Envelope.Create("event", Topics.ConfirmRequest, new JsonObject
            {
                ["confirm_id"] = id, ["prompt"] = "About to …", ["risk"] = "high", ["timeout_s"] = 15, ["action"] = action,
            }));
            if (resolve)
                Resolve(id, approved);
            return id;
        }

        public void Resolve(string id, bool approved) =>
            Approvals.Apply(Envelope.Create("event", Topics.ConfirmResolved, new ConfirmResolvedPayload(id, approved, "body")));
    }

    private static Rig Make(FakeTarget[] targets, params WindowRef[] windows)
    {
        var allowlist = DesktopAllowlist.Load(Path.Combine(AppContext.BaseDirectory, "desktop.allowlist.json")).List;
        var actor = new FakeActor(targets);
        Rig? rig = null;
        var approvals = new ApprovalLedger(() => rig!.Now);
        var risky = RiskyNames.Load(Path.Combine(AppContext.BaseDirectory, RiskyNames.FileName)).Names;
        var svc = new DesktopService(allowlist, new FakeWindows(windows.Length > 0 ? windows : [Calc, Notepad, Chrome]),
                                     new FakeLauncher(), new FakeVolume(), new FakeReader(), null, actor, approvals, risky);
        return rig = new Rig { Svc = svc, Actor = actor, Approvals = approvals };
    }

    private static object Press(string name, string app = "calculator", string? confirmId = null, string? type = null) =>
        confirmId is null
            ? new { app, element = type is null ? (object)new { name } : new { name, control_type = type } }
            : new { app, element = type is null ? (object)new { name } : new { name, control_type = type }, confirm_id = confirmId };

    private static async Task<string> Code(Func<Task> act) => (await Assert.ThrowsAsync<BbpException>(act)).Code;

    private static void ValidRequest(string topic, object payload) =>
        Schemas.Validator.Validate(Envelope.Create("request", topic, Schemas.Payload(payload)));

    // ── invoke ─────────────────────────────────────────────────────────────
    [Fact]
    public async Task Invoke_PressesTheOneMatchingButton_InTheAppsOwnWindows()
    {
        var seven = new FakeTarget("Button", "Seven", "num7Button");
        var rig = Make([seven, new FakeTarget("Button", "Eight")]);
        ValidRequest("desktop.uia.invoke", Press("seven"));
        var result = await rig.Svc.InvokeAsync(Schemas.Payload(Press("seven")));
        Assert.Equal(["invoke"], seven.Pressed);
        Assert.Equal(("calculator", "Seven", "invoke"), (result.App, result.Name, result.Pattern));
        Assert.Equal([Calc.Hwnd], rig.Actor.Queries.Single().Windows);           // not Notepad's or Chrome's
        Schemas.AssertValidResponse("desktop.uia.invoke", result);
    }

    [Theory]
    [InlineData("toggle")]
    [InlineData("select")]
    public async Task Invoke_UsesToggleOrSelectWhenThatsWhatItHas(string pattern)
    {
        var box = new FakeTarget("CheckBox", "Scientific", pattern: pattern);
        var rig = Make([box]);
        var result = await rig.Svc.InvokeAsync(Schemas.Payload(Press("Scientific")));
        Assert.Equal(pattern, result.Pattern);
    }

    [Fact]
    public async Task Invoke_AppsWithoutActFlag_AreRefused_EvenIfReadable()
    {
        var rig = Make([new FakeTarget("Button", "Compose")]);
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("Compose", "chrome"))));
        Assert.Equal("E_NOT_ALLOWED", e.Code);
        Assert.Contains("\"act\": true", e.Detail);
        Assert.Empty(rig.Actor.Queries);                                       // nothing was even looked for
        Assert.Equal("E_NOT_ALLOWED", await Code(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("x", "bankapp")))));
    }

    [Fact]
    public async Task Invoke_NotFound_Ambiguous_NoWindow()
    {
        var rig = Make([new FakeTarget("Button", "OK"), new FakeTarget("Button", "ok"), new FakeTarget("Text", "OK")]);
        Assert.Equal("E_NOT_FOUND", await Code(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("Nine")))));
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("OK"))));
        Assert.Equal("E_NOT_FOUND", e.Code);
        Assert.Contains("3 elements match", e.Detail);
        Assert.Contains("say which", e.Detail);

        var noWindow = Make([new FakeTarget("Button", "OK")], Notepad);
        Assert.Contains("no open window",
            (await Assert.ThrowsAsync<BbpException>(() => noWindow.Svc.InvokeAsync(Schemas.Payload(Press("OK"))))).Detail);
    }

    [Fact]
    public async Task Invoke_RefusesPasswordFields_OtherControlTypes_Disabled_AndPatternless()
    {
        var rig = Make([
            new FakeTarget("Edit", "PIN", password: true), new FakeTarget("Edit", "Display"),
            new FakeTarget("Button", "Memory", enabled: false), new FakeTarget("Button", "Odd", pattern: "none"),
        ]);
        foreach (var (name, word) in new[] { ("PIN", "password"), ("Display", "doesn't press Edit"), ("Memory", "disabled"), ("Odd", "mouse") })
        {
            var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press(name))));
            Assert.Equal("E_NOT_ALLOWED", e.Code);
            Assert.Contains(word, e.Detail);
        }
        Assert.Equal("E_BAD_PAYLOAD", await Code(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("PIN", type: "Bogus")))));
    }

    [Fact]
    public async Task Invoke_RiskyElement_NeedsAnApprovalForExactlyThatRequest()
    {
        var send = new FakeTarget("Button", "Send");
        var rig = Make([send, new FakeTarget("Button", "Seven")]);
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("Send"))));
        Assert.Equal("E_DENIED", e.Code);
        Assert.Contains("needs your approval", e.Detail);
        Assert.Empty(send.Pressed);

        var id = rig.Approve("desktop.uia.invoke", Press("Send"));
        await rig.Svc.InvokeAsync(Schemas.Payload(Press("Send", confirmId: id)));
        Assert.Equal(["invoke"], send.Pressed);

        // once only
        Assert.Contains("already used", (await Assert.ThrowsAsync<BbpException>(
            () => rig.Svc.InvokeAsync(Schemas.Payload(Press("Send", confirmId: id))))).Detail);
        Assert.Single(send.Pressed);
    }

    [Fact]
    public async Task Invoke_RiskyByWhatTheBodyFinds_NotJustWhatTheBrainSaid()
    {
        var hidden = new FakeTarget("Button", "→", "SendButton");
        var rig = Make([hidden]);
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("→"))));
        Assert.Equal("E_DENIED", e.Code);
        Assert.Empty(hidden.Pressed);
    }

    [Fact]
    public async Task Invoke_ApprovalRefusals()
    {
        var rig = Make([new FakeTarget("Button", "Send"), new FakeTarget("Button", "Delete")]);
        async Task<string> Refusal(object payload) =>
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(payload)))).Detail;

        var denied = rig.Approve("desktop.uia.invoke", Press("Send"), approved: false);
        Assert.Contains("you denied it", await Refusal(Press("Send", confirmId: denied)));

        var other = rig.Approve("desktop.uia.invoke", Press("Delete"));
        Assert.Contains("something else", await Refusal(Press("Send", confirmId: other)));

        var otherApp = rig.Approve("desktop.uia.invoke", Press("Send", "notepad"));
        Assert.Contains("something else", await Refusal(Press("Send", confirmId: otherApp)));

        Assert.Contains("never saw", await Refusal(Press("Send", confirmId: "forged")));

        var late = rig.Approve("desktop.uia.invoke", Press("Send"));
        rig.Now = T0 + ApprovalLedger.UseWithin + TimeSpan.FromSeconds(1);
        Assert.Contains("expired", await Refusal(Press("Send", confirmId: late)));

        var dropped = rig.Approve("desktop.uia.invoke", Press("Send"));
        rig.Approvals.Clear();                                                    // the link dropped
        Assert.Contains("never saw", await Refusal(Press("Send", confirmId: dropped)));
    }

    [Fact]
    public async Task Invoke_WaitsBrieflyForAResolutionQueuedAfterTheRequest()
    {
        var send = new FakeTarget("Button", "Send");
        var rig = Make([send]);
        var id = rig.Approve("desktop.uia.invoke", Press("Send"), resolve: false);
        var pressing = rig.Svc.InvokeAsync(Schemas.Payload(Press("Send", confirmId: id)));
        await Task.Delay(100);
        Assert.False(pressing.IsCompleted);
        rig.Resolve(id, true);
        await pressing;
        Assert.Single(send.Pressed);
    }

    [Fact]
    public async Task Invoke_UnansweredConfirmation_IsRefused()
    {
        var rig = Make([new FakeTarget("Button", "Send")]);
        var id = rig.Approve("desktop.uia.invoke", Press("Send"), resolve: false);
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("Send", confirmId: id))));
        Assert.Contains("hasn't been answered", e.Detail);
    }

    // ── set_text ───────────────────────────────────────────────────────────
    private static object Type(string text, string? confirmId, string name = "Search") =>
        new { app = "notepad", element = new { name, control_type = "Edit" }, text, confirm_id = confirmId ?? "none" };

    [Fact]
    public async Task SetText_AlwaysNeedsApproval_AndTypesExactlyThat()
    {
        var box = new FakeTarget("Edit", "Search");
        var rig = Make([box], Notepad);
        Assert.Equal("E_DENIED", await Code(() => rig.Svc.SetTextAsync(Schemas.Payload(Type("hello", null)))));
        Assert.Null(box.Typed);

        var id = rig.Approve("desktop.uia.set_text", Type("hello\nworld", null));
        ValidRequest("desktop.uia.set_text", Type("hello\nworld", id));
        var result = await rig.Svc.SetTextAsync(Schemas.Payload(Type("hello\nworld", id)));
        Assert.Equal("hello\nworld", box.Typed);
        Assert.Equal(11, result.Length);
        Schemas.AssertValidResponse("desktop.uia.set_text", result);

        var other = rig.Approve("desktop.uia.set_text", Type("hello", null));
        Assert.Contains("something else",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Type("hello!", other))))).Detail);
    }

    [Theory]
    [InlineData("a‮b", "right-to-left")]
    [InlineData("a​b", "invisible")]
    [InlineData("a\u0007b", "control")]
    [InlineData("a\rb", "control")]
    public async Task SetText_RefusesTextThatWouldLookDifferentInTheDialog(string text, string word)
    {
        var box = new FakeTarget("Edit", "Search");
        var rig = Make([box], Notepad);
        var id = rig.Approve("desktop.uia.set_text", Type(text, null));
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Type(text, id))));
        Assert.Equal("E_BAD_PAYLOAD", e.Code);
        Assert.Contains(word, e.Detail);
        Assert.Null(box.Typed);
    }

    [Fact]
    public async Task SetText_OnlyEditFields_NotReadOnly_NotPasswords()
    {
        var rig = Make([new FakeTarget("Edit", "Shown", readOnly: true), new FakeTarget("Edit", "Password", password: true),
                        new FakeTarget("Document", "Text editor")], Notepad);
        var id = rig.Approve("desktop.uia.set_text", Type("x", null, "Shown"));
        Assert.Contains("read-only",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Type("x", id, "Shown"))))).Detail);
        Assert.Contains("password",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Type("x", "c", "Password"))))).Detail);
        var doc = new { app = "notepad", element = new { name = "Text editor" }, text = "x", confirm_id = "c" };
        Assert.Contains("doesn't type into Document",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(doc)))).Detail);
    }

    [Fact]
    public void Schemas_RequireATarget_AndConfirmIdForTyping()
    {
        Assert.Throws<BbpException>(() => ValidRequest("desktop.uia.invoke", new { app = "calculator", element = new { control_type = "Button" } }));
        Assert.Throws<BbpException>(() => ValidRequest("desktop.uia.set_text",
            new { app = "notepad", element = new { name = "Search" }, text = "x" }));
        Assert.Throws<BbpException>(() => ValidRequest("desktop.uia.set_text",
            new { app = "notepad", element = new { name = "Search" }, text = new string('x', 2001), confirm_id = "c" }));
    }

    [Fact]
    public void ShippedAllowlist_ActOnlyOnNotepadAndCalculator()
    {
        var list = DesktopAllowlist.Load(Path.Combine(AppContext.BaseDirectory, "desktop.allowlist.json")).List;
        Assert.Equal(["calculator", "notepad"], list.Apps.Where(a => a.Act).Select(a => a.Id).Order());
    }
}
