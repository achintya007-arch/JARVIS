namespace Bbp.Client;

/// <summary>
/// Finds the shared auth token the Brain writes on first run
/// (jarvis_v1/data/bbp_token by default). Looked up again before every
/// connection attempt, so starting the Body before the Brain just works.
/// </summary>
public static class TokenLocator
{
    public const string EnvVar = "JARVIS_BBP_TOKEN_PATH";

    /// <summary>First existing candidate: the configured path, the env var,
    /// then jarvis_v1/data/bbp_token or data/bbp_token in any parent of startDir.</summary>
    public static string? FindPath(string? configured, string startDir)
    {
        foreach (var candidate in Candidates(configured, startDir))
            if (File.Exists(candidate))
                return candidate;
        return null;
    }

    public static IEnumerable<string> Candidates(string? configured, string startDir)
    {
        if (!string.IsNullOrWhiteSpace(configured))
            yield return Path.GetFullPath(configured, startDir);
        var env = Environment.GetEnvironmentVariable(EnvVar);
        if (!string.IsNullOrWhiteSpace(env))
            yield return Path.GetFullPath(env, startDir);
        for (var dir = new DirectoryInfo(startDir); dir is not null; dir = dir.Parent)
        {
            yield return Path.Combine(dir.FullName, "jarvis_v1", "data", "bbp_token");
            yield return Path.Combine(dir.FullName, "data", "bbp_token");
        }
    }

    public static string? ReadToken(string? path)
    {
        if (path is null || !File.Exists(path))
            return null;
        try
        {
            var token = File.ReadAllText(path).Trim();
            return token.Length > 0 ? token : null;
        }
        catch (IOException)
        {
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return null;
        }
    }
}
