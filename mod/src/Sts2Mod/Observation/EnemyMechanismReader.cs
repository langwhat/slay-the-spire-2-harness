using System.Collections;
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

namespace Sts2Mod.Observation;

internal static class EnemyMechanismReader
{
    private const string UnsetMoveId = "UNSET_MOVE";
    private sealed record Readout(string Language, string? Text);
    private static readonly ConditionalWeakTable<MonsterMoveStateMachine, Readout> Readouts = new();
    private static readonly FieldInfo? InitialState = AccessTools.Field(typeof(MonsterMoveStateMachine), "_initialState");
    private static readonly FieldInfo? PerformMove = AccessTools.Field(typeof(MoveState), "_onPerform");
    private static readonly PropertyInfo? ConditionalStates = AccessTools.Property(typeof(ConditionalBranchState), "States");

    internal static bool IsPublicMove(MoveState move)
        => move.StateId != UnsetMoveId
            && move.Intents.Count > 0
            && !move.Intents.Any(intent => intent.IntentType is IntentType.Hidden or IntentType.Unknown);

    public static string? Read(Creature creature, string language)
    {
        if (creature.Monster is not { MoveStateMachine: { } machine } monster
            || monster.NextMove.StateId == UnsetMoveId)
            return null;

        if (Readouts.TryGetValue(machine, out var cached) && cached.Language == language)
            return cached.Text;

        string? text = null;
        try
        {
            // Unresolved native paths are extractor work items. They stay in the diagnostics log.
            (text, var gaps) = Describe(monster);
            if (gaps.Count > 0)
                GD.PushWarning($"[sts2agent] Unresolved native enemy rules for {monster.Id}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");
        }
        catch (Exception error)
        {
            GD.PushWarning($"[sts2agent] Enemy mechanism unavailable for {monster.Id}: {error.Message}");
        }
        Readouts.Remove(machine);
        Readouts.Add(machine, new Readout(language, text));
        return text;
    }

    internal static (string? Text, IReadOnlySet<string> Gaps) Describe(MonsterModel monster)
        => Describe(monster, [monster.GetType()]);

    private static (string? Text, IReadOnlySet<string> Gaps) Describe(MonsterModel monster, HashSet<Type> described)
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
                if (followUp is null)
                    text.Gap(move.Id);

                List<string> effects = [];
                if (PerformMove?.GetValue(move) is Delegate handler)
                    effects = text.ReadMove(handler.Method, move);
                else
                    text.Gap(move.Id);

                var transition = followUp is null ? "" : $" → {StateLabel(followUp)}";
                if (followUp is not null && move.MustPerformOnceBeforeTransitioning)
                    transition += " (only after performing this move once since entering it)";
                var value = string.Join(", ", effects);
                var label = text.MoveLabel(move.Id);
                lines.Add($"{label}{(value.Length > 0 ? $": {value}" : "")}{transition}");
            }
            else if (state is ConditionalBranchState conditional
                && ConditionalStates?.GetValue(conditional) is IEnumerable branches)
            {
                var terms = new List<string>();
                var destinations = new List<string>();
                var allRead = true;
                foreach (var branch in branches)
                {
                    if (branch is null || AccessTools.Field(branch.GetType(), "id")?.GetValue(branch) is not string destination
                        || !machine.States.TryGetValue(destination, out var next))
                    {
                        text.Gap(state.Id);
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
                // Without every condition, state only the reachable moves.
                lines.Add(allRead
                    ? $"{state.Id} (first matching branch): {string.Join("; ", terms)}"
                    : $"{state.Id}: one of {string.Join(", ", destinations.Distinct())}");
            }
            else if (state is RandomBranchState random)
            {
                var terms = new List<string>();
                foreach (var branch in random.States)
                {
                    if (!machine.States.TryGetValue(branch.stateId, out var next))
                    {
                        text.Gap(state.Id);
                        continue;
                    }
                    var weight = branch.weightLambda is { } lambda ? text.ReadWeight(lambda) : null;
                    if (weight is null)
                        text.Gap(state.Id);
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
                    pending.Enqueue(next);
                }
                lines.Add($"{state.Id} (eligible moves chosen by relative weight): {string.Join("; ", terms)}");
            }
            else
                text.Gap(state.Id);

        }
        lines.AddRange(powerLines);
        var gaps = new HashSet<string>(text.Gaps);
        foreach (var summon in text.Summons.Where(described.Add).OrderBy(type => type.FullName, StringComparer.Ordinal))
        {
            // A detached copy builds the summon's native graph without touching the combat.
            if (ModelDb.GetByIdOrNull<MonsterModel>(ModelDb.GetId(summon))?.ToMutable() is not { } copy)
            {
                gaps.Add($"{summon.Name}/summoned move graph");
                continue;
            }
            copy.SetUpForCombat();
            var (nested, nestedGaps) = Describe(copy, described);
            gaps.UnionWith(nestedGaps.Select(gap => $"{summon.Name}/{gap}"));
            if (nested is null)
                continue;
            lines.Add($"Summoned {text.Label(summon)}:");
            lines.AddRange(nested.Split('\n').Select(line => "  " + line));
        }
        return (lines.Count > 1 ? string.Join("\n", lines) : null, gaps);

        bool EnqueueForcedMoves()
        {
            text.ReadPowerHooks(powerLines);
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
