using System.Diagnostics;
using Microsoft.Win32;

namespace Jarvis.Desktop;

public interface IAppLauncher
{
    /// <summary>Start an allowlisted app; returns the pid of the started process
    /// (0 when launched through a protocol URI).</summary>
    int Launch(AllowedApp app);
}

/// <summary>
/// Launches only what the allowlist entry describes: an absolute executable
/// path (from Candidates, PATH/System32 or the App Paths registry) started
/// without a shell, or the entry's fixed protocol URI. Nothing the Brain sends
/// is ever executed — the request carries only an allowlist id.
/// </summary>
public sealed class ProcessLauncher : IAppLauncher
{
    public int Launch(AllowedApp app)
    {
        var exe = Resolve(app);
        if (exe is not null)
        {
            var psi = new ProcessStartInfo(exe) { UseShellExecute = false };
            foreach (var arg in app.Args ?? [])
                psi.ArgumentList.Add(arg);
            using var p = Process.Start(psi) ?? throw new InvalidOperationException($"{app.Label} didn't start");
            return p.Id;
        }
        if (!string.IsNullOrEmpty(app.Uri))
        {
            using var _ = Process.Start(new ProcessStartInfo(app.Uri) { UseShellExecute = true });
            return 0;
        }
        throw new FileNotFoundException($"{app.Label} doesn't seem to be installed");
    }

    public static string? Resolve(AllowedApp app)
    {
        foreach (var candidate in app.Candidates ?? [])
        {
            var path = Environment.ExpandEnvironmentVariables(candidate);
            if (File.Exists(path))
                return path;
        }
        if (string.IsNullOrEmpty(app.Exe))
            return null;
        if (Path.IsPathRooted(app.Exe))
            return File.Exists(app.Exe) ? app.Exe : null;
        return SearchPath(app.Exe) ?? AppPaths(app.Exe);
    }

    private static string? SearchPath(string exe)
    {
        var dirs = new List<string> { Environment.SystemDirectory, Environment.GetFolderPath(Environment.SpecialFolder.Windows) };
        dirs.AddRange((Environment.GetEnvironmentVariable("PATH") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries));
        foreach (var dir in dirs)
        {
            try
            {
                var path = Path.Combine(Environment.ExpandEnvironmentVariables(dir.Trim('"')), exe);
                if (File.Exists(path))
                    return Path.GetFullPath(path);
            }
            catch (ArgumentException)
            {
                // malformed PATH entry
            }
        }
        return null;
    }

    /// <summary>HKCU/HKLM …\App Paths\&lt;exe&gt; — where installers register full paths.</summary>
    private static string? AppPaths(string exe)
    {
        foreach (var hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            using var key = hive.OpenSubKey($@"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}");
            if (key?.GetValue(null) is string value)
            {
                var path = Environment.ExpandEnvironmentVariables(value.Trim('"'));
                if (File.Exists(path))
                    return path;
            }
        }
        return null;
    }
}
