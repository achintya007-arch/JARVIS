using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Jarvis.Launcher.Tests;

/// <summary>The real host with real (tiny) processes: cmd.exe.</summary>
public sealed class ProcessHostTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "jarvis-host-tests", Guid.NewGuid().ToString("n"));
    private static readonly string Cmd = Path.Combine(Environment.SystemDirectory, "cmd.exe");

    public ProcessHostTests() => Directory.CreateDirectory(_dir);

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
    public async Task Start_LogsOutputAndTheExitCode()
    {
        var host = new ProcessHost(_dir);
        var child = host.Start(new ProcessSpec(Cmd, ["/c", "echo hello from %JARVIS_TEST% & exit 3"], _dir,
                                               new Dictionary<string, string> { ["JARVIS_TEST"] = "the launcher" }, "child"));
        Assert.Equal(3, await child.Exited.WaitAsync(TimeSpan.FromSeconds(20)));
        var log = await File.ReadAllTextAsync(Path.Combine(_dir, "child.log"));
        Assert.Contains("cmd.exe /c echo", log);
        Assert.Contains("hello from the launcher", log);
        Assert.Contains("exited with code 3", log);
    }

    [Fact]
    public async Task ControlLines_ReachTheChild_AndKillTreeEndsIt()
    {
        var host = new ProcessHost(_dir);
        var child = host.Start(new ProcessSpec(Cmd, ["/c", "set /p LINE= & call echo got %LINE% & ping -n 30 127.0.0.1 >nul"],
                                               _dir, null, "piped", RedirectInput: true));
        child.SendLine("shutdown");
        child.CloseInput();
        for (var i = 0; i < 100 && !File.ReadAllText(Path.Combine(_dir, "piped.log")).Contains("got shutdown"); i++)
            await Task.Delay(100);
        Assert.Contains("got shutdown", File.ReadAllText(Path.Combine(_dir, "piped.log")));
        Assert.False(child.HasExited);
        child.KillTree();
        await child.Exited.WaitAsync(TimeSpan.FromSeconds(20));
    }

    [Fact]
    public async Task OnlyProcessesMarkedSo_AreTiedToTheLauncher()
    {
        var host = new ProcessHost(_dir);
        var tied = host.Start(new ProcessSpec(Cmd, ["/c", "ping -n 30 127.0.0.1 >nul"], _dir, EndsWithLauncher: true));
        var free = host.Start(new ProcessSpec(Cmd, ["/c", "ping -n 30 127.0.0.1 >nul"], _dir));
        try
        {
            Assert.True(InOurJob(tied.Id));
            Assert.False(InOurJob(free.Id));
        }
        finally
        {
            tied.KillTree();
            free.KillTree();
            await Task.WhenAll(tied.Exited, free.Exited).WaitAsync(TimeSpan.FromSeconds(20));
        }
    }

    [Fact]
    public async Task RunAsync_ReturnsOutput_OrKillsOnTimeout()
    {
        var host = new ProcessHost(_dir);
        var ok = await host.RunAsync(new ProcessSpec(Cmd, ["/c", "echo preflight-output"], _dir), TimeSpan.FromSeconds(20), default);
        Assert.Equal((0, false), (ok.ExitCode, ok.TimedOut));
        Assert.Contains("preflight-output", ok.Output);

        var slow = await host.RunAsync(new ProcessSpec(Cmd, ["/c", "ping -n 30 127.0.0.1 >nul"], _dir), TimeSpan.FromMilliseconds(300), default);
        Assert.True(slow.TimedOut);
    }

    private static bool InOurJob(int pid)
    {
        using var p = Process.GetProcessById(pid);
        Assert.NotEqual(IntPtr.Zero, KillOnCloseJob.Handle);
        Assert.True(IsProcessInJob(p.Handle, KillOnCloseJob.Handle, out var inJob));
        return inJob;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
}
