using System.Text.Json;
using System.Text.Json.Serialization;
using Bbp.Client;

namespace Jarvis.Desktop;

/// <summary>
/// The Body's own allowlist (desktop.allowlist.json next to Jarvis.Body.exe):
/// the only apps it will launch, focus or read, whatever the Brain asks.
/// Enforced here independently of the Brain's allowlist, so a compromised or
/// confused Brain still can't drive arbitrary software. A missing or broken
/// file means an EMPTY allowlist (fail closed), never "allow everything".
/// </summary>
public sealed class DesktopAllowlist
{
    public const string FileName = "desktop.allowlist.json";

    private readonly Dictionary<string, AllowedApp> _byAlias = new(StringComparer.OrdinalIgnoreCase);

    public DesktopAllowlist(IEnumerable<AllowedApp> apps)
    {
        Apps = apps.ToList();
        foreach (var app in Apps)
        {
            _byAlias[app.Id] = app;
            foreach (var alias in app.Aliases ?? [])
                _byAlias[alias] = app;
        }
    }

    public IReadOnlyList<AllowedApp> Apps { get; }

    public static DesktopAllowlist Empty { get; } = new([]);

    /// <summary>Load from a file; (allowlist, problem) — problem is null when fine.</summary>
    public static (DesktopAllowlist List, string? Problem) Load(string path)
    {
        if (!File.Exists(path))
            return (Empty, $"{FileName} not found; desktop control is off");
        try
        {
            var file = JsonSerializer.Deserialize<AllowlistFile>(File.ReadAllText(path), BbpJson.Options);
            var apps = file?.Apps?.Where(a => !string.IsNullOrWhiteSpace(a.Id)).ToList() ?? [];
            return (new DesktopAllowlist(apps), apps.Count == 0 ? $"{FileName} lists no apps" : null);
        }
        catch (JsonException e)
        {
            return (Empty, $"{FileName} is invalid ({e.Message}); desktop control is off");
        }
    }

    /// <summary>The app with this id or alias, or null.</summary>
    public AllowedApp? Find(string idOrAlias) =>
        _byAlias.TryGetValue(idOrAlias.Trim(), out var app) ? app : null;

    /// <summary>Which allowed app owns a window (by process, optionally title), or null.</summary>
    public AllowedApp? ForWindow(string processName, string title) =>
        Apps.FirstOrDefault(a => a.Matches(processName, title));

    private sealed record AllowlistFile(List<AllowedApp>? Apps);
}

/// <summary>One allowlisted app.</summary>
/// <param name="Id">Stable id the Brain uses (matches the Brain's allowlist keys).</param>
/// <param name="Exe">Executable name, resolved via Candidates, PATH and App Paths.</param>
/// <param name="Processes">Process image names whose windows belong to this app.</param>
/// <param name="Titles">Optional: the window title must contain one of these
/// (for hosted apps, e.g. UWP windows owned by ApplicationFrameHost.exe).</param>
/// <param name="Act">JARVIS may also press elements and type into fields of
/// this app (desktop.uia.invoke / set_text, amendment 19). Off unless set.</param>
public sealed record AllowedApp(
    string Id,
    string Display,
    string? Exe = null,
    IReadOnlyList<string>? Args = null,
    IReadOnlyList<string>? Candidates = null,
    string? Uri = null,
    IReadOnlyList<string>? Processes = null,
    IReadOnlyList<string>? Titles = null,
    IReadOnlyList<string>? Aliases = null,
    bool Act = false)
{
    [JsonIgnore]
    public string Label => string.IsNullOrEmpty(Display) ? Id : Display;

    public bool Matches(string processName, string title)
    {
        if (Processes is null || !Processes.Any(p => p.Equals(processName, StringComparison.OrdinalIgnoreCase)))
            return false;
        return Titles is null || Titles.Count == 0
            || Titles.Any(t => title.Contains(t, StringComparison.OrdinalIgnoreCase));
    }
}
