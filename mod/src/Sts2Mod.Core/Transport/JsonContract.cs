using System.Collections.Immutable;
using System.IO;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Sts2Mod.Core.Transport;

/// <summary>
/// Strict UTF-8 JSON contract shared across HTTP endpoints, observations, catalogs, and developer tools.
/// Property names and enum values use lowercase snake_case while dictionary keys preserve exact object references.
/// </summary>
public static class JsonContract
{
    public static JsonSerializerOptions Options { get; } = CreateOptions();

    public static JsonSerializerOptions CreateOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.General)
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DictionaryKeyPolicy = null,
            PropertyNameCaseInsensitive = false,
            DefaultIgnoreCondition = JsonIgnoreCondition.Never,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
            WriteIndented = false,
        };
        options.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower, allowIntegerValues: false));
        return options;
    }

    public static string Serialize<T>(T value) => JsonSerializer.Serialize(value, Options);

    public static T? DeserializeUnique<T>(string json)
    {
        EnsureNoDuplicateProperties(json);
        return JsonSerializer.Deserialize<T>(json, Options);
    }

    public static void EnsureNoDuplicateProperties(string json)
    {
        using var document = JsonDocument.Parse(json);
        EnsureUnique(document.RootElement);

        static void EnsureUnique(JsonElement element)
        {
            if (element.ValueKind == JsonValueKind.Object)
            {
                var names = new HashSet<string>(StringComparer.Ordinal);
                foreach (var property in element.EnumerateObject())
                {
                    if (!names.Add(property.Name))
                        throw new JsonException($"Duplicate JSON property: {property.Name}");
                    EnsureUnique(property.Value);
                }
            }
            else if (element.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in element.EnumerateArray())
                    EnsureUnique(item);
            }
        }
    }

    public static string SerializeCanonical<T>(T value)
    {
        using var document = JsonDocument.Parse(Serialize(value));
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions
        {
            Indented = false,
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        }))
        {
            WriteCanonical(writer, document.RootElement);
        }
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    public static string Wire<T>(T value) where T : struct, Enum =>
        JsonNamingPolicy.SnakeCaseLower.ConvertName(value.ToString());

    public static string RequireText(JsonElement element, string property) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(property, out var value)
            ? RequireNonempty(value)
            : throw new JsonException($"Missing or invalid string property '{property}'.");

    public static string RequireNonempty(JsonElement value) =>
        value.ValueKind == JsonValueKind.String && value.GetString() is { Length: > 0 } text
            ? text
            : throw new JsonException("Expected a non-empty string.");

    public static void RequireKeys(JsonElement value, ReadOnlySpan<string> required, ReadOnlySpan<string> optional = default)
    {
        if (value.ValueKind != JsonValueKind.Object)
            throw new JsonException("Expected a JSON object.");

        foreach (var key in required)
        {
            if (!value.TryGetProperty(key, out _))
                throw new JsonException($"Missing required property '{key}'.");
        }

        foreach (var property in value.EnumerateObject())
        {
            if (!Contains(required, property.Name) && !Contains(optional, property.Name))
                throw new JsonException($"Unexpected property '{property.Name}'.");
        }
    }

    private static bool Contains(ReadOnlySpan<string> items, string candidate)
    {
        foreach (var item in items)
        {
            if (string.Equals(item, candidate, StringComparison.Ordinal))
                return true;
        }
        return false;
    }

    private static void WriteCanonical(Utf8JsonWriter writer, JsonElement value)
    {
        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                writer.WriteStartObject();
                foreach (var property in value.EnumerateObject()
                             .OrderBy(static property => property.Name, StringComparer.Ordinal))
                {
                    writer.WritePropertyName(property.Name);
                    WriteCanonical(writer, property.Value);
                }
                writer.WriteEndObject();
                break;
            case JsonValueKind.Array:
                writer.WriteStartArray();
                foreach (var item in value.EnumerateArray())
                    WriteCanonical(writer, item);
                writer.WriteEndArray();
                break;
            default:
                value.WriteTo(writer);
                break;
        }
    }
}

public enum InvalidRequestCode
{
    InvalidRequest,
}

public enum RejectOutcome
{
    Rejected,
}

public sealed record RequestValidationCause(InvalidRequestCode Code, string Message, ImmutableArray<string> Fields);

public sealed record InvalidRequestReply(RequestValidationCause Error)
{
    public RejectOutcome Status => RejectOutcome.Rejected;

    public static string Json(string message, params string[] fields) =>
        JsonContract.Serialize(new InvalidRequestReply(
            new RequestValidationCause(InvalidRequestCode.InvalidRequest, message, [.. fields])));
}
