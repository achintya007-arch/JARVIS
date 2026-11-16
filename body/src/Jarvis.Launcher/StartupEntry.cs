using Microsoft.Win32;

namespace Jarvis.Launcher;

/// <summary>
/// "Start with Windows": a value under HKCU\...\CurrentVersion\Run (no admin
/// rights), the same one install.ps1 -StartWithWindows writes and
/// uninstall.ps1 removes.
/// </summary>
public sealed class StartupEntry(string valueName = "JARVIS", string subKey = StartupEntry.RunKey)
{
    public const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

    public string? Command
    {
        get
        {
            using var key = Registry.CurrentUser.OpenSubKey(subKey);
            return key?.GetValue(valueName) as string;
        }
    }

    /// <summary>On for this exe (a Run entry for another copy doesn't count).</summary>
    public bool IsEnabledFor(string exePath) =>
        Command is { } command && command.Trim().Trim('"').StartsWith(exePath, StringComparison.OrdinalIgnoreCase);

    public void Enable(string exePath)
    {
        using var key = Registry.CurrentUser.CreateSubKey(subKey);
        key.SetValue(valueName, $"\"{exePath}\"");
    }

    public void Disable()
    {
        using var key = Registry.CurrentUser.OpenSubKey(subKey, writable: true);
        key?.DeleteValue(valueName, throwOnMissingValue: false);
    }
}
