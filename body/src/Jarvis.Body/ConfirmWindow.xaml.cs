using System.Windows;
using System.Windows.Input;
using Bbp.Client;

namespace Jarvis.Body;

/// <summary>An approve/deny dialog for one confirm.request. Always on top,
/// never takes focus by itself; App closes it on confirm.resolved, at the
/// deadline, or when the link to the Brain drops.</summary>
public partial class ConfirmWindow : Window
{
    private const double TopGap = 90;
    private static int _openCount;
    private readonly int _slot;

    public ConfirmWindow(ConfirmDialogModel model)
    {
        InitializeComponent();
        Model = model;
        DataContext = model;
        _slot = _openCount++;
        Closed += (_, _) => _openCount = Math.Max(0, _openCount - 1);
    }

    public ConfirmDialogModel Model { get; }

    /// <summary>(the payload to send). Raised once per click that's allowed to answer.</summary>
    public event Action<ConfirmResponsePayload>? Answered;

    private void OnApprove(object sender, RoutedEventArgs e) => Send(true);

    private void OnDeny(object sender, RoutedEventArgs e) => Send(false);

    private void Send(bool approved)
    {
        if (Model.Answer(approved) is { } payload)
            Answered?.Invoke(payload);
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        // Top centre of the screen, stacked if several are open.
        var area = SystemParameters.WorkArea;
        Left = area.Left + (area.Width - ActualWidth) / 2;
        Top = area.Top + TopGap + _slot * 24;
    }

    private void OnDrag(object sender, MouseButtonEventArgs e)
    {
        if (e.ButtonState == MouseButtonState.Pressed && e.OriginalSource is not System.Windows.Controls.Button)
            DragMove();
    }
}
