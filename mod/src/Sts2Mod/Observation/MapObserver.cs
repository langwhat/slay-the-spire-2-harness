using System.Collections.Immutable;
using MegaCrit.Sts2.Core.Hooks;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Observation;

namespace Sts2Mod.Observation;

/// <summary>
/// Builds deterministic <see cref="PublicMap"/> views directly from native <see cref="RunState"/> and <see cref="ActMap"/>.
/// </summary>
internal static class MapObserver
{
    public static string NodePosition(MapCoord coord) => $"map:{coord.row}:{coord.col}";

    public static string? CurrentNodePosition(RunState run) =>
        run.CurrentMapCoord is { } coord ? NodePosition(coord) : null;

    public static PublicMap? Observe(RunState run)
    {
        var map = run.Map;
        if (map is null or NullActMap)
            return null;

        var bosses = ImmutableArray.CreateBuilder<NamedDefinition>();
        bosses.Add(new NamedDefinition(
            run.Act.BossEncounter.Title.GetFormattedText(),
            run.Act.BossEncounter.Id.ToString()));
        if (map.SecondBossMapPoint is not null && run.Act.SecondBossEncounter is { } secondBoss)
        {
            bosses.Add(new NamedDefinition(
                secondBoss.Title.GetFormattedText(),
                secondBoss.Id.ToString()));
        }

        var allPoints = CollectAllPoints(map);
        var currentOdds = CaptureCurrentUnknownRoomOdds(run);

        var nodes = allPoints
            .OrderBy(point => point.coord.row)
            .ThenBy(point => point.coord.col)
            .Select(point =>
            {
                var children = point.Children
                    .OrderBy(child => child.coord.row)
                    .ThenBy(child => child.coord.col)
                    .Select(child => NodePosition(child.coord))
                    .Distinct(StringComparer.Ordinal)
                    .ToImmutableArray();

                return new PublicMapNode(
                    NodePosition(point.coord),
                    point.PointType.ToString().ToLowerInvariant(),
                    children)
                {
                    Odds = point.PointType == MapPointType.Unknown ? currentOdds : null,
                };
            })
            .ToImmutableArray();

        return new PublicMap(bosses.ToImmutable(), nodes)
        {
            LegalDestinations = ObserveLegalDestinations(run, map),
        };
    }

    public static IReadOnlyList<MapPoint> GetTravelablePointsFrom(IRunState run, MapPoint currentPoint)
    {
        var points = MapTravel.GetTravelablePointsFrom(run, currentPoint).ToList();
        if (Hook.ShouldAllowFreeTravel(run)
            && currentPoint.coord.row == run.Map.GetRowCount() - 1
            && !points.Contains(run.Map.BossMapPoint))
        {
            points.Add(run.Map.BossMapPoint);
        }
        return points;
    }

    private static List<MapPoint> CollectAllPoints(ActMap map)
    {
        var seen = new HashSet<MapCoord>();
        var points = new List<MapPoint>();

        void Add(MapPoint? point)
        {
            if (point is not null && seen.Add(point.coord))
                points.Add(point);
        }

        Add(map.StartingMapPoint);
        foreach (var point in map.GetAllMapPoints())
            Add(point);
        Add(map.BossMapPoint);
        Add(map.SecondBossMapPoint);
        return points;
    }

    private static ImmutableArray<string>? ObserveLegalDestinations(RunState run, ActMap map)
    {
        if (NMapScreen.Instance is not { IsOpen: true, IsTravelEnabled: true })
            return null;

        IEnumerable<MapPoint> reachable;
        if (run.VisitedMapCoords.Count == 0)
        {
            reachable = map.StartingMapPoint is { } start ? [start] : [];
        }
        else
        {
            var last = run.VisitedMapCoords[^1];
            if (map.SecondBossMapPoint is { } secondBoss && map.BossMapPoint is { } boss && last == boss.coord)
            {
                reachable = [secondBoss];
            }
            else if (last.row == map.GetRowCount() - 1 && map.BossMapPoint is { } bossPoint)
            {
                reachable = [bossPoint];
            }
            else if (run.CurrentMapPoint is { } current && current.coord == last)
            {
                reachable = GetTravelablePointsFrom(run, current);
            }
            else
            {
                var match = CollectAllPoints(map).FirstOrDefault(point => point.coord == last);
                reachable = match is not null ? GetTravelablePointsFrom(run, match) : [];
            }
        }

        return reachable
            .OrderBy(point => point.coord.row)
            .ThenBy(point => point.coord.col)
            .Select(point => NodePosition(point.coord))
            .Distinct(StringComparer.Ordinal)
            .ToImmutableArray();
    }

    private static MapOdds CaptureCurrentUnknownRoomOdds(RunState run)
    {
        var previousEntry = run.CurrentMapPointHistoryEntry;
        var nextPoints = run.CurrentMapPoint?.Children ?? (IReadOnlyCollection<MapPoint>)[];
        var blacklist = RunManager.BuildRoomTypeBlacklist(previousEntry, nextPoints);
        var baseTypes = new HashSet<RoomType>
        {
            RoomType.Monster,
            RoomType.Elite,
            RoomType.Treasure,
            RoomType.Shop,
            RoomType.Event,
        };
        baseTypes.ExceptWith(blacklist);
        var modified = Hook.ModifyUnknownMapPointRoomTypes(run, baseTypes);
        var allowed = modified
            .Select(type => type.ToString().ToLowerInvariant())
            .ToImmutableArray();

        var odds = run.Odds.UnknownMapPoint;
        return UnknownRoomOddsMath.Compute(
            (decimal)odds.MonsterOdds,
            (decimal)odds.EliteOdds,
            (decimal)odds.TreasureOdds,
            (decimal)odds.ShopOdds,
            allowed).ToMapOdds();
    }
}
