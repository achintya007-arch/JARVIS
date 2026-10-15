using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;

namespace Jarvis.Body;

/// <summary>Transparent, always-on-top HUD. Drag to move; type and press Enter
/// to talk to JARVIS; right-click to hide or quit. Also the window that
/// receives the global hotkey's WM_HOTKEY.</summary>
public partial class MainWindow : Window
{
    private const double EdgeGap = 16;

    public MainWindow() => InitializeComponent();

    /// <summary>Enter in the text box: (text) — App sends it as input.text.</summary>
    public event Action<string>? TextSubmitted;

    /// <summary>Raw window messages, for the global hotkey.</summary>
    public event Func<int, IntPtr, bool>? WindowMessage;

    public IntPtr Handle => new WindowInteropHelper(this).Handle;

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        HwndSource.FromHwnd(Handle)?.AddHook(WndProc);
    }

    private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (WindowMessage?.Invoke(msg, wParam) == true)
            handled = true;
        return IntPtr.Zero;
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        var area = SystemParameters.WorkArea;
        Left = area.Right - ActualWidth - EdgeGap;
        Top = area.Top + EdgeGap;
    }

    private void OnDrag(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed && e.OriginalSource is not System.Windows.Controls.TextBox)
            DragMove();
    }

    private void OnInputKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Escape)
        {
            Input.Clear();
            e.Handled = true;
        }
        else if (e.Key == Key.Enter)
        {
            var text = Input.Text.Trim();
            if (text.Length > 0)
            {
                TextSubmitted?.Invoke(text);
                Input.Clear();
            }
            e.Handled = true;
        }
    }

    private void OnNoticeClick(object sender, MouseButtonEventArgs e)
    {
        (DataContext as HudViewModel)?.ClearNotice();
        e.Handled = true;
    }

    private void OnHide(object sender, RoutedEventArgs e) => Hide();

    private void OnQuit(object sender, RoutedEventArgs e) => Application.Current.Shutdown();
}
