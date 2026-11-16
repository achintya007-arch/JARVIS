namespace Jarvis.Launcher.Tests;

public class RestartPolicyTests
{
    private static readonly DateTimeOffset T0 = new(2026, 11, 16, 9, 0, 0, TimeSpan.Zero);

    [Fact]
    public void ThreeRestartsInFiveMinutes_WithGrowingWaits_ThenNone()
    {
        var policy = new RestartPolicy();
        Assert.Equal(TimeSpan.FromSeconds(2), policy.Next(T0));
        Assert.Equal(TimeSpan.FromSeconds(5), policy.Next(T0.AddSeconds(10)));
        Assert.Equal(TimeSpan.FromSeconds(15), policy.Next(T0.AddSeconds(20)));
        Assert.Null(policy.Next(T0.AddMinutes(4)));
        Assert.Equal(3, policy.Recent);
    }

    [Fact]
    public void OldRestartsStopCounting()
    {
        var policy = new RestartPolicy();
        policy.Next(T0);
        policy.Next(T0.AddMinutes(1));
        policy.Next(T0.AddMinutes(2));
        // The first one aged out, so this is the 3rd of the window, not a 4th.
        Assert.Equal(TimeSpan.FromSeconds(15), policy.Next(T0.AddMinutes(5)));
        policy.Reset();
        Assert.Equal(0, policy.Recent);
    }
}

public class BrainConfigInfoTests
{
    [Fact]
    public void ParsesThePreflightReport_IgnoringLogLinesBefore()
    {
        var (info, error) = BrainConfigInfo.Parse("INFO something\n" + FakeHost.Report(phonebox: true) + "\n");
        Assert.Null(error);
        Assert.Equal(["qwen", "nomic-embed-text"], info!.Models.Required);
        Assert.Equal(["qwen-small"], info.Models.Optional);
        Assert.True(info.Phonebox.Enabled);
        Assert.Equal("http://localhost:11434", info.OllamaUrl);
    }

    [Fact]
    public void ReportsTheConfigError()
    {
        Assert.Equal("config.yaml can't be read: x", BrainConfigInfo.Parse("{\"error\": \"config.yaml can't be read: x\"}").Error);
        Assert.Contains("isn't valid JSON", BrainConfigInfo.Parse("{nope").Error);
    }

    [Theory]
    [InlineData("nomic-embed-text", true)]                // pulled as nomic-embed-text:latest
    [InlineData("qwen2.5:7b-instruct-q4_K_M", true)]
    [InlineData("qwen2.5:3b-instruct-q4_K_M", false)]
    [InlineData("qwen2.5", false)]                        // a tag was pulled, not :latest
    public void ModelNames(string model, bool has) =>
        Assert.Equal(has, BrainConfigInfo.Has(["nomic-embed-text:latest", "qwen2.5:7b-instruct-q4_K_M"], model));
}

public sealed class RotatingLogTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "jarvis-log-tests", Guid.NewGuid().ToString("n"));

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

    [Fact]
    public void RotatesAtTheLimit_KeepingThree()
    {
        var path = Path.Combine(_dir, "brain.log");
        var log = new RotatingLog(path, maxBytes: 100, keep: 3);
        for (var i = 0; i < 30; i++)
            log.Write($"line {i:00} {new string('x', 30)}");     // ~40 bytes: 2 per file
        Assert.True(new FileInfo(path).Length <= 100);
        Assert.True(File.Exists(path + ".1") && File.Exists(path + ".2") && File.Exists(path + ".3"));
        Assert.False(File.Exists(path + ".4"));
        Assert.Contains("line 29", File.ReadAllText(path));
        Assert.Contains("line 27", File.ReadAllText(path + ".1"));
    }
}

public sealed class SettingsTests : IDisposable
{
    private readonly string _top = Path.Combine(Path.GetTempPath(), "jarvis-root-tests", Guid.NewGuid().ToString("n"));

    public void Dispose()
    {
        try
        {
            Directory.Delete(_top, recursive: true);
        }
        catch (IOException)
        {
        }
    }

    [Fact]
    public void FindsJarvisV1_FromInsideTheRepo()
    {
        var root = Path.Combine(_top, "JARVIS", "jarvis_v1");
        Directory.CreateDirectory(Path.Combine(root, "core_v3"));
        File.WriteAllText(Path.Combine(root, "core_v3", "__main__.py"), "");
        var bin = Path.Combine(_top, "JARVIS", "body", "src", "Jarvis.Body", "bin", "Debug");
        Directory.CreateDirectory(bin);
        Assert.Equal(root, LauncherSettings.FindJarvisRoot(bin));
        Assert.Equal(root, LauncherSettings.FindJarvisRoot(root));
        Assert.Null(LauncherSettings.FindJarvisRoot(Path.GetTempPath()));
    }
}

public sealed class StartupEntryTests : IDisposable
{
    // A throwaway key under HKCU, not the real Run key.
    private readonly string _key = $@"Software\JARVIS-tests\{Guid.NewGuid():n}";

    public void Dispose() => Microsoft.Win32.Registry.CurrentUser.DeleteSubKeyTree(@"Software\JARVIS-tests", throwOnMissingSubKey: false);

    [Fact]
    public void EnableDisable_ForThisExeOnly()
    {
        var entry = new StartupEntry("JARVIS", _key);
        Assert.False(entry.IsEnabledFor(@"C:\Apps\JARVIS\Jarvis.Body.exe"));
        entry.Enable(@"C:\Apps\JARVIS\Jarvis.Body.exe");
        Assert.Equal("\"C:\\Apps\\JARVIS\\Jarvis.Body.exe\"", entry.Command);
        Assert.True(entry.IsEnabledFor(@"c:\apps\jarvis\jarvis.body.exe"));
        Assert.False(entry.IsEnabledFor(@"C:\Other\Jarvis.Body.exe"));
        entry.Disable();
        Assert.Null(entry.Command);
        entry.Disable();                                          // already off: fine
    }
}
