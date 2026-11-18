using System.Collections.Immutable;

namespace Sts2Mod.Core.Observation;

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
    private static readonly string[] BandOrder = ["monster", "elite", "treasure", "shop", "event"];

    public static UnknownRoomOdds Compute(
        decimal monsterWeight,
        decimal eliteWeight,
        decimal treasureWeight,
        decimal shopWeight,
        ImmutableArray<string> allowedRoomTypes)
    {
        var allowed = allowedRoomTypes.ToHashSet(StringComparer.Ordinal);
        var ordered = BandOrder.Where(allowed.Contains).ToImmutableArray();
        var fallback = allowed.Contains("event")
            ? "event"
            : ordered.Length > 0 ? ordered[0] : "monster";

        var probabilities = new Dictionary<string, decimal>(StringComparer.Ordinal)
        {
            ["monster"] = 0m,
            ["elite"] = 0m,
            ["treasure"] = 0m,
            ["shop"] = 0m,
            ["event"] = 0m,
        };

        decimal cumulative = 0m;
        var bands = new[]
        {
            ("monster", monsterWeight),
            ("elite", eliteWeight),
            ("treasure", treasureWeight),
            ("shop", shopWeight),
        };

        foreach (var (name, weight) in bands)
        {
            if (!allowed.Contains(name) || weight < 0m)
                continue;
            var prior = Math.Min(cumulative, 1m);
            cumulative += weight;
            probabilities[name] += Math.Max(0m, Math.Min(cumulative, 1m) - prior);
        }

        probabilities[fallback] += Math.Max(0m, 1m - Math.Min(cumulative, 1m));

        return new UnknownRoomOdds(
            Rounded(probabilities["monster"]),
            Rounded(probabilities["elite"]),
            Rounded(probabilities["treasure"]),
            Rounded(probabilities["shop"]),
            Rounded(probabilities["event"]),
            ordered);
    }

    private static decimal Rounded(decimal value) =>
        Math.Round(value, 4, MidpointRounding.AwayFromZero);
}
