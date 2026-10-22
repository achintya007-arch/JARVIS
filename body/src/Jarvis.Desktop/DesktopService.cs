using System.Text.Json.Nodes;
using Bbp.Client;

namespace Jarvis.Desktop;

// Results (serialized snake_case by Bbp.Client; shapes = protocol/topics/*.response.schema.json)
public sealed record LaunchResult(int Pid, string AppId);
public sealed record FocusResult(long Hwnd, string Title, string App, string Strategy);
public sealed record WindowInfo(string Title, string App, long Hwnd, bool Allowed);
public sealed record WindowListResult(IReadOnlyList<WindowInfo> Windows);
public sealed record VolumeResult(int Level, bool Muted);
public sealed record UiReadResult(string App, string Title, UiNode Root, int NodeCount, bool Truncated);

/// <summary>
/// The Body's desktop.* request handlers. Every app-touching action checks the
/// Body's own allowlist first (E_NOT_ALLOWED, naming the app); nothing a
/// request carries is executed — launch takes an allowlist id, focus/read
/// resolve to a window whose owning app must be allowlisted.
/// </summary>
public sealed class DesktopService(
    DesktopAllowlist allowlist,
    IWindowSystem windows,
    IAppLauncher launcher,
    IVolume volume,
    IUiReader reader,
    Action<string>? log = null)
{
    /// <summary>hello capabilities this service provides (amendment 15).</summary>
    public static readonly string[] Capabilities = ["apps", "audio", "uia"];

    public void Register(BbpClient client)
    {
        client.HandleRequests("desktop.app.launch", (r, _) => Task.FromResult<object>(Launch(r.Payload)));
        client.HandleRequests("desktop.window.focus", (r, _) => Task.FromResult<object>(Focus(r.Payload)));
        client.HandleRequests("desktop.window.list", (_, _) => Task.FromResult<object>(List()));
        client.HandleRequests("desktop.audio.set_volume", (r, _) => Task.FromResult<object>(SetVolume(r.Payload)));
        client.HandleRequests("desktop.uia.read", (r, _) => Task.FromResult<object>(Read(r.Payload)));
    }

    public LaunchResult Launch(JsonObject payload)
    {
        var id = (string)payload["app_id"]!;
        var app = allowlist.Find(id) ?? throw NotAllowed(id);
        try
        {
            var pid = launcher.Launch(app);
            log?.Invoke($"desktop: launched {app.Id} (pid {pid})");
            return new LaunchResult(pid, app.Id);
        }
        catch (FileNotFoundException e)
        {
            throw new BbpException("E_NOT_FOUND", e.Message);
        }
    }

    public FocusResult Focus(JsonObject payload)
    {
        WindowRef target;
        AllowedApp app;
        if (payload["app"] is JsonValue appValue)
        {
            var id = (string)appValue!;
            app = allowlist.Find(id) ?? throw NotAllowed(id);
            target = windows.List().FirstOrDefault(w => app.Matches(w.Process, w.Title))
                     ?? throw new BbpException("E_NOT_FOUND", $"{app.Label} has no open window");
        }
        else
        {
            var text = (string)payload["title_contains"]!;
            var matches = windows.List().Where(w => w.Title.Contains(text, StringComparison.OrdinalIgnoreCase)).ToList();
            if (matches.Count == 0)
                throw new BbpException("E_NOT_FOUND", $"no window title contains '{text}'");
            var allowed = matches.Select(w => (Window: w, App: allowlist.ForWindow(w.Process, w.Title)))
                                 .FirstOrDefault(m => m.App is not null);
            if (allowed.App is null)
                throw NotAllowed(AppName(matches[0]));
            (target, app) = (allowed.Window, allowed.App);
        }

        string strategy;
        try
        {
            strategy = windows.Focus(target.Hwnd);
        }
        catch (InvalidOperationException e)
        {
            throw new BbpException("E_INTERNAL", $"couldn't bring {app.Label} to the front: {e.Message}");
        }
        log?.Invoke($"desktop: focused {app.Id} via {strategy}");
        return new FocusResult(target.Hwnd.ToInt64(), target.Title, app.Id, strategy);
    }

    public WindowListResult List() =>
        new(windows.List()
            .Select(w => (Window: w, App: allowlist.ForWindow(w.Process, w.Title)))
            .Select(m => new WindowInfo(m.Window.Title, m.App?.Id ?? AppName(m.Window), m.Window.Hwnd.ToInt64(), m.App is not null))
            .ToList());

    public VolumeResult SetVolume(JsonObject payload)
    {
        int target = payload["level"] is JsonValue level
            ? (int)level
            : CoreAudioVolume.Apply(volume.Get().Level, (int)payload["delta"]!);
        var (applied, muted) = volume.Set(target);
        log?.Invoke($"desktop: volume {applied}%{(muted ? " (muted)" : "")}");
        return new VolumeResult(applied, muted);
    }

    public UiReadResult Read(JsonObject payload)
    {
        WindowRef target;
        AllowedApp app;
        if (payload["app"] is JsonValue appValue)
        {
            var id = (string)appValue!;
            app = allowlist.Find(id) ?? throw NotAllowed(id);
            target = windows.List().FirstOrDefault(w => app.Matches(w.Process, w.Title))
                     ?? throw new BbpException("E_NOT_FOUND", $"{app.Label} has no open window");
        }
        else
        {
            target = windows.ForegroundExcludingSelf()
                     ?? throw new BbpException("E_NOT_FOUND", "no window is in front");
            app = allowlist.ForWindow(target.Process, target.Title) ?? throw NotAllowed(AppName(target));
        }

        var limits = new UiLimits(
            MaxDepth: payload["max_depth"] is JsonValue d ? (int)d : 12,
            MaxNodes: payload["max_nodes"] is JsonValue n ? (int)n : 300);
        var scope = payload["scope"] is JsonValue s ? (string)s! : "window";
        UiTree tree;
        try
        {
            tree = reader.Read(target.Hwnd, scope, limits);
        }
        catch (InvalidOperationException e)
        {
            throw new BbpException("E_NOT_FOUND", e.Message);
        }
        log?.Invoke($"desktop: read {app.Id} ({tree.NodeCount} nodes{(tree.Truncated ? ", truncated" : "")})");
        return new UiReadResult(app.Id, target.Title, tree.Root, tree.NodeCount, tree.Truncated);
    }

    private static BbpException NotAllowed(string app) =>
        new("E_NOT_ALLOWED", $"{app} isn't on the Body's allowlist");

    private static string AppName(WindowRef w) =>
        w.Process.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? w.Process[..^4] : w.Process;
}
