using System.Collections;
using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Runtime.CompilerServices;
using HarmonyLib;
using Godot;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.MonsterMoves.MonsterMoveStateMachine;
using Sts2Mod.Core.Observation;

namespace Sts2Mod.Observation;

internal static class EnemyMechanismReader
{
    private const string UnsetMoveId = "UNSET_MOVE";
    private sealed record Readout(
        string Language,
        string? Text,
        IReadOnlySet<string> InlinePowerIds,
        IReadOnlyList<Type> HoistedPowers,
        MechanismCompleteness Completeness);
    private static readonly ConditionalWeakTable<MonsterMoveStateMachine, Readout> Readouts = new();
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, byte> LoggedGaps = new(StringComparer.Ordinal);
    private static readonly IReadOnlySet<string> EmptyPowers = new HashSet<string>(StringComparer.Ordinal);
    private static readonly IReadOnlyList<Type> EmptyHoistedPowers = [];
    private static readonly FieldInfo? InitialState = AccessTools.Field(typeof(MonsterMoveStateMachine), "_initialState");
    private static readonly FieldInfo? PerformMove = AccessTools.Field(typeof(MoveState), "_onPerform");
    private static readonly PropertyInfo? ConditionalStates = AccessTools.Property(typeof(ConditionalBranchState), "States");
    private static long _coldExtractionCount;
    private static long _coldExtractionTicks;
    private static long _cachedExtractionCount;
    private static long _cachedExtractionTicks;

    internal static (long ColdCount, double ColdMs, long CachedCount, double CachedMs) ReadLatencyMetrics() =>
        (
            System.Threading.Interlocked.Read(ref _coldExtractionCount),
            System.Threading.Interlocked.Read(ref _coldExtractionTicks) * 1000.0 / Stopwatch.Frequency,
            System.Threading.Interlocked.Read(ref _cachedExtractionCount),
            System.Threading.Interlocked.Read(ref _cachedExtractionTicks) * 1000.0 / Stopwatch.Frequency);

    private static void PushWarning(string message)
    {
        if (MegaCrit.Sts2.Core.Nodes.NGame.Instance is not null)
            GD.PushWarning(message);
    }

    internal static MechanismCompleteness ClassifyCompleteness(string? text, int gapCount, bool failed = false)
    {
        if (failed)
            return MechanismCompleteness.Unavailable;
        if (gapCount > 0)
            return text is not null ? MechanismCompleteness.Partial : MechanismCompleteness.Unavailable;
        return MechanismCompleteness.Complete;
    }

    internal static bool IsPublicMove(MoveState move)
        => move.StateId != UnsetMoveId
            && move.Intents.Count > 0
            && !move.Intents.Any(intent => intent.IntentType is IntentType.Hidden or IntentType.Unknown);

    public static string? Read(Creature creature, string language)
        => ReadWithPowers(creature, language).Text;

    public static (string? Text, IReadOnlySet<string> InlinePowerIds, IReadOnlyList<Type> HoistedPowers, MechanismCompleteness Completeness) ReadWithPowers(Creature creature, string language)
    {
        var started = Stopwatch.GetTimestamp();
        MonsterMoveStateMachine? machine = null;
        string monsterId = "unknown";
        try
        {
            if (creature.Monster is not { MoveStateMachine: { } stateMachine } monster)
                return (null, EmptyPowers, EmptyHoistedPowers, MechanismCompleteness.Complete);
            machine = stateMachine;
            monsterId = monster.Id.ToString();
            if (monster.NextMove.StateId == UnsetMoveId)
                return (null, EmptyPowers, EmptyHoistedPowers, MechanismCompleteness.Complete);

            if (Readouts.TryGetValue(machine, out var cached) && cached.Language == language)
            {
                System.Threading.Interlocked.Increment(ref _cachedExtractionCount);
                System.Threading.Interlocked.Add(ref _cachedExtractionTicks, Stopwatch.GetTimestamp() - started);
                return (cached.Text, cached.InlinePowerIds, cached.HoistedPowers, cached.Completeness);
            }

            // Unresolved native paths are extractor work items. They stay in the diagnostics log;
            // completeness tells the consumer when a readout omits unresolved paths.
            var (text, gaps, inlinePowerIds, hoistedPowers) = DescribeWithPowers(monster);
            var completeness = ClassifyCompleteness(text, gaps.Count);
            if (gaps.Count > 0 && LoggedGaps.TryAdd(monsterId, 0))
                PushWarning($"[sts2agent] Unresolved native enemy rules for {monsterId}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");

            System.Threading.Interlocked.Increment(ref _coldExtractionCount);
            System.Threading.Interlocked.Add(ref _coldExtractionTicks, Stopwatch.GetTimestamp() - started);
            Readouts.AddOrUpdate(machine, new Readout(language, text, inlinePowerIds, hoistedPowers, completeness));
            return (text, inlinePowerIds, hoistedPowers, completeness);
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd(monsterId, 0))
                PushWarning($"[sts2agent] Enemy mechanism unavailable for {monsterId}: {error.Message}");
            System.Threading.Interlocked.Increment(ref _coldExtractionCount);
            System.Threading.Interlocked.Add(ref _coldExtractionTicks, Stopwatch.GetTimestamp() - started);
            if (machine is not null)
                Readouts.AddOrUpdate(machine, new Readout(language, null, EmptyPowers, EmptyHoistedPowers, MechanismCompleteness.Unavailable));
            return (null, EmptyPowers, EmptyHoistedPowers, MechanismCompleteness.Unavailable);
        }
    }

    internal static (string? Text, IReadOnlySet<string> Gaps) Describe(MonsterModel monster)
    {
        var (text, gaps, _, _) = DescribeWithPowers(monster);
        return (text, gaps);
    }

    internal static (string? Text, IReadOnlySet<string> Gaps, IReadOnlySet<string> InlinePowerIds, IReadOnlyList<Type> HoistedPowers) DescribeWithPowers(MonsterModel monster)
    {
        var inlinePowers = new HashSet<string>(StringComparer.Ordinal);
        var hoisted = new HashSet<Type>();
        var (text, gaps) = Describe(monster, [monster.GetType()], inlinePowers, hoisted);
        var orderedHoisted = hoisted.OrderBy(t => ModelDb.GetId(t).ToString(), StringComparer.Ordinal).ToArray();
        return (text, gaps, inlinePowers, orderedHoisted);
    }

    private static (string? Text, IReadOnlySet<string> Gaps) Describe(
        MonsterModel monster,
        HashSet<Type> described,
        HashSet<string>? inlinePowers = null,
        HashSet<Type>? hoistedPowers = null)
    {
        if (monster.MoveStateMachine is not { } machine)
            return (null, new HashSet<string>());
        if (InitialState?.GetValue(machine) is not MonsterState initial)
            throw new InvalidOperationException("The native initial move state is unavailable.");
        var text = new NativeRuleText(monster);
        string StateLabel(MonsterState state) => state is MoveState move && !IsPublicMove(move)
            ? "unrevealed move"
            : state.Id;
        var lines = new List<string>
        {
            initial.IsMove
                ? $"First move: {StateLabel(initial)}"
                : $"Initial selection: {StateLabel(initial)}",
        };
        foreach (var state in machine.States.Values.OfType<ConditionalBranchState>())
        {
            if (ConditionalStates?.GetValue(state) is not IEnumerable branches)
                continue;
            foreach (var branch in branches)
            {
                if (branch is not null && TryReadCondition(branch, out var condition) && condition is not null)
                    _ = text.ReadCondition(condition.Method);
            }
        }
        text.ReadHooks(monster.GetType(), lines);
        var examined = new HashSet<string>();
        var pending = new Queue<MonsterState>();
        var powerLines = new List<string>();
        pending.Enqueue(initial);
        while (pending.Count > 0 || EnqueueForcedMoves())
        {
            var state = pending.Dequeue();
            if (!examined.Add(state.Id))
                continue;
            if (state is MoveState move)
            {
                var followUp = move.FollowUpState ?? (move.FollowUpStateId is { } followUpId
                    ? machine.States.GetValueOrDefault(followUpId)
                    : null);
                if (followUp is not null)
                    pending.Enqueue(followUp);
                // The game hides this move's intent. Withhold its ID and effects but keep walking the graph.
                if (!IsPublicMove(move))
                    continue;

                List<string> effects = [];
                var moveHasGap = false;
                var killsSelf = false;
                if (PerformMove?.GetValue(move) is Delegate handler)
                {
                    (effects, moveHasGap, killsSelf) = text.ReadMove(handler.Method, move);
                }
                else
                {
                    text.Gap(move.Id);
                    moveHasGap = true;
                }

                if (followUp is null && !killsSelf)
                {
                    text.Gap(move.Id);
                    moveHasGap = true;
                }

                if (moveHasGap)
                    continue;

                var transition = followUp is null ? "" : $" → {StateLabel(followUp)}";
                if (followUp is not null && move.MustPerformOnceBeforeTransitioning)
                    transition += " (only after performing this move once since entering it)";
                var value = string.Join("; ", effects);
                var label = text.MoveLabel(move.Id);
                lines.Add($"{label}{(value.Length > 0 ? $": {value}" : "")}{transition}");
            }
            else if (state is ConditionalBranchState conditional
                && ConditionalStates?.GetValue(conditional) is IEnumerable branches)
            {
                var terms = new List<string>();
                var destinations = new List<string>();
                var allRead = true;
                var allDestinationsKnown = true;
                foreach (var branch in branches)
                {
                    if (branch is null || AccessTools.Field(branch.GetType(), "id")?.GetValue(branch) is not string destination
                        || !machine.States.TryGetValue(destination, out var next))
                    {
                        text.Gap(state.Id);
                        allDestinationsKnown = false;
                        continue;
                    }
                    var known = TryReadCondition(branch, out var condition);
                    var rule = !known ? null : condition is null
                        ? "otherwise"
                        : text.ReadCondition(condition.Method);
                    if (rule is null)
                    {
                        text.Gap(state.Id);
                        allRead = false;
                    }
                    terms.Add($"{rule} → {StateLabel(next)}");
                    destinations.Add(StateLabel(next));
                    pending.Enqueue(next);
                    if (known && condition is null)
                        break;
                }
                // Only enumerate branch destinations when every destination in the branch table was resolved.
                if (allDestinationsKnown && destinations.Count > 0)
                {
                    lines.Add(allRead
                        ? $"{state.Id} (first matching branch): {string.Join("; ", terms)}"
                        : $"{state.Id}: one of {string.Join(", ", destinations.Distinct())}");
                }
            }
            else if (state is RandomBranchState random)
            {
                var terms = new List<string>();
                var destinations = new List<string>();
                var allWeightsRead = true;
                var allDestinationsKnown = true;
                foreach (var branch in random.States)
                {
                    if (!machine.States.TryGetValue(branch.stateId, out var next))
                    {
                        text.Gap(state.Id);
                        allWeightsRead = false;
                        allDestinationsKnown = false;
                        continue;
                    }
                    var weight = branch.weightLambda is { } lambda ? text.ReadWeight(lambda) : null;
                    if (weight is null)
                    {
                        text.Gap(state.Id);
                        allWeightsRead = false;
                    }
                    var repeat = branch.repeatType switch
                    {
                        MoveRepeatType.UseOnlyOnce => "once",
                        MoveRepeatType.CannotRepeat => "no consecutive use",
                        MoveRepeatType.CanRepeatXTimes => $"up to {branch.maxTimes.ToString(CultureInfo.InvariantCulture)} consecutive",
                        _ => "",
                    };
                    var cooldown = branch.cooldown > 0
                        ? $"excluded if used in last {branch.cooldown.ToString(CultureInfo.InvariantCulture)} moves"
                        : "";
                    terms.Add($"{StateLabel(next)} ({string.Join(", ", new[] { weight, repeat, cooldown }.Where(part => !string.IsNullOrEmpty(part)))})");
                    destinations.Add(StateLabel(next));
                    pending.Enqueue(next);
                }
                if (allDestinationsKnown && destinations.Count > 0)
                {
                    lines.Add(allWeightsRead
                        ? $"{state.Id} (eligible moves chosen by relative weight): {string.Join("; ", terms)}"
                        : $"{state.Id}: one of {string.Join(", ", destinations.Distinct())}");
                }
            }
            else
                text.Gap(state.Id);

        }
        lines.AddRange(powerLines);
        if (inlinePowers is not null)
        {
            foreach (var p in text.EmittedPowers)
                inlinePowers.Add(ModelDb.GetId(p).ToString());
        }
        text.ReadGeneratedCards(lines);
        var gaps = new HashSet<string>(text.Gaps);
        foreach (var summon in text.Summons.Where(described.Add).OrderBy(type => type.FullName, StringComparer.Ordinal))
        {
            // A detached copy builds the summon's native graph without touching the combat.
            if (ModelDb.GetByIdOrNull<MonsterModel>(ModelDb.GetId(summon))?.ToMutable() is not { } copy)
            {
                gaps.Add($"{summon.Name}/summoned move graph");
                continue;
            }
            text.ConfigureSummon(copy);
            copy.SetUpForCombat();
            var (nested, nestedGaps) = Describe(copy, described, inlinePowers, hoistedPowers);
            gaps.UnionWith(nestedGaps.Select(gap => $"{summon.Name}/{gap}"));
            if (nested is null)
                continue;
            lines.Add($"Summoned {text.Label(summon)}:");
            lines.AddRange(nested.Split('\n').Select(line => "  " + line));
        }
        return (lines.Count > 1 ? string.Join("\n", lines) : null, gaps);

        bool EnqueueForcedMoves()
        {
            text.ReadPowerHooks(powerLines, hoistedPowers);
            foreach (var forced in text.ForcedMoves)
                if (!examined.Contains(forced) && machine.States.TryGetValue(forced, out var next))
                    pending.Enqueue(next);
            return pending.Count > 0;
        }
    }

    private static bool TryReadCondition(object branch, out Delegate? condition)
    {
        condition = null;
        if (AccessTools.Field(branch.GetType(), "_conditionalLambda") is not { } field)
            return false;
        var value = field.GetValue(branch);
        condition = value as Delegate;
        return value is null || condition is not null;
    }

}
