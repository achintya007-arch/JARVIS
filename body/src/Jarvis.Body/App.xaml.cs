using System.Diagnostics;
using System.IO;
using System.Windows;
using Bbp.Client;

namespace Jarvis.Body;

public partial class App : Application
{
    private readonly CancellationTokenSource _cts = new();
    private Task? _clientTask;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var settings = BodySettings.Load();
        var vm = new HudViewModel();

        SchemaValidator validator;
        try
        {
            validator = new SchemaValidator();
        }
        catch (FileNotFoundException ex)
        {
            MessageBox.Show(ex.Message, "JARVIS Body", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
            return;
        }

        var client = new BbpClient(new BbpClientOptions
        {
            Url = new Uri(settings.Url),
            TokenProvider = settings.ReadToken,
            ClientName = "win-shell",
            ClientVersion = "0.1.0",
            Capabilities = ["hud"],
        }, validator);

        // Client events arrive on worker threads; the view model lives on the UI thread.
        client.StateChanged += (state, detail) => Dispatcher.BeginInvoke(() => vm.OnConnection(state, detail));
        client.Welcomed += w => Dispatcher.BeginInvoke(() => vm.OnWelcome(w));
        client.EventReceived += m => Dispatcher.BeginInvoke(() => vm.OnEvent(m));
        client.ProtocolError += err => Debug.WriteLine("BBP: " + err);

        MainWindow = new MainWindow { DataContext = vm };
        MainWindow.Show();
        _clientTask = Task.Run(() => client.RunAsync(_cts.Token));
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _cts.Cancel();
        try
        {
            _clientTask?.Wait(TimeSpan.FromSeconds(2));
        }
        catch (AggregateException)
        {
            // shutting down anyway
        }
        base.OnExit(e);
    }
}
