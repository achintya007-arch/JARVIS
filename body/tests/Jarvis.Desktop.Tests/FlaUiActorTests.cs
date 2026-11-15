using System.Windows.Forms;
using Bbp.Client;

namespace Jarvis.Desktop.Tests;

/// <summary>
/// FlaUiActor against a real window: a small WinForms form the test owns,
/// opened off-screen and never activated, so nothing on your desktop is touched.
/// Proves the patterns work without the mouse or keyboard: Invoke on a button,
/// Toggle on a checkbox, Value on a text box; and that password boxes and
/// read-only fields report themselves.
/// </summary>
public sealed class FlaUiActorTests : IDisposable
{
    private sealed class TestForm : Form
    {
        public int SevenClicks { get; private set; }
        public readonly TextBox Search = new() { Name = "searchBox", AccessibleName = "Search", Top = 40 };
        public readonly TextBox Pin = new() { Name = "pinBox", AccessibleName = "Secret", UseSystemPasswordChar = true, Top = 70 };
        public readonly TextBox ShownBox = new() { Name = "shownBox", AccessibleName = "Shown", ReadOnly = true, Top = 100 };
        public readonly CheckBox Bold = new() { Text = "Bold", Top = 130 };

        public TestForm()
        {
            Text = "JARVIS FlaUiActor test";
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            Location = new System.Drawing.Point(-32000, -32000);
            var seven = new Button { Text = "Seven", Name = "num7Button" };
            seven.Click += (_, _) => SevenClicks++;          // on the form's own thread
            Controls.AddRange([seven, new Button { Text = "Off", Enabled = false, Left = 90 }, Search, Pin, ShownBox, Bold]);
        }

        protected override bool ShowWithoutActivation => true;
    }

    private readonly TestForm _form;
    private readonly Thread _ui;
    private readonly FlaUiActor _actor = new();
    private readonly IntPtr _hwnd;

    public FlaUiActorTests()
    {
        TestForm? form = null;
        using var ready = new ManualResetEventSlim();
        _ui = new Thread(() =>
        {
            form = new TestForm();
            form.Shown += (_, _) => ready.Set();
            Application.Run(form);
        }) { IsBackground = true };
        _ui.SetApartmentState(ApartmentState.STA);
        _ui.Start();
        Assert.True(ready.Wait(TimeSpan.FromSeconds(10)), "test window didn't open");
        _form = form!;
        _hwnd = (IntPtr)_form.Invoke(() => _form.Handle);
    }

    public void Dispose()
    {
        _form.BeginInvoke(() => _form.Close());
        _ui.Join(TimeSpan.FromSeconds(5));
        _actor.Dispose();
    }

    private int Clicks() => (int)_form.Invoke(() => _form.SevenClicks);

    private IUiTarget One(UiElementRef query) => Assert.Single(_actor.Find([_hwnd], query));

    [Fact]
    public void Invoke_PressesTheButton_ByNameCaseInsensitive()
    {
        var seven = One(new UiElementRef("seven", "Button"));
        Assert.Equal(("Seven", "Button", true), (seven.Name, seven.ControlType, seven.IsEnabled));
        Assert.Equal("invoke", seven.Press());
        SpinWait.SpinUntil(() => Clicks() == 1, TimeSpan.FromSeconds(5));
        Assert.Equal(1, Clicks());
    }

    [Fact]
    public void ByAutomationId_AndDisabled()
    {
        Assert.Equal("Seven", One(new UiElementRef(AutomationId: "num7Button")).Name);
        Assert.False(One(new UiElementRef("Off")).IsEnabled);
        Assert.Empty(_actor.Find([_hwnd], new UiElementRef("Nine")));
        Assert.Throws<ArgumentException>(() => _actor.Find([_hwnd], new UiElementRef("Seven", "Bogus")));
    }

    [Fact]
    public void Toggle_TheCheckbox()
    {
        Assert.Equal("toggle", One(new UiElementRef("Bold", "CheckBox")).Press());
        SpinWait.SpinUntil(() => (bool)_form.Invoke(() => _form.Bold.Checked), TimeSpan.FromSeconds(5));
        Assert.True((bool)_form.Invoke(() => _form.Bold.Checked));
    }

    [Fact]
    public void SetText_ThroughTheValuePattern()
    {
        var search = One(new UiElementRef("Search", "Edit"));
        Assert.False(search.IsReadOnly);
        Assert.False(search.IsPassword);
        search.SetText("hello world");
        Assert.Equal("hello world", (string)_form.Invoke(() => _form.Search.Text));
    }

    [Fact]
    public void Candidates_AreTheNamedElementsOfThoseTypes()
    {
        var buttons = _actor.Candidates([_hwnd], ["Button"]).Select(c => c.Name).Order().ToList();
        Assert.Equal(["Close", "Maximize", "Minimize", "Off", "Seven"], buttons);   // the title bar's too
        var mixed = _actor.Candidates([_hwnd], ["Button", "CheckBox", "Edit", "Nonsense"]).Select(c => c.ControlType).ToHashSet();
        Assert.Equal(["Button", "CheckBox", "Edit"], mixed.Order());
        Assert.Empty(_actor.Candidates([_hwnd], ["Nonsense"]));
        Assert.Equal(0, One(new UiElementRef("Search", "Edit")).TextLength);
        Assert.Equal(-1, One(new UiElementRef("Seven", "Button")).TextLength);   // no text to read
    }

    [Fact]
    public void PasswordAndReadOnlyFields_SayWhatTheyAre()
    {
        Assert.True(UiTreeBuilder.IsSecure(One(new UiElementRef("Secret", "Edit"))));
        Assert.True(One(new UiElementRef("Shown", "Edit")).IsReadOnly);
    }
}
