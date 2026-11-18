using System.Text.Json;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Developer;

[JsonConverter(typeof(StandardCharacterConverter))]
public enum StandardCharacter
{
    Ironclad,
    Silent,
    Regent,
    Necrobinder,
    Defect,
    Fool,
}

public sealed class StandardCharacterConverter : JsonConverter<StandardCharacter>
{
    public override StandardCharacter Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
        reader.TokenType == JsonTokenType.String
            ? reader.GetString() switch
            {
                "IRONCLAD" => StandardCharacter.Ironclad,
                "SILENT" => StandardCharacter.Silent,
                "REGENT" => StandardCharacter.Regent,
                "NECROBINDER" => StandardCharacter.Necrobinder,
                "DEFECT" => StandardCharacter.Defect,
                "FOOL" => StandardCharacter.Fool,
                _ => throw new JsonException("Unsupported headless character."),
            }
            : throw new JsonException("Expected a string character identifier.");

    public override void Write(Utf8JsonWriter writer, StandardCharacter value, JsonSerializerOptions options) =>
        writer.WriteStringValue(value switch
        {
            StandardCharacter.Ironclad => "IRONCLAD",
            StandardCharacter.Silent => "SILENT",
            StandardCharacter.Regent => "REGENT",
            StandardCharacter.Necrobinder => "NECROBINDER",
            StandardCharacter.Defect => "DEFECT",
            StandardCharacter.Fool => "FOOL",
            _ => throw new JsonException("Unknown standard character."),
        });
}

public enum HeadlessStartOutcome
{
    Completed,
}

/// <summary>
/// Immutable game-neutral snapshot of main-menu readiness captured on the game thread.
/// </summary>
public readonly record struct MainMenuReadinessObservation(
    bool GamePresent,
    bool MainMenuPresent,
    bool MainMenuValid,
    bool MainMenuInsideTree,
    bool MainMenuVisibleInTree,
    bool SubmenuStackPresent,
    bool SubmenusOpen,
    bool TransitionInProgress);

public static class MainMenuReadinessRules
{
    public static bool IsRunStartReady(MainMenuReadinessObservation observation) =>
        observation.MainMenuPresent
        && observation.MainMenuValid
        && observation.MainMenuInsideTree
        && observation.MainMenuVisibleInTree
        && observation.SubmenuStackPresent
        && !observation.SubmenusOpen;

    public static bool IsMenuSettled(MainMenuReadinessObservation observation) =>
        observation.GamePresent
        && !observation.TransitionInProgress
        && IsRunStartReady(observation);
}

/// <summary>
/// One-shot headless run recipe read from <c>STS2AGENT_START_RUN_FILE</c>.
/// </summary>
public sealed record HeadlessRunConfiguration(
    [property: JsonRequired] StandardCharacter Character,
    int Ascension = 0,
    string? Seed = null)
{
    public static HeadlessRunConfiguration Parse(string json)
    {
        try
        {
            var value = JsonContract.DeserializeUnique<HeadlessRunConfiguration>(json)
                        ?? throw new ArgumentException("Headless run configuration cannot be empty.");
            if (!Enum.IsDefined(value.Character)
                || value.Ascension is < 0 or > 10
                || value.Seed is { Length: 0 })
            {
                throw new ArgumentException("Invalid headless run character, ascension level, or seed.");
            }
            return value;
        }
        catch (JsonException error)
        {
            throw new ArgumentException("Invalid headless run configuration JSON.", error);
        }
    }
}

/// <summary>
/// Verified start receipt written to <c>&lt;start_file&gt;.result.json</c> for <c>sts2_harness.headless</c>.
/// </summary>
public sealed record HeadlessStartReceipt(
    HeadlessStartOutcome Status,
    bool EffectVerified,
    string? ReasonCode,
    string Seed)
{
    public static HeadlessStartReceipt Completed(string seed) =>
        new(HeadlessStartOutcome.Completed, true, null, seed);
}
