using System.IO;
using System.Text.Json.Nodes;
using Bbp.Client;
using Jarvis.Launcher;

namespace Jarvis.Body.Tests;

public sealed class BodySettingsTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "jarvis-body-settings", Guid.NewGuid().ToString("n"));

    public BodySettingsTests() => Directory.CreateDirectory(_dir);

    public void Dispose()
    {
        try
        {
            Directory.Delete(_dir, recursive: true);
        }
        catch (IOException)
        {
        }
    }

    private string Path_ => Path.Combine(_dir, BodySettings.FileName);

    [Fact]
    public void LauncherKeys_ReadInSnakeCase_WithDefaults()
    {
        File.WriteAllText(Path_, """
            { "url": "ws://127.0.0.1:9001/bbp", "jarvis_root": "C:\\J\\jarvis_v1", "python_path": "C:\\py.exe",
              "brain_mode": "text", "autostart_brain": false, "ollama_path": "C:\\o.exe", "phonebox_path": "C:\\p.exe" }
            """);
        var s = BodySettings.Load(Path_);
        Assert.Equal(@"C:\J\jarvis_v1", s.JarvisRoot);
        Assert.False(s.AutostartBrain);
        Assert.Equal(9001, s.Port);
        var l = s.ToLauncher(@"C:\J\jarvis_v1");
        Assert.Equal((@"C:\py.exe", "text", 9001, @"C:\o.exe", @"C:\p.exe"),
                     (l.PythonPath, l.BrainMode, l.BbpPort, l.OllamaPath, l.PhoneBoxPath));

        var defaults = BodySettings.Load(Path.Combine(_dir, "missing.json"));
        Assert.True(defaults.AutostartBrain);
        Assert.Equal("wake", defaults.BrainMode);
        Assert.Null(defaults.JarvisRoot);
        Assert.Equal(8765, defaults.Port);
    }

    [Fact]
    public void SaveValue_KeepsEverythingElse()
    {
        File.WriteAllText(Path_, """{ "hotkey": "Ctrl+Alt+K", "future_key": [1, 2] }""");
        BodySettings.SaveValue("jarvis_root", @"C:\J\jarvis_v1", Path_);
        var json = JsonNode.Parse(File.ReadAllText(Path_))!;
        Assert.Equal("Ctrl+Alt+K", (string)json["hotkey"]!);
        Assert.Equal(2, json["future_key"]!.AsArray().Count);
        Assert.Equal(@"C:\J\jarvis_v1", BodySettings.Load(Path_).JarvisRoot);

        BodySettings.SaveValue("jarvis_root", @"C:\K", Path.Combine(_dir, "new.json"));     // no file yet
        Assert.Equal(@"C:\K", BodySettings.Load(Path.Combine(_dir, "new.json")).JarvisRoot);
    }

    [Fact]
    public void Token_IsFoundUnderJarvisRoot()
    {
        var root = Path.Combine(_dir, "jarvis_v1");
        Directory.CreateDirectory(Path.Combine(root, "data"));
        File.WriteAllText(Path.Combine(root, "data", "bbp_token"), "tok123\n");
        Assert.Equal("tok123", new BodySettings { JarvisRoot = root }.ReadToken());

        var explicitToken = Path.Combine(_dir, "other_token");
        File.WriteAllText(explicitToken, "other");
        Assert.Equal("other", new BodySettings { JarvisRoot = root, TokenPath = explicitToken }.ReadToken());
    }
}

public class BrainViewTests
{
    [Theory]
    [InlineData(BrainStatus.Running, true, "Brain: running (started by JARVIS)")]
    [InlineData(BrainStatus.Attached, false, "Brain: running (started by hand)")]
    [InlineData(BrainStatus.Restarting, false, "Brain: crashed, restarting…")]
    [InlineData(BrainStatus.Failed, false, "Brain: stopped (see notification)")]
    public void TrayLine(BrainStatus status, bool owned, string line) =>
        Assert.Equal(line, BrainView.TrayLine(new LauncherState(status, "x", owned)));

    [Fact]
    public void HudShowsWhyItsOffline_WhileTheBrainStartsOrFails()
    {
        var vm = new HudViewModel();
        vm.OnConnection(ConnectionState.Disconnected, "connection refused — retry in 2 s");
        Assert.Equal("Offline · connection refused — retry in 2 s", vm.StatusText);

        vm.OnBrain(new LauncherState(BrainStatus.StartingOllama, "Starting Ollama", false));
        Assert.Equal("Offline · Starting Ollama", vm.StatusText);

        vm.OnBrain(new LauncherState(BrainStatus.Failed, "Model qwen isn't pulled.", false));
        Assert.Equal("Offline · JARVIS didn't start: Model qwen isn't pulled.", vm.StatusText);

        vm.OnBrain(new LauncherState(BrainStatus.Running, "JARVIS is running", true));
        Assert.Equal("Offline · connection refused — retry in 2 s", vm.StatusText);
    }

    [Theory]
    [InlineData(NoticeLevel.Alert, "alert")]
    [InlineData(NoticeLevel.Warn, "warn")]
    [InlineData(NoticeLevel.Info, "info")]
    public void Levels(NoticeLevel level, string text) => Assert.Equal(text, BrainView.Level(level));
}
