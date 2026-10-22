using System.Text.Json.Nodes;
using Bbp.Client;

namespace Jarvis.Desktop.Tests;

public class AllowlistTests
{
    private static DesktopAllowlist Shipped()
    {
        var (list, problem) = DesktopAllowlist.Load(Path.Combine(AppContext.BaseDirectory, "desktop.allowlist.json"));
        Assert.Null(problem);
        return list;
    }

    [Fact]
    public void ShippedAllowlist_UsesTheBrainsAppIds()
    {
        // Keep in step with jarvis_v1/action/app_launcher.ALLOWED_APPS.
        string[] brainIds = ["notepad", "calculator", "paint", "task manager", "explorer", "chrome", "firefox",
                             "vscode", "spotify", "discord", "todoist", "word", "excel", "outlook"];
        Assert.Equal(brainIds.Order(), Shipped().Apps.Select(a => a.Id).Order());
    }

    [Fact]
    public void ShippedAllowlist_HasNoShells()
    {
        foreach (var app in Shipped().Apps)
        {
            Assert.DoesNotContain(app.Exe ?? "", new[] { "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe" },
                                  StringComparer.OrdinalIgnoreCase);
            Assert.NotEmpty(app.Processes ?? []);
        }
    }

    [Theory]
    [InlineData("vscode", "vscode")]
    [InlineData("VS Code", "vscode")]
    [InlineData("calc", "calculator")]
    [InlineData("Google Chrome", "chrome")]
    public void FindsByIdOrAlias(string name, string id) => Assert.Equal(id, Shipped().Find(name)?.Id);

    [Theory]
    [InlineData("cmd")]
    [InlineData("powershell")]
    [InlineData("notepad & whoami")]
    [InlineData("C:\\Windows\\System32\\cmd.exe")]
    public void UnknownNamesAreNotFound(string name) => Assert.Null(Shipped().Find(name));

    [Fact]
    public void HostedAppsMatchByTitle()
    {
        var list = Shipped();
        Assert.Equal("calculator", list.ForWindow("applicationframehost.exe", "Calculator")?.Id);
        Assert.Null(list.ForWindow("applicationframehost.exe", "Settings"));     // same host, other app
    }

    [Fact]
    public void MissingOrBrokenFile_FailsClosed()
    {
        var dir = Directory.CreateTempSubdirectory("allow");
        try
        {
            var (missing, p1) = DesktopAllowlist.Load(Path.Combine(dir.FullName, "nope.json"));
            Assert.Empty(missing.Apps);
            Assert.NotNull(p1);

            var broken = Path.Combine(dir.FullName, "broken.json");
            File.WriteAllText(broken, "{ not json");
            var (bad, p2) = DesktopAllowlist.Load(broken);
            Assert.Empty(bad.Apps);
            Assert.Contains("invalid", p2);
        }
        finally
        {
            dir.Delete(true);
        }
    }
}

public class UiTreeBuilderTests
{
    private static readonly UiLimits Limits = new();

    private static IEnumerable<UiNode> All(UiNode n) =>
        new[] { n }.Concat((n.Children ?? []).SelectMany(All));

    [Fact]
    public void PasswordFields_AreRemovedWithTheirSubtrees()
    {
        var root = El.Make("Window", "Sign in", children:
        [
            El.Make("Edit", "Username", "user"),
            El.Make("Edit", "", "pwd-box", password: true, children: [El.Make("Text", "hunter2")]),
            El.Make("Edit", "Card security code", "cvc"),
            El.Make("Button", "Sign in"),
        ]);
        var names = All(UiTreeBuilder.Build(root, Limits).Root).Select(n => n.Name ?? n.AutomationId).ToList();
        Assert.Equal(["Sign in", "Username", "Sign in"], names);
        Assert.DoesNotContain("hunter2", names);
    }

    [Fact]
    public void UnnamedContainers_AreFlattened_AndEmptyLeavesDropped()
    {
        var root = El.Make("Window", "App", children:
        [
            El.Make("Pane", children: [El.Make("Group", children: [El.Make("Button", "OK")])]),
            El.Make("Pane"),                                    // empty leaf
        ]);
        var tree = UiTreeBuilder.Build(root, Limits);
        var child = Assert.Single(tree.Root.Children!);
        Assert.Equal(("Button", "OK"), (child.ControlType, child.Name));
        Assert.Equal(2, tree.NodeCount);
        Assert.False(tree.Truncated);
    }

    [Fact]
    public void NodeCap_Truncates()
    {
        var root = El.Make("Window", "Big", children: Enumerable.Range(0, 50).Select(i => (IUiElement)El.Make("ListItem", $"Item {i}")).ToList());
        var tree = UiTreeBuilder.Build(root, Limits with { MaxNodes = 10 });
        Assert.Equal(10, tree.NodeCount);
        Assert.True(tree.Truncated);
    }

    [Fact]
    public void DepthCap_Truncates()
    {
        IUiElement deep = El.Make("Text", "leaf");
        for (var i = 0; i < 20; i++)
            deep = El.Make("TreeItem", $"level {i}", children: [deep]);
        var tree = UiTreeBuilder.Build(El.Make("Window", "Tree", children: [deep]), Limits with { MaxDepth = 5 });
        Assert.True(tree.Truncated);
        Assert.True(tree.NodeCount <= 6);
    }

    [Fact]
    public void TimeBudget_Truncates()
    {
        var slow = Enumerable.Range(0, 20).Select(i => (IUiElement)El.Make("ListItem", $"Item {i}",
            children: [El.Make("Text", "x")], onChildren: () => Thread.Sleep(15))).ToList();
        var tree = UiTreeBuilder.Build(El.Make("Window", "Slow", children: slow), Limits with { BudgetMs = 60 });
        Assert.True(tree.Truncated);
    }

    [Fact]
    public void Names_AreCollapsedAndCut()
    {
        var root = El.Make("Window", "W", children: [El.Make("Text", "  many   spaces\n\there "), El.Make("Text", new string('x', 500))]);
        var kids = UiTreeBuilder.Build(root, Limits).Root.Children!;
        Assert.Equal("many spaces here", kids[0].Name);
        Assert.Equal(200, kids[1].Name!.Length);
        Assert.EndsWith("…", kids[1].Name);
    }

    [Fact]
    public void VanishingElements_DontThrow()
    {
        var root = El.Make("Window", "W", children: [El.Make("Button", "Gone", onChildren: () => throw new InvalidOperationException("element not available"))]);
        Assert.Equal(2, UiTreeBuilder.Build(root, Limits).NodeCount);
    }
}

public class DesktopServiceTests
{
    private static readonly WindowRef Notepad = new(101, "notes.txt - Notepad", "notepad.exe");
    private static readonly WindowRef Chrome = new(202, "Inbox - Google Chrome", "chrome.exe");
    private static readonly WindowRef Banking = new(303, "MyBank - Secure", "bankapp.exe");

    private static (DesktopService Svc, FakeWindows Win, FakeLauncher Launch, FakeVolume Vol, FakeReader Reader) Make(params WindowRef[] windows)
    {
        var allowlist = DesktopAllowlist.Load(Path.Combine(AppContext.BaseDirectory, "desktop.allowlist.json")).List;
        var (win, launch, vol, reader) = (new FakeWindows(windows), new FakeLauncher(), new FakeVolume(), new FakeReader());
        return (new DesktopService(allowlist, win, launch, vol, reader), win, launch, vol, reader);
    }

    private static string Code(Action act) => Assert.Throws<BbpException>(act).Code;

    // ── launch ─────────────────────────────────────────────────────────────
    [Fact]
    public void Launch_AllowedApp()
    {
        var (svc, _, launch, _, _) = Make();
        var result = svc.Launch(Schemas.Payload(new { app_id = "notepad" }));
        Assert.Equal(["notepad"], launch.Launched);
        Schemas.AssertValidResponse("desktop.app.launch", result);
    }

    [Theory]
    [InlineData("cmd")]
    [InlineData("notepad & whoami")]
    [InlineData("bankapp")]
    public void Launch_NotOnTheBodysAllowlist_IsRefused_WhateverTheBrainSays(string id)
    {
        var (svc, _, launch, _, _) = Make();
        var e = Assert.Throws<BbpException>(() => svc.Launch(Schemas.Payload(new { app_id = id })));
        Assert.Equal("E_NOT_ALLOWED", e.Code);
        Assert.Contains(id, e.Detail);
        Assert.Empty(launch.Launched);
    }

    [Fact]
    public void Launch_NotInstalled_IsNotFound()
    {
        var (svc, _, launch, _, _) = Make();
        launch.NotInstalled = true;
        Assert.Equal("E_NOT_FOUND", Code(() => svc.Launch(Schemas.Payload(new { app_id = "word" }))));
    }

    // ── focus ──────────────────────────────────────────────────────────────
    [Fact]
    public void Focus_ByApp_PicksItsMostRecentWindow()
    {
        var older = Notepad with { Hwnd = 999 };
        var (svc, win, _, _, _) = Make(Chrome, Notepad, older);       // z-order: most recent first
        var result = svc.Focus(Schemas.Payload(new { app = "notepad" }));
        Assert.Equal([Notepad.Hwnd], win.Focused);
        Assert.Equal("input-unlock", result.Strategy);
        Schemas.AssertValidResponse("desktop.window.focus", result);
    }

    [Fact]
    public void Focus_ByTitle_OnlyAllowlistedApps()
    {
        var (svc, win, _, _, _) = Make(Banking, Notepad);
        var e = Assert.Throws<BbpException>(() => svc.Focus(Schemas.Payload(new { title_contains = "MyBank" })));
        Assert.Equal("E_NOT_ALLOWED", e.Code);
        Assert.Contains("bankapp", e.Detail);
        Assert.Empty(win.Focused);

        svc.Focus(Schemas.Payload(new { title_contains = "notes.txt" }));
        Assert.Equal([Notepad.Hwnd], win.Focused);
    }

    [Fact]
    public void Focus_Errors()
    {
        var (svc, win, _, _, _) = Make(Notepad);
        Assert.Equal("E_NOT_FOUND", Code(() => svc.Focus(Schemas.Payload(new { app = "chrome" }))));
        Assert.Equal("E_NOT_FOUND", Code(() => svc.Focus(Schemas.Payload(new { title_contains = "nothing like this" }))));
        Assert.Equal("E_NOT_ALLOWED", Code(() => svc.Focus(Schemas.Payload(new { app = "bankapp" }))));
        win.RefuseFocus = true;
        var e = Assert.Throws<BbpException>(() => svc.Focus(Schemas.Payload(new { app = "notepad" })));
        Assert.Equal("E_INTERNAL", e.Code);
        Assert.Contains("tried", e.Detail);
    }

    // ── list ───────────────────────────────────────────────────────────────
    [Fact]
    public void List_MarksWhatMayBeTouched()
    {
        var (svc, _, _, _, _) = Make(Notepad, Banking);
        var result = svc.List();
        Assert.Equal([("notepad", true), ("bankapp", false)], result.Windows.Select(w => (w.App, w.Allowed)));
        Schemas.AssertValidResponse("desktop.window.list", result);
    }

    // ── volume ─────────────────────────────────────────────────────────────
    [Theory]
    [InlineData("level", 30, 30)]
    [InlineData("delta", 10, 60)]
    [InlineData("delta", -80, 0)]
    [InlineData("delta", 90, 100)]
    public void Volume_LevelOrDelta_Clamped(string field, int value, int expected)
    {
        var (svc, _, _, vol, _) = Make();
        var result = svc.SetVolume(new JsonObject { [field] = value });
        Assert.Equal(expected, result.Level);
        Assert.Equal(expected, vol.Level);
        Schemas.AssertValidResponse("desktop.audio.set_volume", result);
    }

    // ── uia.read ───────────────────────────────────────────────────────────
    [Fact]
    public void Read_DefaultsToTheForegroundApp_WhenAllowlisted()
    {
        var (svc, win, _, _, reader) = Make(Notepad, Chrome);
        win.Foreground = Notepad;
        var result = svc.Read(new JsonObject());
        Assert.Equal(("notepad", Notepad.Title), (result.App, result.Title));
        Assert.Equal((Notepad.Hwnd, "window"), (reader.Reads[0].Hwnd, reader.Reads[0].Scope));
        Schemas.AssertValidResponse("desktop.uia.read", result);
    }

    [Fact]
    public void Read_ForegroundNotAllowlisted_IsRefused_NamingTheApp()
    {
        var (svc, win, _, _, reader) = Make(Banking, Notepad);
        win.Foreground = Banking;
        var e = Assert.Throws<BbpException>(() => svc.Read(new JsonObject()));
        Assert.Equal("E_NOT_ALLOWED", e.Code);
        Assert.Contains("bankapp", e.Detail);
        Assert.Empty(reader.Reads);                               // nothing was read
    }

    [Fact]
    public void Read_NamedApp_AndLimits()
    {
        var (svc, _, _, _, reader) = Make(Chrome, Notepad);
        svc.Read(Schemas.Payload(new { app = "notepad", scope = "focused", max_depth = 4, max_nodes = 50 }));
        var (hwnd, scope, limits) = reader.Reads.Single();
        Assert.Equal((Notepad.Hwnd, "focused", 4, 50), (hwnd, scope, limits.MaxDepth, limits.MaxNodes));
        Assert.Equal("E_NOT_ALLOWED", Code(() => svc.Read(Schemas.Payload(new { app = "bankapp" }))));
    }
}
