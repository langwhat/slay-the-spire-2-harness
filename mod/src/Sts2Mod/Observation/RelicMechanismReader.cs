using System.Collections.Concurrent;
using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Models;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal static class RelicMechanismReader
{
    private const BindingFlags DeclaredInstance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

    private sealed record CacheKey(string RelicId, string Language, int AscensionLevel, MegaCrit.Sts2.Core.Runs.GameMode Mode);

    private static readonly ConcurrentDictionary<CacheKey, string?> Cache = new();
    private static readonly ConcurrentDictionary<Type, Type[]> ReferencedPowerCache = new();
    private static readonly ConcurrentDictionary<string, byte> LoggedErrors = new(StringComparer.Ordinal);

    public static string? Read(RelicModel relic, string language)
    {
        var runState = MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState();
        var key = new CacheKey(
            relic.Id.ToString(),
            language,
            runState?.AscensionLevel ?? 0,
            runState?.GameMode ?? MegaCrit.Sts2.Core.Runs.GameMode.Standard);
        if (Cache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            if (ItemMechanismReader.DescribeRelic(relic) is { } curated)
            {
                text = curated;
            }
            else
            {
                var (described, gaps) = Describe(relic.GetType());
                if (gaps.Count == 0 && !string.IsNullOrWhiteSpace(described))
                    text = described;
            }
        }
        catch (Exception error)
        {
            var id = relic.Id.ToString();
            if (LoggedErrors.TryAdd(id, 0))
                Godot.GD.PushWarning($"[sts2agent] Relic mechanism unavailable for {id}: {error.Message}");
        }

        Cache[key] = text;
        return text;
    }

    public static Type[] ReferencedPowers(RelicModel relic)
        => ReferencedPowers(relic.GetType());

    internal static Type[] ReferencedPowers(Type relicType)
    {
        if (ReferencedPowerCache.TryGetValue(relicType, out var cached))
            return cached;

        var powers = new HashSet<Type>();
        try
        {
            foreach (var method in relicType.GetMethods(DeclaredInstance))
            {
                if (method.IsSpecialName || method.ContainsGenericParameters || method.ReturnType != typeof(Task))
                    continue;
                var body = NativeRuleBody.Read(method);
                foreach (var step in body.Calls)
                {
                    if (step.Call.Method.DeclaringType == typeof(PowerCmd)
                        && step.Call.Method.Name == nameof(PowerCmd.Apply)
                        && step.Call.Method.IsGenericMethod
                        && step.Call.Method.GetGenericArguments()[0] is { } pt
                        && typeof(PowerModel).IsAssignableFrom(pt)
                        && PowerMechanismReader.IsStandaloneReadable(pt))
                    {
                        powers.Add(pt);
                    }
                }
            }
        }
        catch
        {
            // Ignore unreadable relic methods; hover tips still provide fallback material.
        }

        var result = powers.OrderBy(t => t.FullName, StringComparer.Ordinal).ToArray();
        ReferencedPowerCache[relicType] = result;
        return result;
    }

    internal static (string? Text, IReadOnlySet<string> Gaps) Describe(Type relicType)
    {
        if (!typeof(RelicModel).IsAssignableFrom(relicType) || relicType.IsAbstract)
            return (null, new HashSet<string>());

        var reader = new NativeRuleText();
        var rawLines = new List<string>();
        reader.ReadHooks(relicType, rawLines);
        if (reader.Gaps.Count > 0)
            return (null, reader.Gaps);

        var cleaned = new List<string>(rawLines.Count);
        foreach (var line in rawLines)
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0)
                continue;
            // Drop purely end-of-combat internal counter resets that add noise once combat has already ended.
            if (trimmed.StartsWith("After combat ends:", StringComparison.Ordinal)
                && !trimmed.Contains("heal", StringComparison.OrdinalIgnoreCase)
                && !trimmed.Contains("Gold", StringComparison.Ordinal)
                && !trimmed.Contains("card", StringComparison.OrdinalIgnoreCase)
                && !trimmed.Contains("HP", StringComparison.Ordinal))
            {
                continue;
            }
            if (!cleaned.Contains(trimmed, StringComparer.Ordinal))
                cleaned.Add(trimmed);
        }

        return cleaned.Count == 0
            ? (null, reader.Gaps)
            : (string.Join("\n", cleaned), reader.Gaps);
    }
}
