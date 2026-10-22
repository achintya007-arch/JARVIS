using System.Text.Json.Nodes;
using Bbp.Client;

namespace Jarvis.Desktop.Tests;

internal sealed class FakeWindows(params WindowRef[] windows) : IWindowSystem
{
    public List<WindowRef> Windows { get; } = [.. windows];
    public WindowRef? Foreground { get; set; }
    public List<IntPtr> Focused { get; } = [];
    public bool RefuseFocus { get; set; }

    public IReadOnlyList<WindowRef> List() => Windows;

    public WindowRef? ForegroundExcludingSelf() => Foreground ?? Windows.FirstOrDefault();

    public string Focus(IntPtr hwnd)
    {
        if (RefuseFocus)
            throw new InvalidOperationException("Windows kept the window in the background (tried input-unlock)");
        Focused.Add(hwnd);
        return "input-unlock";
    }
}

internal sealed class FakeLauncher : IAppLauncher
{
    public List<string> Launched { get; } = [];
    public bool NotInstalled { get; set; }

    public int Launch(AllowedApp app)
    {
        if (NotInstalled)
            throw new FileNotFoundException($"{app.Label} doesn't seem to be installed");
        Launched.Add(app.Id);
        return 4242;
    }
}

internal sealed class FakeVolume(int level = 50, bool muted = false) : IVolume
{
    public int Level { get; private set; } = level;
    public (int Level, bool Muted) Get() => (Level, muted);
    public (int Level, bool Muted) Set(int level) => (Level = level, muted);
}

internal sealed class FakeReader : IUiReader
{
    public List<(IntPtr Hwnd, string Scope, UiLimits Limits)> Reads { get; } = [];

    public UiTree Read(IntPtr hwnd, string scope, UiLimits limits)
    {
        Reads.Add((hwnd, scope, limits));
        return UiTreeBuilder.Build(El.Make("Window", "Untitled - Notepad", children:
        [
            El.Make("Document", "", "RichEditD2DPT"),
            El.Make("Button", "Close"),
        ]), limits);
    }
}

/// <summary>A fake UI element for tree-building tests.</summary>
internal sealed class El(string type, string name, string id, bool password, IReadOnlyList<IUiElement> children,
                         Action? onChildren = null) : IUiElement
{
    public string Name => name;
    public string ControlType => type;
    public string AutomationId => id;
    public bool IsPassword => password;

    public IReadOnlyList<IUiElement> Children()
    {
        onChildren?.Invoke();
        return children;
    }

    public static El Make(string type, string name = "", string id = "", bool password = false,
                          IReadOnlyList<IUiElement>? children = null, Action? onChildren = null) =>
        new(type, name, id, password, children ?? [], onChildren);
}

internal static class Schemas
{
    public static SchemaValidator Validator { get; } = new();

    /// <summary>Wrap a handler result the way BbpClient sends it and validate it
    /// against protocol/topics/&lt;topic&gt;.response.schema.json.</summary>
    public static void AssertValidResponse(string topic, object result) =>
        Validator.Validate(Envelope.Create("response", topic, new RequestOk(true, result), corr: "req-1"));

    public static JsonObject Payload(object o) => (JsonObject)System.Text.Json.JsonSerializer.SerializeToNode(o, BbpJson.Options)!;
}
