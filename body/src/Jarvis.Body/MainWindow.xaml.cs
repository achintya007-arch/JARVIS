using System.Windows;
using System.Windows.Input;

namespace Jarvis.Body;

/// <summary>Transparent, always-on-top HUD. Drag to move; right-click to quit
/// (a tray icon arrives in milestone 2).</summary>
public partial class MainWindow : Window
{
    private const double EdgeGap = 16;

    public MainWindow() => InitializeComponent();

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        var area = SystemParameters.WorkArea;
        Left = area.Right - ActualWidth - EdgeGap;
        Top = area.Top + EdgeGap;
    }

    private void OnDrag(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed)
            DragMove();
    }

    private void OnQuit(object sender, RoutedEventArgs e) => Close();
}
