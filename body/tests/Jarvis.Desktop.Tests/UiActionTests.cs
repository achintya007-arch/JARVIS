using System.Text.Json;
using System.Text.Json.Nodes;
using Bbp.Client;

namespace Jarvis.Desktop.Tests;

/// <summary>A fake element desktop.uia.invoke / set_text acts on.</summary>
internal sealed class FakeTarget(string type, string name, string id = "", bool password = false,
                                 bool enabled = true, bool readOnly = false, string pattern = "invoke",
                                 bool offscreen = false, int textLength = 0) : IUiTarget
{
    public List<string> Pressed { get; } = [];
    public string? Typed { get; private set; }
    public string Name => name;
    public string ControlType => type;
    public string AutomationId => id;
    public bool IsPassword => password;
    public bool IsEnabled => enabled;
    public bool IsReadOnly => readOnly;
    public bool IsOffscreen => offscreen;
    public int TextLength => textLength;
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

    public int CandidateCalls { get; private set; }

    public IReadOnlyList<IUiTarget> Candidates(IReadOnlyList<IntPtr> windows, IReadOnlyCollection<string> controlTypes, int max = 500)
    {
        CandidateCalls++;
        return targets.Where(t => t.Name.Length > 0 && controlTypes.Contains(t.ControlType, StringComparer.OrdinalIgnoreCase))
            .Take(max).Cast<IUiTarget>().ToList();
    }
}

/// <summary>An app whose window shows up only after a few looks (it was just opened).</summary>
internal sealed class LateWindows(int hiddenFor, params WindowRef[] windows) : IWindowSystem
{
    public int Looks { get; private set; }
    public IReadOnlyList<WindowRef> List() => ++Looks > hiddenFor ? windows : [];
    public WindowRef? ForegroundExcludingSelf() => null;
    public string Focus(IntPtr hwnd) => "none";
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

    private static Rig Make(FakeTarget[] targets, params WindowRef[] windows) =>
        Make(targets, new FakeWindows(windows.Length > 0 ? windows : [Calc, Notepad, Chrome]), TimeSpan.Zero);

    private static Rig Make(FakeTarget[] targets, IWindowSystem windows, TimeSpan findWait)
    {
        var allowlist = DesktopAllowlist.Load(Path.Combine(AppContext.BaseDirectory, "desktop.allowlist.json")).List;
        var actor = new FakeActor(targets);
        Rig? rig = null;
        var approvals = new ApprovalLedger(() => rig!.Now);
        var risky = RiskyNames.Load(Path.Combine(AppContext.BaseDirectory, RiskyNames.FileName)).Names;
        var svc = new DesktopService(allowlist, windows, new FakeLauncher(), new FakeVolume(), new FakeReader(),
                                     null, actor, approvals, risky) { FindWait = findWait };
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
        // Digit-only automation ids (WinForms: the window handle) don't tell them apart.
        var rig = Make([new FakeTarget("Button", "OK", "4327900"), new FakeTarget("Button", "ok", "3736734"), new FakeTarget("Text", "OK")]);
        Assert.Equal("E_NOT_FOUND", await Code(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("Nine")))));
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("OK"))));
        Assert.Equal("E_NOT_FOUND", e.Code);
        Assert.Contains("2 elements match 'OK' in Calculator", e.Detail);  // the Text isn't something to press
        Assert.Contains("look the same", e.Detail);
        Assert.Contains("won't guess", e.Detail);
        Assert.DoesNotContain("4327900", e.Detail);

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
    private static object Doc(string text, string? confirmId) =>
        new { app = "notepad", element = new { name = "Text editor", control_type = "Document" }, text, confirm_id = confirmId ?? "none" };

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
                        new FakeTarget("Text", "Text editor")], Notepad);
        var id = rig.Approve("desktop.uia.set_text", Type("x", null, "Shown"));
        Assert.Contains("read-only",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Type("x", id, "Shown"))))).Detail);
        Assert.Contains("password",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Type("x", "c", "Password"))))).Detail);
        var doc = new { app = "notepad", element = new { name = "Text editor" }, text = "x", confirm_id = "c" };
        Assert.Contains("doesn't type into Text",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(doc)))).Detail);
    }

    [Fact]
    public async Task SetText_IntoADocument_OnlyWhenItsEmpty()
    {
        var full = new FakeTarget("Document", "Text editor", textLength: 120);
        var rig = Make([full], Notepad);
        var id = rig.Approve("desktop.uia.set_text", Doc("x", null));
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.SetTextAsync(Schemas.Payload(Doc("x", id))));
        Assert.Equal("E_NOT_ALLOWED", e.Code);
        Assert.Contains("isn't empty", e.Detail);
        Assert.Null(full.Typed);

        var unknown = new FakeTarget("Document", "Text editor", textLength: -1);   // can't tell: not typed into
        rig = Make([unknown], Notepad);
        id = rig.Approve("desktop.uia.set_text", Doc("x", null));
        Assert.Equal("E_NOT_ALLOWED", await Code(() => rig.Svc.SetTextAsync(Schemas.Payload(Doc("x", id)))));

        var empty = new FakeTarget("Document", "Text editor");
        rig = Make([empty], Notepad);
        var payload = new { app = "notepad", element = new { name = "Text editor", control_type = "Document" }, text = "Dear Sir", confirm_id = "?" };
        id = rig.Approve("desktop.uia.set_text", payload);
        await rig.Svc.SetTextAsync(Schemas.Payload(payload with { confirm_id = id }));
        Assert.Equal("Dear Sir", empty.Typed);
    }

    // ── finding the element (amendment 22) ──────────────────────────────────
    [Theory]
    [InlineData("7", "Seven")]
    [InlineData("multiply", "Multiply by")]
    [InlineData("equal", "Equals")]
    [InlineData("root", "Square root")]
    [InlineData("clear", "Clear")]                       // exact wins over "Clear entry"
    [InlineData("CLEAR ENTRY", "Clear entry")]
    [InlineData("decimal", "Decimal separator")]
    public async Task Invoke_FindsTheButtonYouMeant_WhenOnlyOneFits(string said, string pressed)
    {
        FakeTarget[] calc = [new("Button", "Seven"), new("Button", "Multiply by"), new("Button", "Equals"),
                             new("Button", "Square root"), new("Button", "Clear"), new("Button", "Clear entry"),
                             new("Button", "Decimal separator"), new("Text", "Display is 0")];
        var rig = Make(calc);
        var result = await rig.Svc.InvokeAsync(Schemas.Payload(Press(said)));
        Assert.Equal(pressed, result.Name);
        Assert.Equal(["invoke"], calc.Single(t => t.Name == pressed).Pressed);
        Assert.Single(calc, t => t.Pressed.Count > 0);
    }

    [Fact]
    public async Task Invoke_LooseMatch_SeveralFit_ListsThem_PressesNothing()
    {
        FakeTarget[] els = [new("Button", "Divide by"), new("MenuItem", "Divide evenly", "divEven")];
        var rig = Make(els);
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("divide"))));
        Assert.Equal("E_NOT_FOUND", e.Code);
        Assert.Contains("'Divide by' (Button)", e.Detail);
        Assert.Contains("'Divide evenly' (MenuItem, automation id divEven)", e.Detail);
        Assert.Contains("say which one", e.Detail);
        Assert.All(els, t => Assert.Empty(t.Pressed));
    }

    [Fact]
    public async Task Invoke_LooseMatch_NeverLandsOnARiskyElement()
    {
        // "click archive" must not become "Archive and delete", even approved:
        // the approval named 'archive'. A risky element has to be named in full.
        var risky = new FakeTarget("Button", "Archive and delete");
        var rig = Make([risky]);
        var id = rig.Approve("desktop.uia.invoke", Press("archive"));
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("archive", confirmId: id))));
        Assert.Equal("E_NOT_FOUND", e.Code);
        Assert.Contains("the closest is 'Archive and delete'", e.Detail);
        Assert.Contains("full name", e.Detail);
        Assert.Empty(risky.Pressed);
    }

    [Fact]
    public async Task Invoke_NotFound_SuggestsCloseSpellings_ButDoesntPressThem()
    {
        var equals = new FakeTarget("Button", "Equals");
        var rig = Make([equals, new FakeTarget("Button", "Seven")]);
        var e = await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("equls"))));
        Assert.Equal("E_NOT_FOUND", e.Code);
        Assert.Contains("did you mean 'Equals'?", e.Detail);
        Assert.Empty(equals.Pressed);
        Assert.DoesNotContain("did you mean",
            (await Assert.ThrowsAsync<BbpException>(() => rig.Svc.InvokeAsync(Schemas.Payload(Press("zebra"))))).Detail);
    }

    [Theory]
    [InlineData("sned", "send", 1)]       // swapped letters: one edit
    [InlineData("equl", "equal", 1)]
    [InlineData("seven", "seven", 0)]
    [InlineData("zebra", "equal", 4)]
    public void Matcher_Distance(string a, string b, int edits) => Assert.Equal(edits, UiMatcher.Distance(a, b));

    [Fact]
    public void Matcher_Words_IgnoreCasePunctuationPluralsAndDigits()
    {
        Assert.Equal(["sign", "in"], UiMatcher.Words("Sign-In"));
        Assert.Equal(["seven"], UiMatcher.Words("7"));
        Assert.Equal(["equal"], UiMatcher.Words("Equals"));
        Assert.Equal(["send", "button"], UiMatcher.Words("sendButtons"));
        Assert.Equal(["class"], UiMatcher.Words("Class"));           // -ss stays
    }

    [Fact]
    public async Task Invoke_AnAutomationId_IsExactOrNothing()
    {
        var seven = new FakeTarget("Button", "Seven", "num7Button");
        var rig = Make([seven]);
        var byId = new { app = "calculator", element = new { automation_id = "num7" } };
        Assert.Equal("E_NOT_FOUND", await Code(() => rig.Svc.InvokeAsync(Schemas.Payload(byId))));
        Assert.Equal(0, rig.Actor.CandidateCalls);
        Assert.Empty(seven.Pressed);
    }

    [Fact]
    public async Task Invoke_SeveralWithTheSameName_KeepsTheOneThatCanBeMeant()
    {
        var shown = new FakeTarget("Button", "Save");
        var rig = Make([new FakeTarget("Button", "Save", enabled: false), shown, new FakeTarget("Button", "Save", offscreen: true)]);
        await rig.Svc.InvokeAsync(Schemas.Payload(Press("Save")));
        Assert.Equal(["invoke"], shown.Pressed);
    }

    [Fact]
    public async Task Invoke_WaitsForAWindowThatIsStillOpening()
    {
        var one = new FakeTarget("Button", "One");
        var late = new LateWindows(2, Calc);
        var rig = Make([one], late, TimeSpan.FromSeconds(5));
        await rig.Svc.InvokeAsync(Schemas.Payload(Press("one")));
        Assert.Equal(["invoke"], one.Pressed);
        Assert.Equal(3, late.Looks);

        var never = Make([one], new LateWindows(int.MaxValue, Calc), TimeSpan.FromMilliseconds(300));
        var clock = System.Diagnostics.Stopwatch.StartNew();
        Assert.Contains("no open window",
            (await Assert.ThrowsAsync<BbpException>(() => never.Svc.InvokeAsync(Schemas.Payload(Press("one"))))).Detail);
        Assert.InRange(clock.ElapsedMilliseconds, 250, 3000);
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
