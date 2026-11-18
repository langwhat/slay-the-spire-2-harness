using System.Collections.Immutable;
using System.Text.Json;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Observation;

public enum NativeInspectArea
{
    Hand,
    Draw,
    Discard,
    Exhaust,
    Other,
    Deck,
    Options,
    Relics,
    Potions,
    Enemies,
    Powers,
    Map,
    CombatHistory,
    Rewards,
    Shop,
}

public enum InspectErrorCode
{
    InvalidRequest,
    InvalidRefs,
    AreaUnavailable,
}

public sealed record NativeInspectQuery
{
    public required NativeInspectArea Area { get; init; }
    public ImmutableArray<string> Refs { get; init; }
    public bool? Upgrade { get; init; }
}

public sealed record NativeInspectRequest
{
    public required NativeInspectQuery Query { get; init; }
}

public sealed record InspectionCause(InspectErrorCode Code, string Message)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<string>? Fields { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<string>? InvalidRefs { get; init; }
}

/// <summary>
/// Serves <c>/state</c> and <c>/inspect</c> from a single immutable publication without allocating identities.
/// </summary>
public static class ObservationReader
{
    public static string Read(NativeReadPublication publication) =>
        publication.Reply is CompletedStateRead completed
            ? SemanticText.Json(JsonContract.Serialize(publication.Reply), completed.Observation.GameLanguage)
            : JsonContract.Serialize(publication.Reply);

    public static (int StatusCode, string Json) Inspect(NativeReadPublication publication, string body)
    {
        NativeInspectQuery query;
        try
        {
            query = Decode(body);
        }
        catch (JsonException)
        {
            return (400, JsonContract.Serialize(new
            {
                status = ReadOutcome.Rejected,
                error = new InspectionCause(
                    InspectErrorCode.InvalidRequest,
                    "The query does not match the inspection contract.")
                {
                    Fields = ["query"],
                },
            }));
        }

        if (publication.Reply is FailedStateRead failed)
        {
            var error = failed.Error.Code == NativeReadError.NoRun
                ? new ReadCause(NativeReadError.ReadUnavailable, "No native observation is available outside a run.")
                : failed.Error;
            return (200, JsonContract.Serialize(new
            {
                status = ReadOutcome.Failed,
                observation = (NativeObservation?)null,
                error,
            }));
        }

        var observation = ((CompletedStateRead)publication.Reply).Observation;
        if (query.Area == NativeInspectArea.Map && observation.Map is { } map)
            return Completed(observation, new MapInspectionData(map));
        if (query.Area == NativeInspectArea.CombatHistory && publication.History is { } history)
            return Completed(observation, history);

        var members = AreaMembers(publication, observation, query.Area);
        if (members is null)
        {
            return Rejected(observation, new InspectionCause(
                InspectErrorCode.AreaUnavailable,
                "This area is not available in the current native publication."));
        }

        var selected = query.Refs.IsDefault ? members.Value : query.Refs;
        var invalid = selected
            .Where(reference => !members.Value.Contains(reference, StringComparer.Ordinal))
            .ToImmutableArray();
        if (!invalid.IsEmpty)
        {
            return Rejected(observation, new InspectionCause(
                InspectErrorCode.InvalidRefs,
                "The requested references do not belong to this area.")
            {
                InvalidRefs = invalid,
            });
        }

        if (observation.State is RunStateView run && query.Area is NativeInspectArea.Rewards or NativeInspectArea.Shop)
        {
            selected = selected
                .SelectMany(reference => run.Offers.FirstOrDefault(offer => offer.Ref == reference) is { } offer
                    ? offer.Contents.Prepend(reference)
                    : [reference])
                .Distinct(StringComparer.Ordinal)
                .ToImmutableArray();
        }

        selected = selected
            .SelectMany(reference => observation.Objects.TryGetValue(reference, out var value) && value is BundleEntry bundle
                ? bundle.Members.Prepend(reference)
                : [reference])
            .Distinct(StringComparer.Ordinal)
            .ToImmutableArray();

        var cards = selected
            .Where(reference => observation.Objects.TryGetValue(reference, out var value) && value is CardEntry)
            .ToImmutableArray();
        var upgrades = publication.InspectionUpgrades;
        if (query.Upgrade == true && cards.Any(reference => !upgrades.ContainsKey(reference)))
        {
            return (200, SemanticText.Json(JsonContract.Serialize(new
            {
                status = ReadOutcome.Failed,
                observation,
                error = new ReadCause(NativeReadError.ReadUnavailable, "The native upgrade details could not be captured."),
            }), observation.GameLanguage));
        }

        var data = new DetailInspectionData(
            Filter(observation.Objects, selected),
            Filter(observation.Descriptions, selected),
            Filter(observation.ModifierDescriptions, selected),
            observation.Generated
                .Where(pair => selected.Contains(pair.Key, StringComparer.Ordinal))
                .ToImmutableDictionary(StringComparer.Ordinal),
            observation.Mechanisms
                .Where(item => selected.Contains(item.Owner, StringComparer.Ordinal))
                .ToImmutableArray())
        {
            Upgrades = query.Upgrade == true ? Filter(upgrades, cards) : null,
        };
        return Completed(observation, data);
    }

    private static NativeInspectQuery Decode(string body)
    {
        var request = JsonContract.DeserializeUnique<NativeInspectRequest>(body) ?? throw new JsonException();
        var query = request.Query ?? throw new JsonException();
        using var document = JsonDocument.Parse(body);
        var raw = document.RootElement.GetProperty("query");
        if (raw.GetProperty("area").GetString() != JsonContract.Wire(query.Area))
            throw new JsonException();

        var cardArea = query.Area is NativeInspectArea.Hand
            or NativeInspectArea.Draw
            or NativeInspectArea.Discard
            or NativeInspectArea.Exhaust
            or NativeInspectArea.Other
            or NativeInspectArea.Deck
            or NativeInspectArea.Options
            or NativeInspectArea.Rewards
            or NativeInspectArea.Shop;
        var refsArea = cardArea || query.Area is NativeInspectArea.Relics or NativeInspectArea.Potions or NativeInspectArea.Enemies;

        if (raw.TryGetProperty("upgrade", out var upgrade)
            && (!cardArea || upgrade.ValueKind is not (JsonValueKind.True or JsonValueKind.False)))
        {
            throw new JsonException();
        }

        if (raw.TryGetProperty("refs", out _)
            && (!refsArea
                || query.Refs.IsDefaultOrEmpty
                || query.Refs.Any(string.IsNullOrEmpty)
                || query.Refs.Distinct(StringComparer.Ordinal).Count() != query.Refs.Length))
        {
            throw new JsonException();
        }

        return query;
    }

    private static ImmutableArray<string>? AreaMembers(
        NativeReadPublication publication,
        NativeObservation observation,
        NativeInspectArea area) => area switch
    {
        NativeInspectArea.Hand when observation.State is CombatStateView combat => combat.Piles.Hand,
        NativeInspectArea.Draw when observation.State is CombatStateView combat => combat.Piles.Draw,
        NativeInspectArea.Discard when observation.State is CombatStateView combat => combat.Piles.Discard,
        NativeInspectArea.Exhaust when observation.State is CombatStateView combat => combat.Piles.Exhaust,
        NativeInspectArea.Other when observation.State is CombatStateView combat => combat.Piles.Other,
        NativeInspectArea.Deck => publication.Deck.IsDefault ? null : publication.Deck,
        NativeInspectArea.Options when observation.State.Selection is { } selection => selection.Candidates,
        NativeInspectArea.Options when observation.State is RunStateView { Screen: RunScreen.Event or RunScreen.Rest } options =>
            options.Actions
                .Select(action => action.Subject)
                .OfType<string>()
                .Where(reference => observation.Objects.TryGetValue(reference, out var value) && value is OptionEntry)
                .Distinct(StringComparer.Ordinal)
                .ToImmutableArray(),
        NativeInspectArea.Relics => observation.State.Relics,
        NativeInspectArea.Potions => observation.State.Potions,
        NativeInspectArea.Enemies when observation.State is CombatStateView combat =>
            combat.Enemies.Concat(combat.InactiveEnemies).Select(enemy => enemy.Ref).ToImmutableArray(),
        NativeInspectArea.Powers when observation.State is CombatStateView =>
            observation.Objects
                .Where(pair => pair.Value is EnemyEntry or AllyEntry)
                .Select(pair => pair.Key)
                .Prepend("player")
                .ToImmutableArray(),
        NativeInspectArea.Rewards when observation.State is RunStateView { Screen: RunScreen.Rewards } rewards => OfferMembers(rewards),
        NativeInspectArea.Shop when observation.State is RunStateView { Screen: RunScreen.Shop } shop => OfferMembers(shop),
        _ => null,
    };

    private static ImmutableArray<string> OfferMembers(RunStateView state) =>
        state.Offers
            .SelectMany(offer => offer.Contents.Prepend(offer.Ref))
            .Distinct(StringComparer.Ordinal)
            .ToImmutableArray();

    private static ImmutableDictionary<string, T> Filter<T>(
        ImmutableDictionary<string, T> directory,
        ImmutableArray<string> selected) =>
        directory
            .Where(pair => selected.Contains(pair.Key, StringComparer.Ordinal))
            .ToImmutableDictionary(StringComparer.Ordinal);

    private static (int StatusCode, string Json) Completed<T>(NativeObservation observation, T data)
        where T : InspectionData =>
        (200, SemanticText.Json(
            JsonContract.Serialize(new { status = ReadOutcome.Completed, observation, data }),
            observation.GameLanguage));

    private static (int StatusCode, string Json) Rejected(NativeObservation observation, InspectionCause error) =>
        (200, SemanticText.Json(
            JsonContract.Serialize(new { status = ReadOutcome.Rejected, observation, error }),
            observation.GameLanguage));
}
