using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Jarvis.Desktop;

/// <summary>
/// Element names and automation ids that make desktop.uia.invoke high risk
/// (BBP amendment 20): send, delete, pay, sign out, … The words come from
/// risky-words.json (protocol/policy, copied next to the exe), the same file the
/// Brain uses; the Body checks the element it actually found, whatever the
/// Brain said. Case, full-width letters, zero-width characters and common
/// Cyrillic/Greek look-alikes don't hide a word, and a word mixing Latin with
/// other letters counts as risky. No file, or a broken one: everything is risky.
/// </summary>
public sealed partial class RiskyNames
{
    public const string FileName = "risky-words.json";
    private static readonly string[] Endings = ["ing", "es", "ed", "s", "d"];

    private static readonly Dictionary<char, char> Confusables = new()
    {
        ['а'] = 'a', ['в'] = 'b', ['е'] = 'e', ['ё'] = 'e', ['к'] = 'k', ['м'] = 'm', ['н'] = 'h', ['о'] = 'o',
        ['р'] = 'p', ['с'] = 'c', ['т'] = 't', ['у'] = 'y', ['х'] = 'x', ['ѕ'] = 's', ['і'] = 'i', ['ї'] = 'i',
        ['ј'] = 'j', ['ԁ'] = 'd', ['ԛ'] = 'q', ['ԝ'] = 'w', ['ɡ'] = 'g', ['ɩ'] = 'i', ['ʟ'] = 'l',
        ['А'] = 'A', ['В'] = 'B', ['Е'] = 'E', ['К'] = 'K', ['М'] = 'M', ['Н'] = 'H', ['О'] = 'O', ['Р'] = 'P',
        ['С'] = 'C', ['Т'] = 'T', ['У'] = 'Y', ['Х'] = 'X', ['Ѕ'] = 'S', ['І'] = 'I', ['Ј'] = 'J',
        ['α'] = 'a', ['ε'] = 'e', ['ι'] = 'i', ['κ'] = 'k', ['ν'] = 'v', ['ο'] = 'o', ['ρ'] = 'p', ['τ'] = 't',
        ['υ'] = 'u', ['χ'] = 'x', ['Α'] = 'A', ['Β'] = 'B', ['Ε'] = 'E', ['Ζ'] = 'Z', ['Η'] = 'H', ['Ι'] = 'I',
        ['Κ'] = 'K', ['Μ'] = 'M', ['Ν'] = 'N', ['Ο'] = 'O', ['Ρ'] = 'P', ['Τ'] = 'T', ['Υ'] = 'Y', ['Χ'] = 'X',
    };

    private readonly HashSet<string>? _words;
    private readonly List<string[]> _phrases = [];

    private RiskyNames(HashSet<string>? words, IEnumerable<string[]> phrases)
    {
        _words = words;
        _phrases.AddRange(phrases);
    }

    /// <summary>Every name is risky (the word list couldn't be loaded).</summary>
    public static RiskyNames FailClosed { get; } = new(null, []);

    public bool AllRisky => _words is null;

    public static (RiskyNames Names, string? Problem) Load(string path)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var words = doc.RootElement.GetProperty("words").EnumerateArray()
                           .Select(w => w.GetString()!.ToLowerInvariant()).ToHashSet();
            var phrases = doc.RootElement.GetProperty("phrases").EnumerateArray()
                             .Select(p => p.GetString()!.ToLowerInvariant().Split(' ', StringSplitOptions.RemoveEmptyEntries));
            return (new RiskyNames(words, phrases), null);
        }
        catch (Exception e) when (e is IOException or JsonException or KeyNotFoundException
                                       or InvalidOperationException or UnauthorizedAccessException)
        {
            return (FailClosed, $"{FileName} unavailable ({e.Message}); every click will need your approval");
        }
    }

    public bool IsRisky(params string?[] texts)
    {
        if (_words is null)
            return true;
        foreach (var text in texts)
        {
            if (string.IsNullOrWhiteSpace(text))
                continue;
            var tokens = WordsOf(text);
            if (tokens.Any(t => Hits(t, _words) || MixedScript(t)))
                return true;
            foreach (var phrase in _phrases)
            {
                var joined = string.Concat(phrase);
                if (tokens.Any(t => Hits(t, [joined])))
                    return true;
                for (var i = 0; i + phrase.Length <= tokens.Count; i++)
                    if (tokens.Skip(i).Take(phrase.Length).SequenceEqual(phrase))
                        return true;
            }
        }
        return false;
    }

    /// <summary>The words of a label, normalised as the policy compares them.</summary>
    public static List<string> WordsOf(string text)
    {
        var s = text.Normalize(NormalizationForm.FormKC);
        var sb = new StringBuilder(s.Length);
        foreach (var c in s)
        {
            if (CharUnicodeInfo.GetUnicodeCategory(c) == UnicodeCategory.Format)
                continue;                                         // zero-width and friends
            sb.Append(Confusables.TryGetValue(c, out var latin) ? latin : c);
        }
        var decomposed = sb.ToString().Normalize(NormalizationForm.FormKD);
        sb.Clear();
        foreach (var c in decomposed)
            if (CharUnicodeInfo.GetUnicodeCategory(c) != UnicodeCategory.NonSpacingMark)
                sb.Append(c);                                     // accents off: é → e
        var split = Camel().Replace(sb.ToString(), " ");
        return NonWord().Split(split.ToLowerInvariant()).Where(w => w.Length > 0).ToList();
    }

    private static bool Hits(string word, ICollection<string> words) =>
        words.Contains(word) || Endings.Any(e => word.EndsWith(e, StringComparison.Ordinal) && words.Contains(word[..^e.Length]));

    private static bool MixedScript(string word) =>
        word.Any(c => c is >= 'a' and <= 'z') && word.Any(c => char.IsLetter(c) && c is not (>= 'a' and <= 'z'));

    [GeneratedRegex(@"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")]
    private static partial Regex Camel();

    [GeneratedRegex(@"[^\p{L}\p{N}]+")]
    private static partial Regex NonWord();
}
