using System.Collections.Concurrent;
using System.Reflection;
using System.Runtime.CompilerServices;
using Godot;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal static class EncounterMechanismReader
{
    private const BindingFlags DeclaredInstance = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.DeclaredOnly;
    private const BindingFlags AnyInstance = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
    private const string FirstMovePrefix = "First move: ";

    private sealed record CacheKey(string EncounterId, int AscensionLevel, GameMode Mode, string Language);

    private sealed record IntSetter(PropertyInfo Property, int? ConstantValue, Call? RngSource, int RngOffset, int[]? ExactDomain);

    private sealed record SpawnSpec(Type MonsterType, IReadOnlyList<IntSetter> Setters);

    private sealed record SpawnDescribe(
        Type MonsterType,
        string MonsterId,
        int MinHp,
        int MaxHp,
        string? RuleText);

    private sealed record CachedEncounter(string? Text, IReadOnlyList<Type> HoistedPowers);
    private static readonly IReadOnlyList<Type> EmptyHoistedPowers = [];
    private static readonly ConcurrentDictionary<CacheKey, CachedEncounter> Cache = new();
    private static readonly ConcurrentDictionary<string, byte> LoggedGaps = new(StringComparer.Ordinal);

    public static string? Read(EncounterModel encounter, int ascensionLevel, GameMode gameMode, string language)
        => ReadWithHoistedPowers(encounter, ascensionLevel, gameMode, language).Text;

    public static (string? Text, IReadOnlyList<Type> HoistedPowers) ReadWithHoistedPowers(
        EncounterModel encounter, int ascensionLevel, GameMode gameMode, string language)
    {
        var id = encounter.Id.ToString();
        var key = new CacheKey(id, ascensionLevel, gameMode, language);
        if (Cache.TryGetValue(key, out var cached))
            return (cached.Text, cached.HoistedPowers);

        string? text = null;
        IReadOnlyList<Type> hoistedPowers = EmptyHoistedPowers;
        try
        {
            // Unresolved native paths are extractor work items. They stay in the diagnostics log.
            (text, var gaps, hoistedPowers) = DescribeWithHoistedPowers(encounter);
            if (gaps.Count > 0 && LoggedGaps.TryAdd(id, 0))
            {
                GD.PushWarning(
                    $"[sts2agent] Unresolved native encounter rules for {id}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");
            }
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd(id, 0))
                GD.PushWarning($"[sts2agent] Encounter mechanism unavailable for {id}: {error.Message}");
        }

        Cache[key] = new CachedEncounter(text, hoistedPowers);
        return (text, hoistedPowers);
    }

    internal static (string? Text, IReadOnlySet<string> Gaps) Describe(EncounterModel encounter)
    {
        var (text, gaps, _) = DescribeWithHoistedPowers(encounter);
        return (text, gaps);
    }

    internal static (string? Text, IReadOnlySet<string> Gaps, IReadOnlyList<Type> HoistedPowers) DescribeWithHoistedPowers(EncounterModel encounter)
    {
        var gaps = new HashSet<string>(StringComparer.Ordinal);
        var hoisted = new HashSet<Type>();
        var generate = encounter.GetType().GetMethod("GenerateMonsters", DeclaredInstance, Type.EmptyTypes);
        if (generate is null)
        {
            gaps.Add("GenerateMonsters");
            return (null, gaps, EmptyHoistedPowers);
        }

        var body = NativeRuleBody.Read(generate);
        if (body.Incomplete)
        {
            gaps.Add("GenerateMonsters");
            return (null, gaps, EmptyHoistedPowers);
        }

        var spawns = ExtractSpawns(body, gaps);
        if (spawns.Count == 0)
        {
            gaps.Add("GenerateMonsters/empty");
            return (null, gaps, EmptyHoistedPowers);
        }
        if (gaps.Count > 0)
            return (null, gaps, EmptyHoistedPowers);

        var sequentialRngGroups = FindSequentialRngGroups(spawns);
        var described = new List<SpawnDescribe>(spawns.Count);
        foreach (var spawn in spawns)
        {
            var monster = CreateConfiguredMonster(spawn.MonsterType, spawn.Setters);
            var (ruleText, monsterGaps, _, monsterHoisted) = EnemyMechanismReader.DescribeWithPowers(monster);
            foreach (var gap in monsterGaps)
                gaps.Add($"{spawn.MonsterType.Name}/{gap}");
            hoisted.UnionWith(monsterHoisted);

            if (ruleText is not null && spawn.Setters.FirstOrDefault(setter => setter.ConstantValue is null) is { } rngSetter)
            {
                if (rngSetter.ExactDomain is { Length: > 0 } exactDomain)
                {
                    var isSequential = rngSetter.RngSource is not null && sequentialRngGroups.Contains((rngSetter.RngSource, rngSetter.Property.Name));
                    ruleText = ReplaceFirstMoveWithRngVariants(spawn, rngSetter.Property, exactDomain, isSequential, ruleText, gaps);
                }
                else
                {
                    gaps.Add($"GenerateMonsters/first-move-domain:{spawn.MonsterType.Name}.{rngSetter.Property.Name}");
                    ruleText = StripFirstMoveLine(ruleText);
                }
            }

            described.Add(new SpawnDescribe(
                spawn.MonsterType,
                monster.Id.ToString(),
                monster.MinInitialHp,
                monster.MaxInitialHp,
                ruleText));
        }

        var orderedHoisted = hoisted.OrderBy(t => ModelDb.GetId(t).ToString(), StringComparer.Ordinal).ToArray();
        var lineup = FormatLineup(described);
        var sections = FormatRuleSections(described);
        if (sections.Count == 0)
            return ($"Monsters: {lineup}", gaps, orderedHoisted);

        return ($"Monsters: {lineup}\n{string.Join("\n", sections)}", gaps, orderedHoisted);
    }

    private static List<SpawnSpec> ExtractSpawns(NativeRuleBody body, HashSet<string> gaps)
    {
        var spawns = new List<SpawnSpec>();
        var orderedSteps = body.Calls.OrderBy(step => step.Order).ThenBy(step => step.Index).ToArray();
        foreach (var step in orderedSteps)
        {
            if (step.Call is not { Method.Name: "ToMutable", Receiver: Call { Method: MethodInfo { Name: "Monster", IsGenericMethod: true } getter } } spawnCall)
                continue;
            var monsterType = getter.GetGenericArguments()[0];
            if (!typeof(MonsterModel).IsAssignableFrom(monsterType))
                continue;
            if (step.Conditions.Length > 0)
            {
                gaps.Add($"GenerateMonsters/conditional:{monsterType.Name}");
                continue;
            }

            var setters = new List<IntSetter>();
            foreach (var candidate in orderedSteps)
            {
                if (UnwrapCast(candidate.Call.Receiver) != spawnCall
                    || candidate.Call.Method is not MethodInfo { Name: var methodName } setterMethod
                    || !methodName.StartsWith("set_", StringComparison.Ordinal))
                {
                    continue;
                }

                if (candidate.Conditions.Length > 0 || candidate.Call.Arguments is not [var assigned])
                {
                    gaps.Add($"GenerateMonsters/setter:{monsterType.Name}.{methodName}");
                    continue;
                }

                var property = setterMethod.DeclaringType?.GetProperty(methodName[4..], AnyInstance);
                if (property is null || property.PropertyType != typeof(int) || !property.CanWrite)
                {
                    gaps.Add($"GenerateMonsters/setter:{monsterType.Name}.{methodName}");
                    continue;
                }

                if (assigned is Constant { Data: int constantInt })
                {
                    setters.Add(new IntSetter(property, constantInt, null, 0, null));
                }
                else if (TryReadExactRngDomain(assigned, out var rngSource, out var offset, out var exactDomain))
                {
                    setters.Add(new IntSetter(property, null, rngSource, offset, exactDomain));
                }
                else if (DependsOnRng(assigned))
                {
                    setters.Add(new IntSetter(property, null, null, 0, null));
                }
                else
                {
                    gaps.Add($"GenerateMonsters/setter:{monsterType.Name}.{methodName}");
                }
            }

            spawns.Add(new SpawnSpec(monsterType, setters));
        }

        return spawns;
    }

    private sealed class ReferencePairComparer : IEqualityComparer<(Call RngSource, string PropertyName)>
    {
        public static readonly ReferencePairComparer Instance = new();
        public bool Equals((Call RngSource, string PropertyName) x, (Call RngSource, string PropertyName) y)
            => ReferenceEquals(x.RngSource, y.RngSource) && StringComparer.Ordinal.Equals(x.PropertyName, y.PropertyName);
        public int GetHashCode((Call RngSource, string PropertyName) obj)
            => HashCode.Combine(RuntimeHelpers.GetHashCode(obj.RngSource), StringComparer.Ordinal.GetHashCode(obj.PropertyName));
    }

    private static HashSet<(Call RngSource, string PropertyName)> FindSequentialRngGroups(IReadOnlyList<SpawnSpec> spawns)
    {
        var groups = spawns
            .SelectMany(spawn => spawn.Setters
                .Where(setter => setter.ConstantValue is null && setter.RngSource is not null)
                .Select(setter => (RngSource: setter.RngSource!, PropertyName: setter.Property.Name, setter.RngOffset)))
            .GroupBy(entry => (entry.RngSource, entry.PropertyName), ReferencePairComparer.Instance);

        var result = new HashSet<(Call RngSource, string PropertyName)>(ReferencePairComparer.Instance);
        foreach (var group in groups)
        {
            var offsets = group.Select(entry => entry.RngOffset).Order().ToArray();
            if (offsets.Length > 1 && offsets.SequenceEqual(Enumerable.Range(0, offsets.Length)))
                result.Add(group.Key);
        }

        return result;
    }

    private static MonsterModel CreateConfiguredMonster(
        Type monsterType,
        IReadOnlyList<IntSetter> setters,
        (PropertyInfo Property, int Value)? overrideSetter = null)
    {
        var monster = ModelDb.GetById<MonsterModel>(ModelDb.GetId(monsterType)).ToMutable();
        foreach (var setter in setters)
        {
            if (setter.ConstantValue is { } value)
                setter.Property.SetValue(monster, value);
        }

        if (overrideSetter is var (property, overrideValue))
            property.SetValue(monster, overrideValue);

        monster.SetUpForCombat();
        return monster;
    }

    private static string ReplaceFirstMoveWithRngVariants(
        SpawnSpec spawn,
        PropertyInfo property,
        int[] exactDomain,
        bool sequentialAcrossCopies,
        string baselineText,
        HashSet<string> gaps)
    {
        var lines = baselineText.Split('\n');
        if (lines.Length == 0 || !lines[0].StartsWith(FirstMovePrefix, StringComparison.Ordinal))
            return baselineText;

        var firstMoves = new List<string>();
        foreach (var candidate in exactDomain)
        {
            var sample = CreateConfiguredMonster(spawn.MonsterType, spawn.Setters, (property, candidate));
            var (sampleText, sampleGaps) = EnemyMechanismReader.Describe(sample);
            foreach (var gap in sampleGaps)
                gaps.Add($"{spawn.MonsterType.Name}/{gap}");
            if (sampleText is null)
                return StripFirstMoveLine(baselineText);
            var firstLine = sampleText.Split('\n')[0];
            if (!firstLine.StartsWith(FirstMovePrefix, StringComparison.Ordinal))
                return StripFirstMoveLine(baselineText);
            var moveId = firstLine[FirstMovePrefix.Length..];
            if (!firstMoves.Contains(moveId, StringComparer.Ordinal))
                firstMoves.Add(moveId);
        }

        if (firstMoves.Count == 0)
            return StripFirstMoveLine(baselineText);
        if (firstMoves.Count == 1)
        {
            lines[0] = $"First move: {firstMoves[0]}";
            return string.Join("\n", lines);
        }

        var qualifier = sequentialAcrossCopies ? "distinct initial moves across copies from" : "one of";
        lines[0] = $"First move: {qualifier} {string.Join(", ", firstMoves)}";
        return string.Join("\n", lines);
    }

    private static string StripFirstMoveLine(string ruleText)
    {
        var lines = ruleText.Split('\n');
        if (lines.Length == 0 || !lines[0].StartsWith(FirstMovePrefix, StringComparison.Ordinal))
            return ruleText;
        return string.Join("\n", lines.Skip(1));
    }

    private static string FormatLineup(IReadOnlyList<SpawnDescribe> described)
    {
        var counts = new Dictionary<(Type MonsterType, string MonsterId, int MinHp, int MaxHp), int>();
        var order = new List<(Type MonsterType, string MonsterId, int MinHp, int MaxHp)>();
        foreach (var item in described)
        {
            var key = (item.MonsterType, item.MonsterId, item.MinHp, item.MaxHp);
            if (!counts.TryAdd(key, 1))
            {
                counts[key]++;
            }
            else
            {
                order.Add(key);
            }
        }

        var parts = new List<string>(order.Count);
        foreach (var key in order)
        {
            var count = counts[key];
            var hp = key.MinHp == key.MaxHp ? $"{key.MinHp} HP" : $"{key.MinHp}–{key.MaxHp} HP";
            var label = NativeRuleText.ModelLabel(key.MonsterType);
            var idSuffix = $" ({key.MonsterId})";
            parts.Add(label.EndsWith(idSuffix, StringComparison.Ordinal)
                ? $"{count}× {label[..^idSuffix.Length]} ({key.MonsterId}, {hp})"
                : $"{count}× {key.MonsterId} ({hp})");
        }

        return string.Join(", ", parts);
    }

    private static List<string> FormatRuleSections(IReadOnlyList<SpawnDescribe> described)
    {
        var perMonster = new List<(Type MonsterType, string RuleText)>();
        foreach (var group in described.Where(item => !string.IsNullOrWhiteSpace(item.RuleText)).GroupBy(item => item.MonsterType))
        {
            var texts = group.Select(item => item.RuleText!).ToArray();
            var distinct = texts.Distinct(StringComparer.Ordinal).ToArray();
            if (distinct.Length == 1)
            {
                perMonster.Add((group.Key, distinct[0]));
                continue;
            }

            if (TryMergeCopyFirstMoves(texts, out var merged))
            {
                perMonster.Add((group.Key, merged));
                continue;
            }

            foreach (var variant in distinct)
                perMonster.Add((group.Key, variant));
        }

        var sections = new List<string>();
        foreach (var shared in perMonster.GroupBy(item => item.RuleText, StringComparer.Ordinal))
        {
            var header = string.Join(" / ", shared.Select(item => NativeRuleText.ModelLabel(item.MonsterType)).Distinct(StringComparer.Ordinal));
            sections.Add($"[{header}]\n{shared.Key}");
        }

        return sections;
    }

    private static bool TryMergeCopyFirstMoves(IReadOnlyList<string> texts, out string merged)
    {
        merged = "";
        var split = texts.Select(text => text.Split('\n')).ToArray();
        if (split.Any(lines => lines.Length == 0 || !lines[0].StartsWith(FirstMovePrefix, StringComparison.Ordinal)))
            return false;

        var baselineTail = split[0].Skip(1).ToArray();
        var baselineSet = baselineTail.ToHashSet(StringComparer.Ordinal);
        if (split.Skip(1).Any(lines => lines.Length - 1 != baselineTail.Length || !baselineSet.SetEquals(lines.Skip(1))))
            return false;

        var tail = string.Join("\n", baselineTail);
        var moves = split.Select(lines => lines[0][FirstMovePrefix.Length..]).ToArray();
        var firstLine = $"First move (by copy): {string.Join(", ", moves)}";
        merged = string.IsNullOrEmpty(tail) ? firstLine : $"{firstLine}\n{tail}";
        return true;
    }

    private static Value? UnwrapCast(Value? value) => value switch
    {
        Operation { Operator: "castclass" or "isinst", Left: var inner } => UnwrapCast(inner),
        _ => value,
    };

    private const int MaxExactDomainSize = 32;

    internal static bool TryReadExactRngDomain(Value value, out Call? rngSource, out int offset, out int[] exactDomain)
    {
        rngSource = null;
        offset = 0;
        exactDomain = [];
        switch (value)
        {
            case Call { Method.Name: "NextInt", Receiver: Call { Method.Name: "get_Rng" } } rngCall:
                rngSource = rngCall;
                if (rngCall.Arguments is [Constant { Data: int maxExclusive }] && maxExclusive > 0 && maxExclusive <= MaxExactDomainSize)
                {
                    exactDomain = Enumerable.Range(0, maxExclusive).ToArray();
                    return true;
                }
                if (rngCall.Arguments is [Constant { Data: int minInclusive }, Constant { Data: int upperExclusive }]
                    && upperExclusive > minInclusive && (upperExclusive - minInclusive) <= MaxExactDomainSize)
                {
                    exactDomain = Enumerable.Range(minInclusive, upperExclusive - minInclusive).ToArray();
                    return true;
                }
                return false;
            case Call { Method.Name: "NextBool", Receiver: Call { Method.Name: "get_Rng" } } boolCall:
                rngSource = boolCall;
                exactDomain = [0, 1];
                return true;
            case Operation { Operator: "add", Left: var left, Right: Constant { Data: int delta } }
                when TryReadExactRngDomain(left, out rngSource, out var innerOffset, out var innerDomain):
                offset = innerOffset + delta;
                exactDomain = innerDomain.Select(v => v + delta).Distinct().ToArray();
                return true;
            case Operation { Operator: "sub", Left: var left, Right: Constant { Data: int subDelta } }
                when TryReadExactRngDomain(left, out rngSource, out var innerSubOffset, out var innerSubDomain):
                offset = innerSubOffset - subDelta;
                exactDomain = innerSubDomain.Select(v => v - subDelta).Distinct().ToArray();
                return true;
            case Operation { Operator: "rem", Left: var left, Right: Constant { Data: int modulus } }
                when modulus > 0 && TryReadExactRngDomain(left, out rngSource, out var innerModOffset, out var innerModDomain):
                offset = ((innerModOffset % modulus) + modulus) % modulus;
                exactDomain = innerModDomain.Select(v => v % modulus).Distinct().ToArray();
                return true;
            default:
                return false;
        }
    }

    private static bool DependsOnRng(Value? value) => value switch
    {
        Call { Method.Name: "get_Rng" or "NextInt" or "NextBool" or "NextFloat" } => true,
        Call call => DependsOnRng(call.Receiver) || call.Arguments.Any(DependsOnRng),
        Field field => DependsOnRng(field.Receiver),
        Operation operation => DependsOnRng(operation.Left) || DependsOnRng(operation.Right),
        _ => false,
    };
}
