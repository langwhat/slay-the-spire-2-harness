using System.Collections.Immutable;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Observation;

public enum UnknownRoomKind
{
    Monster,
    Elite,
    Treasure,
    Shop,
    Event,
}

public sealed record UnknownRoomOdds(
    decimal Monster,
    decimal Elite,
    decimal Treasure,
    decimal Shop,
    decimal Event,
    ImmutableArray<string> AllowedRoomTypes)
{
    public MapOdds ToMapOdds() => new(Monster, Elite, Event, Shop, Treasure);
}

/// <summary>
/// Pure probability calculation matching native <c>UnknownMapPointOdds.Roll</c> without advancing RNG.
/// </summary>
public static class UnknownRoomOddsMath
{
    private static readonly UnknownRoomKind[] BandOrder =
    [
        UnknownRoomKind.Monster,
        UnknownRoomKind.Elite,
        UnknownRoomKind.Treasure,
        UnknownRoomKind.Shop,
        UnknownRoomKind.Event,
    ];

    public static UnknownRoomOdds Compute(
        decimal monsterWeight,
        decimal eliteWeight,
        decimal treasureWeight,
        decimal shopWeight,
        ImmutableArray<string> allowedRoomTypes)
    {
        var allowed = new HashSet<UnknownRoomKind>();
        foreach (var raw in allowedRoomTypes)
        {
            if (TryParseKind(raw, out var kind))
                allowed.Add(kind);
        }

        var ordered = BandOrder.Where(allowed.Contains).ToImmutableArray();
        var fallback = allowed.Contains(UnknownRoomKind.Event)
            ? UnknownRoomKind.Event
            : ordered.Length > 0 ? ordered[0] : UnknownRoomKind.Monster;

        var probabilities = new Dictionary<UnknownRoomKind, decimal>
        {
            [UnknownRoomKind.Monster] = 0m,
            [UnknownRoomKind.Elite] = 0m,
            [UnknownRoomKind.Treasure] = 0m,
            [UnknownRoomKind.Shop] = 0m,
            [UnknownRoomKind.Event] = 0m,
        };

        decimal cumulative = 0m;
        var bands = new[]
        {
            (UnknownRoomKind.Monster, monsterWeight),
            (UnknownRoomKind.Elite, eliteWeight),
            (UnknownRoomKind.Treasure, treasureWeight),
            (UnknownRoomKind.Shop, shopWeight),
        };

        foreach (var (kind, weight) in bands)
        {
            if (!allowed.Contains(kind) || weight < 0m)
                continue;
            var prior = Math.Min(cumulative, 1m);
            cumulative += weight;
            probabilities[kind] += Math.Max(0m, Math.Min(cumulative, 1m) - prior);
        }

        probabilities[fallback] += Math.Max(0m, 1m - Math.Min(cumulative, 1m));

        return new UnknownRoomOdds(
            Rounded(probabilities[UnknownRoomKind.Monster]),
            Rounded(probabilities[UnknownRoomKind.Elite]),
            Rounded(probabilities[UnknownRoomKind.Treasure]),
            Rounded(probabilities[UnknownRoomKind.Shop]),
            Rounded(probabilities[UnknownRoomKind.Event]),
            ordered.Select(JsonContract.Wire).ToImmutableArray());
    }

    private static bool TryParseKind(string value, out UnknownRoomKind kind)
    {
        switch (value)
        {
            case "monster":
                kind = UnknownRoomKind.Monster;
                return true;
            case "elite":
                kind = UnknownRoomKind.Elite;
                return true;
            case "treasure":
                kind = UnknownRoomKind.Treasure;
                return true;
            case "shop":
                kind = UnknownRoomKind.Shop;
                return true;
            case "event":
                kind = UnknownRoomKind.Event;
                return true;
            default:
                kind = default;
                return false;
        }
    }

    private static decimal Rounded(decimal value) =>
        Math.Round(value, 4, MidpointRounding.AwayFromZero);
}
