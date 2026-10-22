using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace Jarvis.Desktop;

public interface IUiReader
{
    /// <summary>Read the trimmed tree of a window ("window") or of the focused
    /// element inside it ("focused"). Throws InvalidOperationException if the
    /// focused element isn't in that window.</summary>
    UiTree Read(IntPtr hwnd, string scope, UiLimits limits);
}

/// <summary>UI Automation through FlaUI (UIA3). Read-only: nothing here invokes,
/// types or changes focus.
///
/// Speed: every UIA property read is a cross-process call, so the reader
/// activates a CacheRequest — each FindAll(Children) returns the children
/// together with Name / ControlType / AutomationId / IsPassword in one round
/// trip, and later property reads hit the cache.</summary>
public sealed class FlaUiReader : IUiReader, IDisposable
{
    private readonly UIA3Automation _automation = new();
    private readonly PropertyCondition _controlView;
    private readonly CacheRequest _cache;

    public FlaUiReader()
    {
        var p = _automation.PropertyLibrary.Element;
        _controlView = new PropertyCondition(p.IsControlElement, true);
        // Full mode: cached elements stay live, so FindAll works on them too
        // (FlaUI's default, None, returns detached elements → E_FAIL one level down).
        _cache = new CacheRequest
        {
            TreeScope = TreeScope.Element,
            TreeFilter = _controlView,
            AutomationElementMode = AutomationElementMode.Full,
        };
        _cache.Add(p.Name);
        _cache.Add(p.ControlType);
        _cache.Add(p.AutomationId);
        _cache.Add(p.IsPassword);
        _cache.Add(p.ProcessId);
    }

    public UiTree Read(IntPtr hwnd, string scope, UiLimits limits)
    {
        using (_cache.Activate())             // thread-local: covers the whole synchronous walk
        {
            var window = _automation.FromHandle(hwnd);
            var root = window;
            if (scope == "focused")
            {
                var focused = _automation.FocusedElement();
                if (focused is null || focused.Properties.ProcessId.ValueOrDefault != window.Properties.ProcessId.ValueOrDefault)
                    throw new InvalidOperationException("the focused element isn't in that app's window");
                root = focused;
            }
            return UiTreeBuilder.Build(new Element(root, _controlView), limits);
        }
    }

    public void Dispose() => _automation.Dispose();

    private sealed class Element(AutomationElement element, PropertyCondition controlView) : IUiElement
    {
        public string Name => Safe(() => element.Properties.Name.ValueOrDefault) ?? "";
        public string ControlType => Safe(() => element.Properties.ControlType.ValueOrDefault.ToString()) ?? "Unknown";
        public string AutomationId => Safe(() => element.Properties.AutomationId.ValueOrDefault) ?? "";

        // Fail closed: an edit field whose IsPassword can't be read is treated as one.
        public bool IsPassword => Safe(() => (bool?)element.Properties.IsPassword.ValueOrDefault) ?? ControlType == "Edit";

        public IReadOnlyList<IUiElement> Children() =>
            element.FindAll(TreeScope.Children, controlView)
                   .Select(c => (IUiElement)new Element(c, controlView)).ToList();

        private static T? Safe<T>(Func<T> read)
        {
            try
            {
                return read();
            }
            catch (Exception)
            {
                return default;               // element went away or doesn't support it
            }
        }
    }
}
