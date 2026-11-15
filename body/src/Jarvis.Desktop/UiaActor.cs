using Bbp.Client;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace Jarvis.Desktop;

/// <summary>One element desktop.uia.invoke / set_text may act on.</summary>
public interface IUiTarget : IUiElement
{
    bool IsEnabled { get; }

    /// <summary>True when the text can't be set (no Value pattern, or read-only).</summary>
    bool IsReadOnly { get; }

    /// <summary>Press it through a UIA pattern: "invoke", "toggle" or "select".
    /// Throws NotSupportedException when it has none of them (no mouse fallback).</summary>
    string Press();

    void SetText(string text);
}

public interface IUiActor
{
    /// <summary>Elements in these windows (and their descendants) matching the
    /// query: name case-insensitive and exact, automation id exact, control type
    /// exact. At most <paramref name="max"/>. Throws ArgumentException for an
    /// unknown control type.</summary>
    IReadOnlyList<IUiTarget> Find(IReadOnlyList<IntPtr> windows, UiElementRef query, int max = 10);
}

/// <summary>UI Automation through FlaUI (UIA3): patterns only. Nothing here moves
/// the mouse, sends keys or changes focus.</summary>
public sealed class FlaUiActor : IUiActor, IDisposable
{
    private readonly UIA3Automation _automation = new();

    public IReadOnlyList<IUiTarget> Find(IReadOnlyList<IntPtr> windows, UiElementRef query, int max = 10)
    {
        var cf = _automation.ConditionFactory;
        var conditions = new List<ConditionBase> { new PropertyCondition(_automation.PropertyLibrary.Element.IsControlElement, true) };
        if (!string.IsNullOrEmpty(query.ControlType))
        {
            if (!Enum.TryParse<ControlType>(query.ControlType, true, out var type) || type == ControlType.Unknown)
                throw new ArgumentException($"'{query.ControlType}' isn't a UI Automation control type");
            conditions.Add(cf.ByControlType(type));
        }
        if (!string.IsNullOrEmpty(query.Name))
            conditions.Add(cf.ByName(query.Name, PropertyConditionFlags.IgnoreCase));
        if (!string.IsNullOrEmpty(query.AutomationId))
            conditions.Add(cf.ByAutomationId(query.AutomationId));
        var condition = new AndCondition(conditions.ToArray());

        var found = new List<AutomationElement>();
        foreach (var hwnd in windows)
        {
            AutomationElement window;
            try
            {
                window = _automation.FromHandle(hwnd);
            }
            catch (Exception)
            {
                continue;                       // window closed meanwhile
            }
            foreach (var e in window.FindAllDescendants(condition))
            {
                // Hosted apps (UWP under ApplicationFrameHost) can show the same
                // element under two top-level windows: count it once.
                if (found.Any(f => f.Equals(e)))
                    continue;
                found.Add(e);
                if (found.Count >= max)
                    return found.Select(f => (IUiTarget)new Target(f)).ToList();
            }
        }
        return found.Select(f => (IUiTarget)new Target(f)).ToList();
    }

    public void Dispose() => _automation.Dispose();

    private sealed class Target(AutomationElement e) : IUiTarget
    {
        public string Name => Safe(() => e.Properties.Name.ValueOrDefault) ?? "";
        public string ControlType => Safe(() => e.Properties.ControlType.ValueOrDefault.ToString()) ?? "Unknown";
        public string AutomationId => Safe(() => e.Properties.AutomationId.ValueOrDefault) ?? "";

        // Fail closed: unreadable means "password", "disabled", "read-only".
        public bool IsPassword => Safe(() => (bool?)e.Properties.IsPassword.ValueOrDefault) ?? true;
        public bool IsEnabled => Safe(() => (bool?)e.Properties.IsEnabled.ValueOrDefault) ?? false;
        public bool IsReadOnly =>
            Safe(() => (bool?)(!e.Patterns.Value.IsSupported || e.Patterns.Value.Pattern.IsReadOnly.ValueOrDefault)) ?? true;

        public IReadOnlyList<IUiElement> Children() => [];

        /// <summary>What a click means for this control type: a checkbox toggles;
        /// a list, tree or tab item or a radio button is selected (Invoke on a
        /// list item would open it, e.g. a file in Explorer); anything else is invoked.</summary>
        public string Press()
        {
            string[] order = ControlType switch
            {
                "CheckBox" => ["toggle", "invoke"],
                "ListItem" or "TreeItem" or "TabItem" or "RadioButton" => ["select", "toggle"],
                _ => ["invoke", "toggle", "select"],
            };
            foreach (var pattern in order)
            {
                switch (pattern)
                {
                    case "invoke" when e.Patterns.Invoke.IsSupported:
                        e.Patterns.Invoke.Pattern.Invoke();
                        return pattern;
                    case "toggle" when e.Patterns.Toggle.IsSupported:
                        e.Patterns.Toggle.Pattern.Toggle();
                        return pattern;
                    case "select" when e.Patterns.SelectionItem.IsSupported:
                        e.Patterns.SelectionItem.Pattern.Select();
                        return pattern;
                }
            }
            throw new NotSupportedException("it can't be pressed without moving the mouse");
        }

        public void SetText(string text) => e.Patterns.Value.Pattern.SetValue(text);

        private static T? Safe<T>(Func<T> read)
        {
            try
            {
                return read();
            }
            catch (Exception)
            {
                return default;
            }
        }
    }
}
