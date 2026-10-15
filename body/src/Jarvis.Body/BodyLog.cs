using System.Diagnostics;
using System.IO;

namespace Jarvis.Body;

/// <summary>
/// Append-only log at %LOCALAPPDATA%\JARVIS\body.log (a WinExe has no console).
/// Logging never throws: a full disk must not take the HUD down.
/// </summary>
public static class BodyLog
{
    private static readonly object Gate = new();

    public static string FilePath { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JARVIS", "body.log");

    public static void Write(string message)
    {
        var line = $"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss.fff} {message}";
        Debug.WriteLine(line);
        try
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
                File.AppendAllText(FilePath, line + Environment.NewLine);
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // best effort
        }
    }
}
