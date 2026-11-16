using System.ComponentModel;
using System.Diagnostics;
using System.Net.Http.Json;
using System.Runtime.InteropServices;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Serialization;

namespace Jarvis.Launcher;

/// <summary>Real processes: hidden (no console window), output to rotating logs.</summary>
public sealed class ProcessHost(string logDirectory) : IProcessHost
{
    public string LogDirectory { get; } = logDirectory;

    public IChildProcess Start(ProcessSpec spec)
    {
        var log = spec.LogName is null ? null : new RotatingLog(Path.Combine(LogDirectory, spec.LogName + ".log"));
        var process = new Process { StartInfo = Info(spec, spec.RedirectInput), EnableRaisingEvents = true };
        var exited = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        process.OutputDataReceived += (_, e) => { if (e.Data is not null) log?.Write(e.Data); };
        process.ErrorDataReceived += (_, e) => { if (e.Data is not null) log?.Write(e.Data); };
        process.Exited += (_, _) =>
        {
            process.WaitForExit();                                // flush the output readers
            log?.Write($"==== {Path.GetFileName(spec.FileName)} exited with code {process.ExitCode} at {DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss} ====");
            exited.TrySetResult(process.ExitCode);
        };
        log?.Write($"==== {Path.GetFileName(spec.FileName)} {string.Join(' ', spec.Arguments)} starting at {DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss} ====");
        process.Start();
        if (spec.EndsWithLauncher)
            KillOnCloseJob.Add(process, log);
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return new Child(process, exited.Task);
    }

    public async Task<RunResult> RunAsync(ProcessSpec spec, TimeSpan timeout, CancellationToken ct)
    {
        using var process = new Process { StartInfo = Info(spec, redirectInput: true) };
        process.Start();
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync(ct);
        var stderr = process.StandardError.ReadToEndAsync(ct);
        using var limit = CancellationTokenSource.CreateLinkedTokenSource(ct);
        limit.CancelAfter(timeout);
        try
        {
            await process.WaitForExitAsync(limit.Token);
        }
        catch (OperationCanceledException)
        {
            try
            {
                process.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException)
            {
                // already gone
            }
            if (ct.IsCancellationRequested)
                throw;
            return new RunResult(-1, "", TimedOut: true);
        }
        return new RunResult(process.ExitCode, await stdout + await stderr, TimedOut: false);
    }

    public bool FileExists(string path) => File.Exists(path);

    public string? FindOnPath(string exe) =>
        (Environment.GetEnvironmentVariable("PATH") ?? "")
            .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(dir => Path.Combine(dir, exe))
            .FirstOrDefault(File.Exists);

    private static ProcessStartInfo Info(ProcessSpec spec, bool redirectInput)
    {
        var info = new ProcessStartInfo(spec.FileName)
        {
            WorkingDirectory = spec.WorkingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,                                // no console window
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = redirectInput,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        if (redirectInput)
            info.StandardInputEncoding = new UTF8Encoding(false);  // no BOM before "shutdown"
        foreach (var arg in spec.Arguments)
            info.ArgumentList.Add(arg);
        foreach (var (key, value) in spec.Environment ?? new Dictionary<string, string>())
            info.Environment[key] = value;
        return info;
    }

    private sealed class Child(Process process, Task<int> exited) : IChildProcess
    {
        public int Id { get; } = process.Id;
        public Task<int> Exited => exited;
        public bool HasExited => exited.IsCompleted;

        public void SendLine(string line)
        {
            process.StandardInput.WriteLine(line);
            process.StandardInput.Flush();
        }

        public void CloseInput() => process.StandardInput.Close();

        public void KillTree()
        {
            if (!process.HasExited)
                process.Kill(entireProcessTree: true);
        }
    }
}

/// <summary>Real TCP and Ollama checks, short timeouts.</summary>
public sealed class Network : INetwork
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(3) };

    public async Task<bool> IsListeningAsync(string host, int port, CancellationToken ct)
    {
        using var client = new TcpClient();
        using var limit = CancellationTokenSource.CreateLinkedTokenSource(ct);
        limit.CancelAfter(TimeSpan.FromMilliseconds(800));
        try
        {
            await client.ConnectAsync(host, port, limit.Token);
            return true;
        }
        catch (Exception e) when (e is SocketException or OperationCanceledException && !ct.IsCancellationRequested)
        {
            return false;
        }
    }

    public async Task<IReadOnlyList<string>?> OllamaModelsAsync(Uri baseUrl, CancellationToken ct)
    {
        try
        {
            var tags = await Http.GetFromJsonAsync<Tags>(new Uri(baseUrl, "/api/tags"), ct);
            return tags?.Models?.Select(m => m.Name).Where(n => n is not null).Select(n => n!).ToList() ?? [];
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or System.Text.Json.JsonException
                                      && !ct.IsCancellationRequested)
        {
            return null;
        }
    }

    private sealed record Tags([property: JsonPropertyName("models")] List<Model>? Models);
    private sealed record Model([property: JsonPropertyName("name")] string? Name);
}

/// <summary>
/// A Windows job that ends its processes when the Body's handle to it closes,
/// which Windows does even when the Body crashes or is killed. Ollama and
/// PhoneBox (when the Body started them) go in it, so they never outlive the
/// Body. The Brain doesn't: it stops itself, cleanly, when its stdin closes.
/// </summary>
internal static class KillOnCloseJob
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x2000;

    private static readonly Lazy<IntPtr> Job = new(Create);

    /// <summary>For tests: the job's handle (zero if it couldn't be made).</summary>
    internal static IntPtr Handle => Job.Value;

    public static void Add(Process process, RotatingLog? log)
    {
        try
        {
            var job = Job.Value;
            if (job == IntPtr.Zero || !AssignProcessToJobObject(job, process.Handle))
                log?.Write($"(couldn't tie this process to the Body: {new Win32Exception().Message})");
        }
        catch (Exception e) when (e is Win32Exception or InvalidOperationException)
        {
            log?.Write($"(couldn't tie this process to the Body: {e.Message})");
        }
    }

    private static IntPtr Create()
    {
        var job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            return IntPtr.Zero;
        var info = new JobObjectExtendedLimit { Basic = new JobObjectBasicLimit { LimitFlags = JobObjectLimitKillOnJobClose } };
        var size = Marshal.SizeOf<JobObjectExtendedLimit>();
        var buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(info, buffer, fDeleteOld: false);
            if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, buffer, (uint)size))
            {
                CloseHandle(job);
                return IntPtr.Zero;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
        return job;     // never closed: Windows closes it when the Body exits, ending the job's processes
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimit
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimit
    {
        public JobObjectBasicLimit Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
}
