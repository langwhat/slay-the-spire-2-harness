using System.IO;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Sts2Mod.Core.Observation;

/// <summary>
/// Strips Godot BBCode styling tags and converts inline resource icon tags into localized plain text.
/// </summary>
public static partial class SemanticText
{
    public static string Json(string json, string language)
    {
        var root = JsonNode.Parse(json)!;
        ConvertNode(root, language, null);
        return root.ToJsonString();
    }

    public static string Plain(string text, string language)
    {
        text = Styles().Replace(text, "").Replace("[lb]", "[").Replace("[rb]", "]");
        var result = new StringBuilder();
        var previous = 0;
        for (var match = Images().Match(text); match.Success; match = match.NextMatch())
        {
            result.Append(text.AsSpan(previous, match.Index - previous));
            var name = Path.GetFileNameWithoutExtension(match.Groups[1].Value.Trim());
            var resource = ResourceWord(name, language);
            var end = match.Index + match.Length;
            var count = 1;
            var next = match.NextMatch();
            while (resource is not null
                   && next.Success
                   && next.Index == end
                   && ResourceWord(Path.GetFileNameWithoutExtension(next.Groups[1].Value.Trim()), language) == resource)
            {
                count++;
                end += next.Length;
                match = next;
                next = match.NextMatch();
            }

            if (resource is null)
            {
                result.Append($"[icon:{name}]");
            }
            else
            {
                var hasAmount = AmountBefore().IsMatch(result.ToString());
                if (!hasAmount)
                    result.Append(count).Append(' ');
                else if (result.Length > 0 && !char.IsWhiteSpace(result[^1]) && language is not ("zhs" or "zht" or "jpn" or "kor"))
                    result.Append(' ');
                result.Append(resource);
            }

            previous = end;
        }
        return result.Append(text.AsSpan(previous)).ToString();
    }

    private static void ConvertNode(JsonNode node, string language, string? parent)
    {
        if (node is JsonArray array)
        {
            for (var i = 0; i < array.Count; i++)
            {
                var item = array[i];
                if (parent == "local_keywords" && item is JsonValue value && value.TryGetValue<string>(out var text))
                    array[i] = Plain(text, language);
                else if (item is not null)
                    ConvertNode(item, language, parent);
            }
            return;
        }

        if (node is not JsonObject obj)
            return;

        foreach (var property in obj.ToArray())
        {
            if (property.Value is JsonValue value
                && value.TryGetValue<string>(out var text)
                && (property.Key is "name" or "text" or "description" or "label" or "prompt" or "reason"
                    || parent is "descriptions" or "modifier_descriptions"))
            {
                obj[property.Key] = Plain(text, language);
            }
            else if (property.Value is not null)
            {
                ConvertNode(property.Value, language, parent == "modifier_descriptions" ? parent : property.Key);
            }
        }
    }

    private static string? ResourceWord(string name, string language)
    {
        var energy = name.StartsWith("energy_", StringComparison.OrdinalIgnoreCase)
                     || name.EndsWith("_energy_icon", StringComparison.OrdinalIgnoreCase);
        var stars = name.Equals("star_icon", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("star", StringComparison.OrdinalIgnoreCase);
        if (!energy && !stars)
            return null;

        var words = language switch
        {
            "eng" => ("energy", "star"),
            "zhs" => ("能量", "辉星"),
            "zht" => ("能量", "輝星"),
            "deu" => ("Energie", "Stern"),
            "esp" or "spa" => ("energía", "estrella"),
            "fra" => ("énergie", "étoile"),
            "ind" => ("energi", "bintang"),
            "ita" => ("energia", "stella"),
            "jpn" => ("エナジー", "スター"),
            "kor" => ("에너지", "별"),
            "pol" => ("energia", "gwiazda"),
            "ptb" => ("energia", "estrela"),
            "rus" => ("энергия", "звезда"),
            "tha" => ("พลังงาน", "ดาว"),
            "tur" => ("enerji", "yıldız"),
            _ => ((string?)null, (string?)null),
        };
        return energy ? words.Item1 : words.Item2;
    }

    [GeneratedRegex(@"\[img(?:[^\]]*)\](.*?)\[/img\]", RegexOptions.Singleline | RegexOptions.IgnoreCase)]
    private static partial Regex Images();

    [GeneratedRegex(@"\[/?(?:b|i|u|s|center|left|right|fill|color|bgcolor|font|font_size|outline_size|outline_color|wave|sine|shake|rainbow|pulse|fade|tornado|red|blue|gold|purple|green|orange|pink|cream|gray|grey|yellow|white)(?:=[^\]]*)?\]", RegexOptions.IgnoreCase)]
    private static partial Regex Styles();

    [GeneratedRegex(@"(?:\d+(?:[.,]\d+)?|(?<![A-Za-z\p{N}_])[xX])\s*$")]
    private static partial Regex AmountBefore();
}
