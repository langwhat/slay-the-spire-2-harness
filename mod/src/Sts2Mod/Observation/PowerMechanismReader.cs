using System.Collections.Concurrent;
using Godot;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Mod.Observation;

internal static class PowerMechanismReader
{
    private const string MultiplayerScalePrefix = "Applied stacks scaled by player count:";

    private sealed record CacheKey(
        string PowerId,
        string Language,
        int AscensionLevel,
        bool IsDread,
        MegaCrit.Sts2.Core.Runs.GameMode Mode,
        bool IncludeMultiplayerScale);

    private static readonly ConcurrentDictionary<CacheKey, string?> Cache = new();
    private static readonly ConcurrentDictionary<string, byte> LoggedGaps = new(StringComparer.Ordinal);

    public static string? Read(Type powerType, string language, bool includeMultiplayerScale = false)
    {
        if (!typeof(PowerModel).IsAssignableFrom(powerType) || powerType.IsAbstract)
            return null;

        string id;
        try
        {
            id = ModelDb.GetId(powerType).ToString();
        }
        catch
        {
            return null;
        }

        var runState = MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState();
        var ascension = runState?.AscensionLevel ?? 0;
        var mode = runState?.GameMode ?? MegaCrit.Sts2.Core.Runs.GameMode.Standard;
        var isDread = MegaCrit.Sts2.Core.Helpers.AscensionHelper.IsDread();
        var key = new CacheKey(id, language, ascension, isDread, mode, includeMultiplayerScale);
        if (Cache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            var (described, gaps) = Describe(powerType, includeMultiplayerScale);
            if (gaps.Count > 0)
            {
                if (LoggedGaps.TryAdd(id, 0))
                {
                    GD.PushWarning(
                        $"[sts2agent] Unresolved native power rules for {id}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");
                }
            }
            else
            {
                text = described;
            }
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd(id, 0))
                GD.PushWarning($"[sts2agent] Power mechanism unavailable for {id}: {error.Message}");
        }

        Cache[key] = text;
        return text;
    }

    internal static (string? Text, IReadOnlySet<string> Gaps) Describe(Type powerType, bool includeMultiplayerScale = false)
    {
        var reader = new NativeRuleText();
        var rawLines = new List<string>();
        reader.ReadHooks(powerType, rawLines);
        if (reader.Gaps.Count > 0)
            return (null, reader.Gaps);

        var prefix = NativeRuleText.ModelLabel(powerType) + " · ";
        var cleaned = new List<string>(rawLines.Count);
        foreach (var raw in rawLines)
        {
            var line = raw.StartsWith(prefix, StringComparison.Ordinal)
                ? raw[prefix.Length..]
                : raw;
            if (!includeMultiplayerScale && line.StartsWith(MultiplayerScalePrefix, StringComparison.Ordinal))
                continue;
            if (!string.IsNullOrWhiteSpace(line))
                cleaned.Add(line);
        }

        reader.ReadGeneratedCards(cleaned);
        if (reader.Gaps.Count > 0)
            return (null, reader.Gaps);

        return (cleaned.Count > 0 ? string.Join("\n", cleaned) : null, reader.Gaps);
    }
}
