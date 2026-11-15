namespace Jarvis.Desktop;

/// <summary>
/// How a spoken element name finds the element when no name matches exactly
/// (amendment 22). Pure, so it is tested without UI Automation.
///
/// Names are compared as words: case, punctuation, accents and camelCase are
/// ignored (<see cref="RiskyNames.WordsOf"/>), a single digit equals its word
/// ("7" = "seven") and a plural -s is dropped ("equal" = "Equals"). Then, in
/// order, the first rule with any match decides:
/// <list type="number">
/// <item>same words ("sign-in" = "Sign in", "7" = "Seven");</item>
/// <item>the element's name starts with those words ("multiply" = "Multiply by");</item>
/// <item>the element's name contains all of them ("root" = "Square root").</item>
/// </list>
/// The caller acts only when that rule matches exactly one element, and never
/// on an element that looks risky (that must be named in full). Close
/// spellings are only suggested, never acted on.
/// </summary>
public static class UiMatcher
{
    public enum Rule { SameWords, StartsWith, Contains }

    private static readonly string[] Digits = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];

    public static List<string> Words(string text) => RiskyNames.WordsOf(text).Select(Canonical).ToList();

    private static string Canonical(string word)
    {
        if (word.Length == 1 && word[0] is >= '0' and <= '9')
            return Digits[word[0] - '0'];
        if (word.Length > 3 && word[^1] == 's' && word[^2] != 's')
            return word[..^1];
        return word;
    }

    /// <summary>The first rule that matches anything, and everything it matches;
    /// null when no rule matches.</summary>
    public static (Rule Rule, List<T> Matches)? Relaxed<T>(string query, IEnumerable<T> candidates) where T : IUiElement
    {
        var q = Words(query);
        if (q.Count == 0)
            return null;
        var named = candidates.Select(c => (Element: c, Words: Words(c.Name))).Where(c => c.Words.Count > 0).ToList();
        foreach (var rule in Enum.GetValues<Rule>())
        {
            var matches = named.Where(c => Test(rule, q, c.Words)).Select(c => c.Element).ToList();
            if (matches.Count > 0)
                return (rule, matches);
        }
        return null;
    }

    private static bool Test(Rule rule, List<string> q, List<string> name) => rule switch
    {
        Rule.SameWords => name.SequenceEqual(q),
        Rule.StartsWith => name.Count > q.Count && name.Take(q.Count).SequenceEqual(q),
        _ => q.All(name.Contains),
    };

    /// <summary>Up to <paramref name="max"/> names that are spelled close to the
    /// query (a typo or two), closest first. Only for "did you mean".</summary>
    public static List<string> Suggest(string query, IEnumerable<IUiElement> candidates, int max = 3)
    {
        var q = string.Join(' ', Words(query));
        if (q.Length == 0)
            return [];
        var limit = Math.Max(1, q.Length / 3);
        return candidates
            .Select(c => c.Name)
            .Where(n => n.Length > 0)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Select(n => (Name: n, Distance: Distance(q, string.Join(' ', Words(n)))))
            .Where(x => x.Distance <= limit)
            .OrderBy(x => x.Distance)
            .ThenBy(x => x.Name, StringComparer.OrdinalIgnoreCase)
            .Take(max)
            .Select(x => x.Name)
            .ToList();
    }

    /// <summary>Edits between two strings, a swap of neighbours counting as one
    /// ("sned" is 1 from "send"): optimal string alignment distance.</summary>
    public static int Distance(string a, string b)
    {
        var d = new int[a.Length + 1, b.Length + 1];
        for (var i = 0; i <= a.Length; i++)
            d[i, 0] = i;
        for (var j = 0; j <= b.Length; j++)
            d[0, j] = j;
        for (var i = 1; i <= a.Length; i++)
        {
            for (var j = 1; j <= b.Length; j++)
            {
                var cost = a[i - 1] == b[j - 1] ? 0 : 1;
                d[i, j] = Math.Min(Math.Min(d[i - 1, j] + 1, d[i, j - 1] + 1), d[i - 1, j - 1] + cost);
                if (i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1])
                    d[i, j] = Math.Min(d[i, j], d[i - 2, j - 2] + 1);
            }
        }
        return d[a.Length, b.Length];
    }
}
