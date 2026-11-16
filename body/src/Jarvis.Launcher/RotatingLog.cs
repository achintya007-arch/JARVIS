using System.Text;

namespace Jarvis.Launcher;

/// <summary>
/// A child's output, appended line by line to e.g. brain.log. When the file
/// would pass <see cref="MaxBytes"/> it becomes brain.log.1 (older ones shift to
/// .2, .3; the oldest is dropped). Never throws: logging must not take JARVIS down.
/// </summary>
public sealed class RotatingLog(string path, long maxBytes = 5 * 1024 * 1024, int keep = 3)
{
    private readonly object _gate = new();
    private long _size = -1;

    public string Path { get; } = path;
    public long MaxBytes { get; } = maxBytes;

    public void Write(string line)
    {
        var bytes = Encoding.UTF8.GetBytes(line + Environment.NewLine);
        lock (_gate)
        {
            try
            {
                Directory.CreateDirectory(System.IO.Path.GetDirectoryName(Path)!);
                if (_size < 0)
                    _size = File.Exists(Path) ? new FileInfo(Path).Length : 0;
                if (_size > 0 && _size + bytes.Length > MaxBytes)
                    Rotate();
                using (var f = new FileStream(Path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete))
                    f.Write(bytes);
                _size += bytes.Length;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                _size = -1;                                       // re-read next time
            }
        }
    }

    private void Rotate()
    {
        File.Delete($"{Path}.{keep}");
        for (var i = keep - 1; i >= 1; i--)
            if (File.Exists($"{Path}.{i}"))
                File.Move($"{Path}.{i}", $"{Path}.{i + 1}");
        File.Move(Path, $"{Path}.1");
        _size = 0;
    }
}
