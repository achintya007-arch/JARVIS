using System.Globalization;
using System.Text.Json;
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
public sealed record UiInvokeResult(string App, string Name, string ControlType, string AutomationId, string Pattern);
public sealed record UiSetTextResult(string App, string Name, string ControlType, string AutomationId, int Length);

/// <summary>
/// The Body's desktop.* request handlers. Every app-touching action checks the
/// Body's own allowlist first (E_NOT_ALLOWED, naming the app); nothing a
/// request carries is executed — launch takes an allowlist id, focus/read
/// resolve to a window whose owning app must be allowlisted.
///
/// desktop.uia.invoke / set_text (amendments 19–20) also need the app's
/// <c>act</c> flag, an allowed control type, exactly one matching element that
/// isn't a password field, disabled or read-only, and, for a risky element or
/// any typing, the confirm_id of an approval this Body saw for exactly that
/// request (ApprovalLedger). UIA patterns only: no mouse, no keys.
/// </summary>
public sealed class DesktopService(
    DesktopAllowlist allowlist,
    IWindowSystem windows,
    IAppLauncher launcher,
    IVolume volume,
    IUiReader reader,
    Action<string>? log = null,
    IUiActor? actor = null,
    ApprovalLedger? approvals = null,
    RiskyNames? risky = null)
{
    public const int MaxText = 2000;

    /// <summary>What desktop.uia.invoke may press, and desktop.uia.set_text may fill.</summary>
    public static readonly IReadOnlySet<string> InvokeTypes = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        { "Button", "MenuItem", "Hyperlink", "TabItem", "ListItem", "TreeItem", "CheckBox", "RadioButton" };
    public static readonly IReadOnlySet<string> TextTypes = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "Edit", "Document" };

    /// <summary>How long a press may take before the answer goes back anyway
    /// (an Invoke that opens a modal dialog can block until it closes).</summary>
    public static TimeSpan PressWait { get; set; } = TimeSpan.FromSeconds(3);

    /// <summary>How long to keep looking for a window or element that isn't
    /// there yet (an app that was just opened is still drawing).</summary>
    public TimeSpan FindWait { get; init; } = TimeSpan.FromSeconds(2);

    private static readonly TimeSpan FindPoll = TimeSpan.FromMilliseconds(250);

    private readonly RiskyNames _risky = risky ?? RiskyNames.FailClosed;

    /// <summary>hello capabilities this service provides (amendment 15).</summary>
    public static readonly string[] Capabilities = ["apps", "audio", "uia"];

    public void Register(BbpClient client)
    {
        client.HandleRequests("desktop.app.launch", (r, _) => Task.FromResult<object>(Launch(r.Payload)));
        client.HandleRequests("desktop.window.focus", (r, _) => Task.FromResult<object>(Focus(r.Payload)));
        client.HandleRequests("desktop.window.list", (_, _) => Task.FromResult<object>(List()));
        client.HandleRequests("desktop.audio.set_volume", (r, _) => Task.FromResult<object>(SetVolume(r.Payload)));
        client.HandleRequests("desktop.uia.read", (r, _) => Task.FromResult<object>(Read(r.Payload)));
        if (actor is null || approvals is null)
            return;
        client.HandleRequests("desktop.uia.invoke", async (r, ct) => await InvokeAsync(r.Payload, ct));
        client.HandleRequests("desktop.uia.set_text", async (r, ct) => await SetTextAsync(r.Payload, ct));
        // On the receive loop, in order: an approval is recorded before the request that uses it.
        client.EventReceived += approvals.Apply;
        client.Welcomed += approvals.ApplyWelcome;
        client.StateChanged += (state, _) =>
        {
            if (state != ConnectionState.Connected)
                approvals.Clear();
        };
    }

    public async Task<UiInvokeResult> InvokeAsync(JsonObject payload, CancellationToken ct = default)
    {
        var (app, target, element) = await FindTargetAsync(payload, InvokeTypes, "press", ct);
        if (_risky.IsRisky(target.Name, target.AutomationId, element.Name, element.AutomationId))
            await RequireApproval(payload, "desktop.uia.invoke", element, null, $"pressing '{Label(target, element)}'", ct);

        var press = Task.Run(target.Press, ct);
        string pattern;
        try
        {
            pattern = await press.WaitAsync(PressWait, ct);
        }
        catch (TimeoutException)
        {
            pattern = "invoke";                 // pressed; the app is still busy with it
        }
        catch (NotSupportedException e)
        {
            throw new BbpException("E_NOT_ALLOWED", $"'{Label(target, element)}' in {app.Label}: {e.Message}");
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            throw new BbpException("E_INTERNAL", $"pressing '{Label(target, element)}' failed: {e.Message}");
        }
        log?.Invoke($"desktop: pressed {app.Id} {target.ControlType} '{Label(target, element)}' ({pattern})");
        return new UiInvokeResult(app.Id, target.Name, target.ControlType, target.AutomationId, pattern);
    }

    public async Task<UiSetTextResult> SetTextAsync(JsonObject payload, CancellationToken ct = default)
    {
        var text = (string)payload["text"]!;
        if (UnsafeText(text) is { } problem)
            throw new BbpException("E_BAD_PAYLOAD", $"won't type that: {problem}");
        var (app, target, element) = await FindTargetAsync(payload, TextTypes, "type into", ct);
        if (target.IsReadOnly)
            throw new BbpException("E_NOT_ALLOWED", $"'{Label(target, element)}' in {app.Label} is read-only");
        // Setting a value replaces everything: fine for a field, not for a document you wrote.
        if (target.ControlType.Equals("Document", StringComparison.OrdinalIgnoreCase) && target.TextLength != 0)
            throw new BbpException("E_NOT_ALLOWED",
                $"'{Label(target, element)}' in {app.Label} isn't empty, and typing would replace what's there; " +
                "JARVIS only types into an empty document (open a new tab or window first)");
        await RequireApproval(payload, "desktop.uia.set_text", element, text, $"typing into '{Label(target, element)}'", ct);
        try
        {
            await Task.Run(() => target.SetText(text), ct).WaitAsync(PressWait, ct);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            throw new BbpException("E_INTERNAL", $"typing into '{Label(target, element)}' failed: {e.Message}");
        }
        log?.Invoke($"desktop: typed {text.Length} characters into {app.Id} {target.ControlType} '{Label(target, element)}'");
        return new UiSetTextResult(app.Id, target.Name, target.ControlType, target.AutomationId, text.Length);
    }

    /// <summary>Null when text may be typed: no control characters except tab
    /// and newline, no bidirectional or zero-width characters (so the dialog
    /// shows exactly what is typed), at most <see cref="MaxText"/> characters.</summary>
    public static string? UnsafeText(string text)
    {
        if (text.Length > MaxText)
            return $"it's {text.Length} characters; the limit is {MaxText}";
        foreach (var c in text)
        {
            if (c is '\t' or '\n')
                continue;
            if (c is (>= '\u202A' and <= '\u202E') or (>= '\u2066' and <= '\u2069') or '\u200E' or '\u200F' or '\u061C')
                return "it contains a right-to-left override character";
            var cat = CharUnicodeInfo.GetUnicodeCategory(c);
            if (cat == UnicodeCategory.Control)
                return "it contains a control character";
            if (cat == UnicodeCategory.Format)
                return "it contains an invisible formatting character";
        }
        return null;
    }

    private async Task<(AllowedApp App, IUiTarget Target, UiElementRef Element)> FindTargetAsync(
        JsonObject payload, IReadOnlySet<string> types, string verb, CancellationToken ct)
    {
        var id = (string)payload["app"]!;
        var app = allowlist.Find(id) ?? throw NotAllowed(id);
        if (!app.Act)
            throw new BbpException("E_NOT_ALLOWED",
                $"JARVIS may read {app.Label} but not click or type in it (\"act\": true in {DesktopAllowlist.FileName} allows it)");
        var element = payload["element"].Deserialize<UiElementRef>(BbpJson.Options)!;
        var label = element.Name ?? element.AutomationId ?? "";
        var kind = element.ControlType ?? "element";
        // Only a name is matched loosely (amendment 22); an automation id is exact or nothing.
        var loose = element.Name is { Length: > 0 } && element.AutomationId is null;
        IReadOnlyCollection<string> looseTypes = element.ControlType is { } t ? [t] : types;

        List<IntPtr> hwnds;
        IReadOnlyList<IUiTarget> found;
        IReadOnlyList<IUiTarget> candidates = [];
        UiMatcher.Rule? rule = null;
        var clock = System.Diagnostics.Stopwatch.StartNew();
        while (true)
        {
            hwnds = windows.List().Where(w => app.Matches(w.Process, w.Title)).Select(w => w.Hwnd).ToList();
            found = [];
            rule = null;
            if (hwnds.Count > 0)
            {
                try
                {
                    found = actor!.Find(hwnds, element);
                }
                catch (ArgumentException e)
                {
                    throw new BbpException("E_BAD_PAYLOAD", e.Message);
                }
                if (found.Count == 0 && loose)
                {
                    candidates = actor!.Candidates(hwnds, looseTypes);
                    if (UiMatcher.Relaxed(element.Name!, candidates) is { } hit)
                        (rule, found) = (hit.Rule, hit.Matches);
                }
                if (found.Count > 0)
                    break;
            }
            if (clock.Elapsed >= FindWait)
                break;
            await Task.Delay(FindPoll, ct);
        }

        if (hwnds.Count == 0)
            throw new BbpException("E_NOT_FOUND", $"{app.Label} has no open window");
        if (found.Count == 0)
        {
            var close = loose ? UiMatcher.Suggest(element.Name!, candidates) : [];
            throw new BbpException("E_NOT_FOUND", $"no {kind} '{label}' in {app.Label}" +
                (close.Count > 0 ? $"; did you mean {string.Join(" or ", close.Select(n => $"'{n}'"))}?" : ""));
        }
        if (found.Count > 1)
            found = Narrow(found, types);
        if (found.Count > 1)
            throw Ambiguous(found, label, app.Label);

        var target = found[0];
        if (rule is not null && _risky.IsRisky(target.Name, target.AutomationId))
            throw new BbpException("E_NOT_FOUND",
                $"nothing in {app.Label} is called exactly '{label}'; the closest is '{target.Name}', " +
                "which needs your approval, so say its full name");
        if (UiTreeBuilder.IsSecure(target))
            throw new BbpException("E_NOT_ALLOWED", $"'{Label(target, element)}' in {app.Label} is a password field");
        if (!types.Contains(target.ControlType))
            throw new BbpException("E_NOT_ALLOWED", $"JARVIS doesn't {verb} {target.ControlType} elements");
        if (!target.IsEnabled)
            throw new BbpException("E_NOT_ALLOWED", $"'{Label(target, element)}' in {app.Label} is disabled");
        return (app, target, element);
    }

    /// <summary>Several elements match: keep only those that could be meant
    /// (of a type JARVIS acts on, then enabled, then on screen) as long as that
    /// leaves at least one. Stops as soon as one is left.</summary>
    private static IReadOnlyList<IUiTarget> Narrow(IReadOnlyList<IUiTarget> found, IReadOnlySet<string> types)
    {
        Func<IUiTarget, bool>[] filters = [f => types.Contains(f.ControlType), f => f.IsEnabled, f => !f.IsOffscreen];
        foreach (var keep in filters)
        {
            var kept = found.Where(keep).ToList();
            if (kept.Count > 0)
                found = kept;
            if (found.Count == 1)
                break;
        }
        return found;
    }

    /// <summary>An automation id you could say: not empty and not just digits
    /// (WinForms numbers unnamed controls by window handle, new every run).</summary>
    private static bool Meaningful(string automationId) =>
        automationId.Length > 0 && !automationId.All(char.IsAsciiDigit);

    private static BbpException Ambiguous(IReadOnlyList<IUiTarget> found, string label, string app)
    {
        var count = found.Count >= 10 ? "10 or more" : found.Count.ToString(CultureInfo.InvariantCulture);
        var shown = found.Take(4).Select(f =>
            $"'{f.Name}' ({f.ControlType}{(Meaningful(f.AutomationId) ? $", automation id {f.AutomationId}" : "")})");
        var distinct = found.Select(f => (f.Name.ToLowerInvariant(), f.ControlType, Meaningful(f.AutomationId) ? f.AutomationId : ""))
            .Distinct().Count() == found.Count;
        return new BbpException("E_NOT_FOUND",
            $"{count} elements match '{label}' in {app}: {string.Join(", ", shown)}{(found.Count > 4 ? ", …" : "")}; " +
            (distinct ? "say which one (its full name, kind or automation id)"
                      : "they look the same from here, so I won't guess which"));
    }

    private async Task RequireApproval(JsonObject payload, string topic, UiElementRef element, string? text,
                                       string what, CancellationToken ct)
    {
        var confirmId = payload["confirm_id"] is JsonValue v ? (string?)v : null;
        var action = new ConfirmAction(topic, (string)payload["app"]!, element, text);
        var refusal = await approvals!.CheckAsync(confirmId, action, ct);
        if (refusal is not null)
            throw new BbpException("E_DENIED", $"{what} needs your approval: {refusal}");
    }

    private static string Label(IUiTarget target, UiElementRef element) =>
        target.Name.Length > 0 ? target.Name : element.Name ?? element.AutomationId ?? target.AutomationId;

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
