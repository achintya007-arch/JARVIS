using System.Diagnostics;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace Jarvis.Desktop;

/// <summary>One element of an app's UI, as the tree builder needs it.</summary>
public interface IUiElement
{
    string Name { get; }
    string ControlType { get; }
    string AutomationId { get; }
    bool IsPassword { get; }
    IReadOnlyList<IUiElement> Children();
}

public sealed record UiLimits(int MaxDepth = 12, int MaxNodes = 300, int MaxNameLength = 200, int BudgetMs = 2500);

/// <summary>A node of the trimmed tree sent to the Brain (empty fields omitted).</summary>
public sealed class UiNode(string controlType, string? name, string? automationId)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Name { get; } = name;

    public string ControlType { get; } = controlType;

    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? AutomationId { get; } = automationId;

    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public List<UiNode>? Children { get; private set; }

    internal void Add(UiNode child) => (Children ??= []).Add(child);
}

public sealed record UiTree(UiNode Root, int NodeCount, bool Truncated);

/// <summary>
/// Builds the trimmed, bounded tree for desktop.uia.read:
///   - password / secure fields are dropped together with their subtrees,
///     always (nothing typed into them can reach the Brain);
///   - unnamed containers (no name, no automation id) are flattened away and
///     empty leaves dropped, so the tree is mostly text a person would see;
///   - depth, node count, name length and wall-clock time are capped, and
///     <c>Truncated</c> says when a cap cut something off.
/// </summary>
public static partial class UiTreeBuilder
{
    private const int MaxRawDepth = 64;       // guards flattening on pathological trees

    [GeneratedRegex(@"pass(word|code|phrase)?|pwd|\bpin\b|cvv|cvc|otp|one.time|secret|security code",
                    RegexOptions.IgnoreCase)]
    private static partial Regex SecureLabel();

    public static UiTree Build(IUiElement root, UiLimits limits)
    {
        var state = new State(limits);
        var node = new UiNode(root.ControlType, state.Clean(root.Name), Blank(root.AutomationId));
        state.Count = 1;
        Expand(root, node, 1, 0, state);
        return new UiTree(node, state.Count, state.Truncated);
    }

    /// <summary>True for fields whose content must never leave the Body.</summary>
    public static bool IsSecure(IUiElement e) =>
        e.IsPassword
        || (e.ControlType is "Edit" or "Document" or "Custom"
            && (SecureLabel().IsMatch(e.Name) || SecureLabel().IsMatch(e.AutomationId)));

    private static void Expand(IUiElement element, UiNode into, int depth, int rawDepth, State s)
    {
        if (rawDepth >= MaxRawDepth)
        {
            s.Truncated = true;
            return;
        }
        IReadOnlyList<IUiElement> children;
        try
        {
            children = element.Children();
        }
        catch (Exception)
        {
            return;                           // element vanished while we read it
        }
        foreach (var child in children)
        {
            if (s.Count >= s.Limits.MaxNodes || s.Clock.ElapsedMilliseconds > s.Limits.BudgetMs)
            {
                s.Truncated = true;
                return;
            }
            if (IsSecure(child))
                continue;
            var name = s.Clean(child.Name);
            var id = Blank(child.AutomationId);
            if (name is null && id is null)
            {
                Expand(child, into, depth, rawDepth + 1, s);     // flatten an unnamed container
                continue;
            }
            if (depth > s.Limits.MaxDepth)
            {
                s.Truncated = true;
                return;
            }
            var node = new UiNode(child.ControlType, name, id);
            s.Count++;
            into.Add(node);
            Expand(child, node, depth + 1, rawDepth + 1, s);
        }
    }

    private static string? Blank(string? value) => string.IsNullOrWhiteSpace(value) ? null : value.Trim();

    private sealed class State(UiLimits limits)
    {
        public UiLimits Limits { get; } = limits;
        public Stopwatch Clock { get; } = Stopwatch.StartNew();
        public int Count;
        public bool Truncated;

        public string? Clean(string? name)
        {
            if (string.IsNullOrWhiteSpace(name))
                return null;
            var collapsed = string.Join(' ', name.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
            return collapsed.Length <= Limits.MaxNameLength
                ? collapsed
                : collapsed[..(Limits.MaxNameLength - 1)] + "…";
        }
    }
}
