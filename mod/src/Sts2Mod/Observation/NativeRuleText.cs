using System.Globalization;
using System.Reflection;
using System.Text.Json;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Ascension;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Powers;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.MonsterMoves.MonsterMoveStateMachine;
using MegaCrit.Sts2.Core.ValueProps;
using static Sts2Mod.Observation.NativeRuleBody;
using NativeHpDisplay = MegaCrit.Sts2.Core.Entities.Creatures.HpDisplay;

namespace Sts2Mod.Observation;

// Translate engine operations after the native receiver, operands and branch paths are known.
internal sealed partial class NativeRuleText(MonsterModel monster)
{
    private readonly Dictionary<MethodInfo, NativeRuleBody> _bodies = [];
    private readonly HashSet<Type> _powers = [];
    private readonly HashSet<Type> _readPowers = [];
    private readonly HashSet<string> _forcedMoves = [];
    private readonly HashSet<Type> _summons = [];
    private readonly HashSet<string> _branchFlags = [];
    private readonly Dictionary<FieldInfo, string?> _flagNames = [];
    private readonly HashSet<string> _gaps = [];
    private readonly HashSet<string> _initializedMonsterInts = [];

    private static readonly (string Method, string Timing)[] Hooks =
    [
        ("AfterAddedToRoom", "On entering combat"),
        ("BeforeCombatStart", "Before combat starts"),
        ("BeforeSideTurnStart", "Before side turn starts"),
        ("AfterSideTurnStart", "After side turn starts"),
        ("AfterSideTurnStartLate", "At side turn start, late"),
        ("AfterCardEnteredCombat", "After a card enters combat"),
        ("AfterCardDrawn", "After drawing a card"),
        ("BeforeCardPlayed", "Before card play"),
        ("AfterCardPlayed", "After each card play resolves"),
        ("AfterCardChangedPilesLate", "After a card changes piles, late"),
        ("AfterDamageReceived", "After damage resolves"),
        ("AfterDeath", "On death event"),
        ("BeforeSideTurnEndVeryEarly", "At side turn end, very early"),
        ("BeforeSideTurnEndEarly", "At side turn end, early"),
        ("BeforeSideTurnEnd", "Before side turn ends"),
        ("AfterSideTurnEnd", "After side turn ends"),
        ("AfterSideTurnEndLate", "At side turn end, late"),
        ("AfterPowerAmountChanged", "After a power changes stacks"),
        ("AfterApplied", "After this power is applied"),
        ("AfterRemoved", "After this power is removed"),
        ("BeforeAttack", "Before an attack"),
        ("AfterAttack", "After an attack resolves"),
        ("BeforeDamageReceived", "Before damage resolves"),
        ("AfterDamageGiven", "After dealing damage"),
        ("AfterCurrentHpChanged", "After HP changes"),
        ("AfterBlockBroken", "After block is broken"),
        ("BeforeDeath", "Before death"),
        ("BeforePotionUsed", "Before a potion is used"),
        ("AfterCardGeneratedForCombat", "After a card is generated in combat"),
        ("AfterCreatureAddedToCombat", "After a creature enters combat"),
        ("AfterOstyRevived", "After Osty revives"),
        ("AfterModifyingHandDraw", "After modifying hand draw"),
        ("AfterModifyingDamageAmount", "After modifying damage"),
        ("AfterModifyingHpLostBeforeOsty", "After modifying HP lost"),
        ("AfterModifyingHpLostAfterOsty", "After modifying HP lost"),
        ("AfterCombatEnd", "After combat ends"),
    ];

    internal IReadOnlySet<string> ForcedMoves => _forcedMoves;
    internal IReadOnlySet<Type> Summons => _summons;
    internal string Label(Type model) => ModelLabel(model);
    internal IReadOnlySet<string> Gaps => _gaps;
    internal void Gap(string location) => _gaps.Add(location);

    internal string MoveLabel(string id)
    {
        var key = id.Split("_MOVE", StringSplitOptions.None)[0];
        var title = MonsterModel.L10NMonsterLookup($"{monster.Id.Entry}.moves.{key}.title");
        return title.Exists() ? $"{title.GetFormattedText()} ({id})" : id;
    }

    internal string? ReadCondition(MethodInfo method)
    {
        foreach (var result in Body(method).Returns)
        {
            if (BooleanGetter(result.Value) is { } getter)
                _branchFlags.Add(getter);
            if (result.Value is Operation { Operator: "ceq", Left: var left }
                && BooleanGetter(left) is { } inner)
                _branchFlags.Add(inner);
        }
        return RenderReturns(method, value => Condition(new Test(value, true)));
    }

    internal string? ReadWeight(Delegate weight)
    {
        if (weight.Target is { } target && target.GetType().IsDefined(typeof(System.Runtime.CompilerServices.CompilerGeneratedAttribute)))
        {
            var result = Body(weight.Method);
            if (!result.Incomplete && result.Returns is [{ Value: Field { Receiver: Input { Name: "this" } } field, Conditions.Length: 0 }]
                && field.Member.DeclaringType == target.GetType() && field.Member.FieldType == typeof(float)
                && field.Member.Name == "weight")
                return Number(field.Member.GetValue(target));
        }
        return RenderReturns(weight.Method, ValueText);
    }

    internal List<string> ReadMove(MethodInfo method, MoveState move)
    {
        var (effects, _) = ReadEffects(method);
        if ((move.Intents.Any(intent => intent is AttackIntent) && !effects.Any(effect => effect.Attack))
            || (move.Intents.Any(intent => intent is BuffIntent or DebuffIntent or DefendIntent) && effects.Count == 0))
            Gap(move.Id);
        return effects.Select(effect => effect.Text).ToList();
    }

    internal void ReadHooks(Type type, List<string> lines)
    {
        var power = typeof(PowerModel).IsAssignableFrom(type);
        var previousOwner = _hookOwner;
        _hookOwner = power ? type : null;
        try
        {
            ReadHooks(type, power, lines);
        }
        finally
        {
            _hookOwner = previousOwner;
        }
    }

    private void ReadHooks(Type type, bool power, List<string> lines)
    {
        var gapsBefore = _gaps.Count;
        var targetLines = power ? new List<string>() : lines;
        var powerPrefix = power ? PowerLabel(type) + " · " : "";
        var handled = new HashSet<string>(Hooks.Select(hook => hook.Method), StringComparer.Ordinal);
        var preHandled = new HashSet<string>(StringComparer.Ordinal);
        if (power)
        {
            ReadPlayerResourceHooks(type, preHandled, out var resourceLines);
            ReadPowerReceptionHooks(type, powerPrefix, preHandled, targetLines);
            ReadDamageCapHooks(type, powerPrefix, preHandled, targetLines);
            ReadSpecialPowerHooks(type, powerPrefix, preHandled, targetLines);
            foreach (var line in resourceLines)
                targetLines.Add($"{powerPrefix}{line}");
            handled.UnionWith(preHandled);
        }

        if (power && MutableDynamicKeys(type) is { Count: > 0 } mutableKeys
            && ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(type)) is { } canonicalPower)
        {
            var nonZeroDefaults = mutableKeys.Order(StringComparer.Ordinal)
                .Where(k => canonicalPower.DynamicVars.TryGetValue(k, out var v) && v.BaseValue != 0m)
                .Select(k => $"parameter {k} starts at {Number(canonicalPower.DynamicVars[k].BaseValue)}")
                .ToArray();
            if (nonZeroDefaults.Length > 0)
                targetLines.Add($"{powerPrefix}Initial state: {string.Join("; ", nonZeroDefaults)}");
        }

        foreach (var (name, rawTiming) in Hooks)
        {
            if (preHandled.Contains(name) || FindOverride(type, name) is not { } method)
                continue;
            var timing = name == "AfterOstyRevived"
                ? $"After {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} revives"
                : rawTiming;
            var (effects, _) = ReadEffects(method, power ? type : null);
            if (effects.Count != 0)
            {
                var trigger = effects.Select(effect => effect.Trigger).FirstOrDefault(trigger => trigger is not null) ?? timing;
                targetLines.Add($"{powerPrefix}{trigger}: {string.Join("; ", effects.Select(effect => effect.Text))}");
            }
        }

        foreach (var (name, neutral, timing, format) in ValueHooks)
        {
            if (preHandled.Contains(name) || FindOverride(type, name) is not { } hook)
                continue;
            handled.Add(name);
            var rendered = ReadValueHook(hook, neutral, format, out var adjusters);
            if (rendered is null)
            {
                Gap($"{type.Name}.{name}");
                continue;
            }
            if (rendered.Count == 0)
                continue;
            var adjusted = adjusters.Count == 0 ? "" : $" (then adjusted by {string.Join(", ", adjusters)} when present)";
            targetLines.Add($"{powerPrefix}{timing}: {string.Join("; ", rendered)}{adjusted}");
        }

        if (FindOverride(type, "TryModifyKeywordsInCombat") is { } keywordHook)
        {
            handled.Add(keywordHook.Name);
            var rendered = ReadKeywordHook(keywordHook);
            if (rendered is null)
                Gap($"{type.Name}.{keywordHook.Name}");
            else if (rendered.Count != 0)
                targetLines.Add($"{powerPrefix}Card keywords in combat: {string.Join("; ", rendered)}");
        }

        // Combat queries are asked outside the shared dispatchers too, so read every override.
        // Monster-level queries only drive presentation such as the bestiary.
        foreach (var query in type.GetMethods(Declared).Where(method => method.ReturnType == typeof(bool)
            && method.Name.StartsWith("Should", StringComparison.Ordinal) && IsHookOverride(method)
            && method.GetBaseDefinition().DeclaringType != typeof(MonsterModel)
            && !preHandled.Contains(method.Name)))
        {
            handled.Add(query.Name);
            var rendered = ReadQuery(query);
            if (rendered is null)
                Gap($"{type.Name}.{query.Name}");
            else if (rendered.Count != 0)
                targetLines.Add($"{powerPrefix}{Humanize(query.Name)}: {string.Join("; ", rendered)}");
        }

        foreach (var unhandled in NativeHookDispatch.Overrides(type))
            if (!handled.Contains(unhandled.Name))
                Gap($"{unhandled.DeclaringType?.Name}.{unhandled.Name}");
        foreach (var unread in NativeHookDispatch.Unread)
            Gap(unread);

        if (power && (_gaps.Count == gapsBefore || (MutableDynamicKeys(type).Count == 0 && !AllOwnedFields(type).Any())))
            lines.AddRange(targetLines);
    }

    private void ReadPlayerResourceHooks(Type type, HashSet<string> preHandled, out List<string> lines)
    {
        lines = [];
        ReadPlayerResourceHooks(type, "", preHandled, lines);
    }

    private const BindingFlags Declared = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    private static readonly Type[] BaseModels = [typeof(AbstractModel), typeof(MonsterModel), typeof(PowerModel), typeof(AfflictionModel)];

    // Hooks that return a modified number, with the result that leaves the number unchanged.
    // A null neutral result means the hook passes its `amount` argument through unchanged.
    private static readonly (string Method, decimal? Neutral, string Timing, string Format)[] ValueHooks =
    [
        ("ModifyDamageAdditive", 0m, "Damage addition per hit, before multipliers and block", "+{0}"),
        ("ModifyDamageMultiplicative", 1m, "Damage multiplier per hit, after additions", "×{0}"),
        ("ModifyDamageMultiplicativeLate", 1m, "Damage multiplier per hit, after other multipliers", "×{0}"),
        ("ModifyBlockAdditive", 0m, "Block addition, before multipliers", "+{0}"),
        ("ModifyBlockMultiplicative", 1m, "Block multiplier, after additions", "×{0}"),
        ("GetScaledAmountForMultiplayer", null, "Applied stacks scaled by player count", "{0}"),
    ];

    private Type? _hookOwner;

    private List<string>? ReadValueHook(MethodInfo hook, decimal? neutral, string format, out SortedSet<string> adjusters)
    {
        adjusters = new SortedSet<string>(StringComparer.Ordinal);
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            var value = Peel(result.Value, adjusters);
            if (neutral is null ? value is Input { Name: "amount" } : Numeric(value) == neutral)
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }
            if (ValueText(value) is not { } amount)
                return null;
            outcomes.Add((string.Format(CultureInfo.InvariantCulture, format, amount), result.Conditions));
        }
        return RenderOutcomes(outcomes, body.Terminals.Where(terminal => !terminal.Returned).Select(terminal => terminal.Conditions));
    }

    // Report the answers that differ from the engine default.
    private List<string>? ReadQuery(MethodInfo query)
    {
        if (Body(query.GetBaseDefinition()) is not { Incomplete: false, Returns: [{ Conditions.Length: 0 } fallback] }
            || !TryTruth(fallback.Value, out var standard))
            return null;
        var body = Body(query);
        if (body.Incomplete)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (TryTruth(result.Value, out var answer))
            {
                outcomes.Add((answer == standard ? "" : AnswerText(answer), result.Conditions));
                continue;
            }
            // A computed answer splits into the test that decides it.
            var testValue = result.Value is Operation { Operator: "ceq", Left: var negated, Right: Constant { Data: 0 or null } }
                ? negated
                : result.Value is Operation { Operator: "cgt.un", Left: var positive, Right: Constant { Data: 0 or null } }
                    ? positive
                    : result.Value;
            var testExpected = result.Value is Operation { Operator: "ceq", Right: Constant { Data: 0 or null } }
                ? standard
                : !standard;
            outcomes.Add((AnswerText(!standard), [.. result.Conditions, new Test(testValue, testExpected)]));
            outcomes.Add(("", [.. result.Conditions, new Test(testValue, !testExpected)]));
        }
        return RenderOutcomes(outcomes, body.Terminals.Where(terminal => !terminal.Returned).Select(terminal => terminal.Conditions));

        static string AnswerText(bool answer) => answer ? "yes" : "no";
    }

    // The hook adds keywords to the set it receives and reports whether it added one.
    private List<string>? ReadKeywordHook(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (TryTruth(result.Value, out var added) && !added)
                outcomes.Add(("", result.Conditions));
            else if (result.Value is Call { Method.Name: "Add", Receiver: Input { Name: "keywords" }, Arguments: [Constant { Data: int keyword }] })
                outcomes.Add(($"that card has {(CardKeyword)keyword}", result.Conditions));
            else
                return null;
        }
        return RenderOutcomes(outcomes, body.Terminals.Where(terminal => !terminal.Returned).Select(terminal => terminal.Conditions));
    }

    // Each distinct outcome gets the minimal guard that separates it from the other outcomes.
    private List<string>? RenderOutcomes(List<(string Text, Test[] Conditions)> outcomes, IEnumerable<Test[]> excluded)
    {
        var excludedPaths = excluded.ToArray();
        var rendered = new List<GuardedEffects>();
        foreach (var group in outcomes.Where(outcome => outcome.Text.Length != 0).GroupBy(outcome => outcome.Text))
        {
            var reaching = group.Select(outcome => outcome.Conditions).ToArray();
            var others = outcomes.Where(outcome => outcome.Text != group.Key).Select(outcome => outcome.Conditions)
                .Concat(excludedPaths);
            foreach (var path in NativeRuleGuards.Minimize(reaching, others, TestKey) ?? MergeTests(reaching))
            {
                if (Guard(path) is not { } guard)
                    return null;
                if (guard != Never)
                    rendered.Add(new(path, guard, [new Effect(group.Key)]));
            }
        }
        return RenderBranches(rendered).Select(effect => effect.Text).Distinct().ToList();
    }

    // A relic or power that rewrites a number exposes its own rule; name it instead of restating it.
    private static Value Peel(Value value, ISet<string> adjusters)
    {
        while (value is Call { Method: MethodInfo method, Receiver: not null, Arguments: var arguments }
            && method.ReturnType == typeof(decimal) && method.DeclaringType is { } owner
            && (typeof(RelicModel).IsAssignableFrom(owner) || typeof(PowerModel).IsAssignableFrom(owner))
            && method.GetParameters().FirstOrDefault(parameter => parameter.Name == "amount") is { } amount)
        {
            adjusters.Add(ModelLabel(owner));
            value = arguments[amount.Position];
        }
        return value;
    }

    private static bool IsHookOverride(MethodInfo method)
        => method.IsVirtual && method.DeclaringType is { } owner && !BaseModels.Contains(owner)
            && method.GetBaseDefinition().DeclaringType is { } origin && BaseModels.Contains(origin);

    private static string Humanize(string name)
    {
        var words = System.Text.RegularExpressions.Regex.Replace(name, "(?<=[a-z])(?=[A-Z])", " ").ToLowerInvariant();
        return char.ToUpperInvariant(words[0]) + words[1..];
    }

    internal void ReadPowerHooks(List<string> lines)
    {
        while (_powers.Except(_readPowers).OrderBy(type => type.FullName, StringComparer.Ordinal).FirstOrDefault() is { } power)
        {
            _readPowers.Add(power);
            ReadHooks(power, lines);
        }
        // Emit initializers for monster int counters read in moves/branches that were not set in AfterAddedToRoom.
        var canonicalMonster = ModelDb.GetByIdOrNull<MonsterModel>(monster.Id);
        foreach (var (name, field) in _trackedMonsterInts.OrderBy(entry => entry.Name, StringComparer.Ordinal))
        {
            if (_choiceCursors.Contains(field))
                continue;
            if (_initializedMonsterInts.Add(name))
            {
                var initialVal = canonicalMonster is not null && field.DeclaringType?.IsInstanceOfType(canonicalMonster) == true
                    && field.GetValue(canonicalMonster) is int rawInit ? rawInit : 0;
                lines.Insert(0, $"Initial {name}: {initialVal}");
            }
        }
    }

    // Overloads can share a hook's name, so match the override of the engine hook itself.
    private static MethodInfo? FindOverride(Type type, string name)
        => type.GetMethods(Declared).FirstOrDefault(method => method.Name == name && IsHookOverride(method));

    private NativeRuleBody Body(MethodInfo method)
    {
        if (!_bodies.TryGetValue(method, out var result))
        {
            result = NativeRuleBody.Read(method);
            _bodies.Add(method, result);
        }
        return result;
    }

    private sealed record Effect(string Text, bool Attack = false, string? Trigger = null, bool ChangesGuard = false,
        Type? AddedPower = null, Type? AddedCard = null, Type? AddedSummon = null, string? ForcedMove = null);

    private sealed record GuardedEffects(Test[] Path, string Guard, Effect[] Effects, string? Repetitions = null);

    private IEnumerable<Effect> RenderBranches(List<GuardedEffects> effects)
    {
        for (var start = 0; start < effects.Count;)
        {
            var first = effects[start];
            if (FindExclusiveBranch(effects, start) is { } branch)
            {
                yield return branch.Effect;
                start = branch.End;
                continue;
            }
            if (first.Repetitions is null && first.Guard.Length != 0 && first.Effects.Length > 1
                && first.Effects.Any(effect => effect.ChangesGuard))
            {
                yield return Combine(first.Effects, When(first.Guard, $"({string.Join("; ", first.Effects.Select(effect => effect.Text))})"));
                start++;
                continue;
            }
            foreach (var effect in first.Effects)
            {
                var text = When(first.Guard, effect.Text);
                yield return effect with { Text = first.Repetitions is { } count
                    ? $"{count} times: {text}"
                    : text };
            }
            start++;
        }
    }

    private (Effect Effect, int End)? FindExclusiveBranch(List<GuardedEffects> effects, int start)
    {
        var candidates = new List<GuardedEffects>();
        for (var end = start; end < effects.Count && effects[end] is { Guard.Length: > 0 } candidate; end++)
            candidates.Add(candidate with
            {
                Path = candidate.Path.Where(test => Condition(test) is { Length: > 0 })
                    .DistinctBy(test => (TestKey(test), test.Expected)).ToArray(),
            });
        var paths = candidates.ToArray().AsSpan();
        for (var count = paths.Length; count > 1; count--)
            if (ExclusiveBranch(paths[..count], 0) is { } branch)
                return (Combine(candidates.Take(count).SelectMany(candidate => candidate.Effects), branch),
                    start + count);
        return null;
    }

    // A test's identity includes its instruction for mutable values. Two tests of the
    // same flag on opposite sides of an effect therefore cannot form an otherwise.
    private string? ExclusiveBranch(ReadOnlySpan<GuardedEffects> paths, int index)
    {
        var common = new List<Test>();
        while (true)
        {
            if (paths[0].Path.Length <= index)
                return null;
            var first = paths[0].Path[index];
            var key = TestKey(first);
            var whenTruePaths = new List<GuardedEffects>();
            var otherwisePaths = new List<GuardedEffects>();
            foreach (var candidate in paths)
            {
                if (candidate.Path.Length <= index || TestKey(candidate.Path[index]) != key)
                    return null;
                (candidate.Path[index].Expected == first.Expected ? whenTruePaths : otherwisePaths).Add(candidate);
            }
            if (otherwisePaths.Count == 0)
            {
                common.Add(first);
                index++;
                continue;
            }
            if (BranchArm(whenTruePaths.ToArray(), index + 1, wrapBranch: true) is not { } whenTrue
                || BranchArm(otherwisePaths.ToArray(), index + 1, wrapBranch: false) is not { } otherwise
                || Condition(first) is not { } condition || Guard(common.ToArray()) is not { } guard)
                return null;
            // Simplify `if A: (if B: X; otherwise: Y); otherwise: Y` to `if A and B: X; otherwise: Y`.
            var redundantSuffix = $"; otherwise: {otherwise})";
            string branch;
            if (whenTrue.StartsWith("(if ", StringComparison.Ordinal)
                && whenTrue.EndsWith(redundantSuffix, StringComparison.Ordinal)
                && !condition.Contains(" or ", StringComparison.Ordinal)
                && !whenTrue[4..^redundantSuffix.Length].Contains("; otherwise", StringComparison.Ordinal))
            {
                var inner = whenTrue[4..^redundantSuffix.Length];
                branch = $"if {condition} and {inner}; otherwise: {otherwise}";
            }
            else
            {
                branch = $"if {condition}: {whenTrue}; otherwise{(otherwise.StartsWith("if ", StringComparison.Ordinal) ? " " : ": ")}{otherwise}";
            }
            return guard.Length == 0 ? branch : $"if {guard}: ({branch})";
        }
    }

    private string? BranchArm(ReadOnlySpan<GuardedEffects> paths, int index, bool wrapBranch)
    {
        var texts = new List<string>();
        foreach (var path in paths)
        {
            if (path.Path.Length != index)
                return ExclusiveBranch(paths, index) is { } nested
                    ? wrapBranch ? $"({nested})" : nested
                    : null;
            texts.AddRange(path.Effects.Select(effect => path.Repetitions is { } count ? $"{count} times: {effect.Text}" : effect.Text));
        }
        return texts.Count == 1 ? texts[0] : $"({string.Join("; ", texts)})";
    }

    private static Effect Combine(IEnumerable<Effect> effects, string text)
        => new(text, effects.Any(effect => effect.Attack), effects.Select(effect => effect.Trigger).FirstOrDefault(trigger => trigger is not null),
            effects.Any(effect => effect.ChangesGuard));

    private sealed record EffectStep(int Index, Value Action, Test[] Conditions, int Epoch = 0, int Order = 0);

    private (List<Effect> Effects, bool Incomplete) ReadEffects(MethodInfo method, Type? power = null,
        int depth = 0, bool onOwnedMonster = false, IReadOnlyDictionary<string, Value>? bindings = null)
    {
        var body = Body(method);
        var incomplete = false;
        if (body.Incomplete)
            MarkIncomplete(method.Name);
        foreach (var field in body.Writes)
            MarkIncomplete($"{method.Name}/{field.Name}");
        foreach (var field in body.FieldSteps.Select(step => step.Assignment.Target).Distinct())
            if (!IsTrackedSlot(field) && IsGameplayField(field.Member))
                MarkIncomplete($"{method.Name}/{field.Member.Name}");

        RecordSummonPropertySetters(body);
        var powerTargets = CollectMutablePowerTargets(body);
        var powerHolders = CollectAppliedPowerHolders(body);

        // A helper reads its parameters as the operands its caller passed.
        IEnumerable<EffectStep> calls = body.Calls.Select(step => new EffectStep(step.Index, step.Call, step.Conditions, step.Epoch, step.Order))
            .Concat(body.FieldSteps.Where(step => IsTrackedSlot(step.Assignment.Target))
                .Select(step => new EffectStep(step.Index, step.Assignment, step.Conditions, step.Epoch, step.Order)));
        IEnumerable<Terminal> terminals = body.Terminals;
        if (bindings is not null)
        {
            calls = calls.Select(step => step with { Action = Bind(step.Action, bindings), Conditions = Bind(step.Conditions, bindings) }).ToList();
            terminals = body.Terminals.Select(terminal => terminal with { Conditions = Bind(terminal.Conditions, bindings) }).ToList();
        }

        IReadOnlySet<string> flags = _branchFlags;
        if (onOwnedMonster)
            flags = _branchFlags.Concat(calls.SelectMany(step => step.Conditions)
                .Select(test => BooleanField(test.Value)).OfType<string>()).ToHashSet();

        var effects = new List<GuardedEffects>();
        foreach (var group in calls.GroupBy(step => (step.Epoch, step.Index))
            .OrderBy(group => (group.Key.Epoch, group.Max(s => s.Order), group.Key.Index)))
        {
            var loop = stepLoop(body, group.Key.Index);
            var repetitions = loop is null ? null : ValueText(loop.Count);
            var rendered = new List<GuardedEffects>();
            var alternatives = group.GroupBy(step => Key(step.Action));
            foreach (var alternative in alternatives)
            {
                var matching = alternative.Where(step => step.Conditions.All(IsCurrentRun) && !IsUnchangedAssignment(step)).ToArray();
                if (matching.Length == 0)
                    continue;
                var action = matching[0].Action;
                var call = action as Call;
                if (call is not null && call.Method.IsSpecialName && call.Method.DeclaringType == typeof(Creature)
                    && call.Method.Name.StartsWith("add_", StringComparison.Ordinal))
                {
                    if (!IsPresentationEventSubscription(call, method.DeclaringType))
                        MarkIncomplete($"{method.Name}/{call.Method.Name}");
                    continue;
                }

                var recognized = true;
                var effect = call is not null ? DescribeEffect(call, method, power, flags, powerTargets, powerHolders, out recognized)
                    : action is Assignment assignment ? DescribeSlotAssignment(assignment, method) : null;

                List<Effect> extracted;
                if (effect is not null)
                    extracted = [effect];
                else if (call is not null && !recognized && depth < 2 && ReadableHelper(call, method, power, matching.Select(step => step.Conditions)) is { } helper)
                {
                    var nested = ReadEffects(helper.Method, helper.HelperPower,
                        depth + 1, onOwnedMonster || helper.OwnedMonster, helper.Bindings);
                    extracted = nested.Effects;
                    if (nested.Incomplete)
                        MarkIncomplete($"{method.Name}/{helper.Method.DeclaringType?.Name}.{helper.Method.Name}");
                }
                else if (call is not null && !recognized && IsSkippedPresentationHelper(call, method))
                    continue;
                else
                {
                    if (call is null)
                        MarkIncomplete($"{method.Name}/card instance");
                    else if (recognized || IsModelEffect(call.Method))
                        MarkIncomplete($"{method.Name}/{call.Method.DeclaringType?.Name}.{call.Method.Name}");
                    continue;
                }

                if (extracted.Count == 0)
                    continue;
                if (method.Name == "AfterDamageReceived" && action is Call { Method.Name: "AddGeneratedCardToCombat" })
                    extracted = extracted.Select(item => item with { Trigger = AfterEachReceivedHit }).ToList();
                if (method.Name == "AfterAttack" && matching.Any(step => step.Conditions.Any(test =>
                    test.Value is Call { Method.DeclaringType: var result, Receiver: { } hit }
                    && result == typeof(DamageResult) && AttackHit(hit) is not null)))
                    extracted = extracted.Select(item => item with { Trigger = "Once after the entire attack resolves" }).ToList();
                var missing = terminals.Where(terminal => !terminal.Visited.Contains(group.Key))
                    .Select(terminal => terminal.Conditions)
                    .Concat(group.Where(step => Key(step.Action) != alternative.Key || IsUnchangedAssignment(step)).Select(step => step.Conditions));
                var reaching = matching.Select(step => StripImpliedTypeFilter(action, step.Conditions)).ToArray();
                // A stack comparison is reached only after the corresponding power was found.
                // Keep that short-circuit test in the rendered branch.
                var minimized = reaching.Any(path => path.Any(test => ReadsOwnedPowerStacks(test.Value)))
                    ? null : NativeRuleGuards.Minimize(reaching, missing, TestKey);
                foreach (var path in minimized ?? MergeTests(reaching))
                {
                    if (Guard(path) is not { } guard)
                    {
                        MarkIncomplete($"{method.Name}/condition");
                        continue;
                    }
                    if (guard == Never)
                        continue;
                    if (!rendered.Any(previous => previous.Guard == guard && previous.Effects.SequenceEqual(extracted)))
                    {
                        rendered.Add(new(path, guard, extracted.ToArray(), repetitions));
                        foreach (var item in extracted)
                        {
                            if (item.AddedPower is { } ap)
                                _powers.Add(ap);
                            if (item.AddedCard is { } ac)
                                _generatedCards.Add(ac);
                            if (item.AddedSummon is { } asum)
                                _summons.Add(asum);
                            if (item.ForcedMove is { } fm)
                                _forcedMoves.Add(fm);
                        }
                    }
                }
            }
            // Equivalent paths can reach one instruction. Separate instructions still run separately.
            effects.AddRange(rendered);
        }
        return (RenderBranches(GroupGuardedChanges(CollapseRepeatedUnrolled(effects))).ToList(), incomplete);

        static Loop? stepLoop(NativeRuleBody b, int idx)
            => b.Loops.Where(l => idx >= l.Start && idx < l.End).MaxBy(l => l.Start);

        void MarkIncomplete(string location)
        {
            incomplete = true;
            Gap(location);
        }
    }

    private static List<GuardedEffects> CollapseRepeatedUnrolled(List<GuardedEffects> input)
    {
        var result = new List<GuardedEffects>();
        for (var i = 0; i < input.Count;)
        {
            var current = input[i];
            if (current.Repetitions is not null)
            {
                result.Add(current);
                i++;
                continue;
            }
            var count = 1;
            while (i + count < input.Count && input[i + count].Repetitions is null
                && input[i + count].Guard == current.Guard
                && input[i + count].Effects.SequenceEqual(current.Effects))
                count++;
            result.Add(count > 1 ? current with { Repetitions = Number(count) } : current);
            i += count;
        }
        return result;
    }

    private void RecordSummonPropertySetters(NativeRuleBody body)
    {
        foreach (var step in body.Calls)
        {
            if (step.Conditions.All(IsCurrentRun)
                && step.Call.Method is MethodInfo { Name: var name } setter && name.StartsWith("set_", StringComparison.Ordinal)
                && step.Call.Arguments is [Constant { Data: 1 or true }]
                && step.Call.Receiver is Operation { Operator: "castclass" or "isinst", Right: Constant { Data: Type monsterType } }
                && typeof(MonsterModel).IsAssignableFrom(monsterType)
                && monsterType.GetProperty(name[4..], Declared) is { CanWrite: true } prop && prop.PropertyType == typeof(bool))
            {
                if (!_summonTrueFlags.TryGetValue(monsterType, out var list))
                {
                    list = [];
                    _summonTrueFlags[monsterType] = list;
                }
                if (!list.Contains(prop))
                    list.Add(prop);
            }
        }
    }

    private Dictionary<string, string> CollectAppliedPowerHolders(NativeRuleBody body)
    {
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var step in body.Calls)
        {
            if (step.Call.Method.DeclaringType == typeof(PowerCmd) && step.Call.Method.Name == "Apply"
                && !step.Call.Method.IsGenericMethod
                && Argument(step.Call, "power") is { } instance && Argument(step.Call, "target") is { } receiving
                && ValueText(receiving) is { } who)
                map[Key(instance)] = who;
        }
        return map;
    }

    private Dictionary<string, string> CollectMutablePowerTargets(NativeRuleBody body)
    {
        var map = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var step in body.Calls)
        {
            if (step.Call.Method.Name == "set_Target" && step.Call.Method.DeclaringType == typeof(PowerModel)
                && step.Call.Receiver is { } instance && step.Call.Arguments is [var target]
                && ValueText(target) is { } who)
                map[Key(instance)] = who;
        }
        return map;
    }

    private static bool IsSameMonsterTypeLookup(Value? receiver, Type? declaringType)
        => receiver is Operation { Operator: "castclass" or "isinst", Right: Constant { Data: Type cast } }
            && typeof(MonsterModel).IsAssignableFrom(cast) && cast == declaringType;

    private static Value Bind(Value value, IReadOnlyDictionary<string, Value> bindings) => value switch
    {
        Input { Name: var name } when bindings.TryGetValue(name, out var bound) => bound,
        Assignment assignment => assignment with { Target = (Field)Bind(assignment.Target, bindings), Assigned = Bind(assignment.Assigned, bindings) },
        Field field when field.Receiver is { } receiver => field with { Receiver = Bind(receiver, bindings) },
        Call call => call with
        {
            Receiver = call.Receiver is { } receiver ? Bind(receiver, bindings) : null,
            Arguments = call.Arguments.Select(argument => Bind(argument, bindings)).ToArray(),
        },
        Operation operation => operation with
        {
            Left = Bind(operation.Left, bindings),
            Right = operation.Right is { } right ? Bind(right, bindings) : null,
        },
        _ => value,
    };

    private static Test[] Bind(Test[] tests, IReadOnlyDictionary<string, Value> bindings)
        => tests.Select(test => test with { Value = Bind(test.Value, bindings) }).ToArray();

    private (MethodInfo Method, Type? HelperPower, bool OwnedMonster, IReadOnlyDictionary<string, Value>? Bindings)? ReadableHelper(Call call,
        MethodInfo caller, Type? power, IEnumerable<Test[]> paths)
    {
        if (call.Method is not MethodInfo helper || helper == caller || helper.IsSpecialName
            || (helper.ReturnType != typeof(Task) && helper.ReturnType != typeof(void))
            || helper.ContainsGenericParameters || helper.DeclaringType is not { } type)
            return null;

        var onSelf = call.Receiver == Self && type == caller.DeclaringType;
        var onOwner = power is not null && !helper.IsVirtual && type.IsInstanceOfType(monster)
            && call.Receiver is Operation { Operator: "castclass" or "isinst", Left: Call { Method.DeclaringType: var declaring,
                Method.Name: "get_Monster", Receiver: { } creature } }
            && declaring == typeof(Creature) && (IsOwner(creature)
                || paths.All(path => path.Any(test => EstablishesOwner(test, creature))
                    || path.Any(test => EstablishesMonsterType(test, creature, type))));
        var onOwnedPower = power is null && !helper.IsVirtual && typeof(PowerModel).IsAssignableFrom(type)
            && call.Receiver is Call { Method: { Name: "GetPower", IsGenericMethod: true } getPower, Receiver: var ownerCreature }
            && getPower.GetGenericArguments()[0] == type
            && ownerCreature is Call { Method.Name: "get_Creature", Receiver: Input { Name: "this" } };
        if (!onSelf && !onOwner && !onOwnedPower)
            return null;
        var targetPower = onOwnedPower ? type : onOwner ? null : power;
        if (call.Arguments.Length == 0 || Body(helper) is { ReadsParameters: false, Incomplete: false })
            return (helper, targetPower, onOwner, null);
        if (Body(helper).Incomplete)
            return null;
        var bindings = helper.GetParameters().Where(parameter => parameter.Name is not null && parameter.Position < call.Arguments.Length)
            .ToDictionary(parameter => parameter.Name!, parameter => call.Arguments[parameter.Position]);
        return (helper, targetPower, onOwner, bindings);
    }

    private static bool EstablishesOwner(Test test, Value creature)
        => test.Value is Operation { Right: { } right } comparison
            && (comparison.Operator is "ceq" or "beq" && test.Expected
                || comparison.Operator == "bne.un" && !test.Expected)
            && (comparison.Left == creature && IsOwner(right) || right == creature && IsOwner(comparison.Left));

    private static bool EstablishesMonsterType(Test test, Value creature, Type monsterType)
        => test is { Expected: true, Value: Operation { Operator: "isinst",
            Left: Call { Method.Name: "get_Monster", Receiver: var tested },
            Right: Constant { Data: Type t } } } && tested == creature && t == monsterType;

    private static bool IsModelEffect(MethodBase method)
        => method is MethodInfo info && info.DeclaringType is { } type
            && type != typeof(AbstractModel) && type != typeof(MonsterModel) && type != typeof(PowerModel)
            && typeof(AbstractModel).IsAssignableFrom(type)
            && (info.ReturnType == typeof(Task) || info.ReturnType == typeof(void) && !info.IsSpecialName);

    private Effect? DescribeEffect(Call call, MethodInfo caller, Type? power, IReadOnlySet<string> flags,
        IReadOnlyDictionary<string, string> powerTargets, IReadOnlyDictionary<string, string> powerHolders, out bool recognized)
    {
        recognized = true;
        var type = call.Method.DeclaringType;
        var effect = call.Method.Name;
        var args = call.Arguments;
        var generic = call.Method.IsGenericMethod ? call.Method.GetGenericArguments()[0] : null;

        if (IsPresentationCall(call.Method))
        {
            recognized = false;
            return null;
        }

        if (type == typeof(PowerCmd))
        {
            if (effect == "Apply" && generic is not null && args.Length >= 3)
            {
                return ValueText(args[1]) is { } who && ValueText(args[2]) is { } amount
                    ? new Effect($"attempt to apply {amount} {PowerLabel(generic)} to {who}", AddedPower: generic)
                    : null;
            }
            if (effect == "Apply" && generic is null && Argument(call, "power") is { } instance)
            {
                if (AppliedPower(instance) is not { } applied)
                    return null;
                if (Argument(call, "target") is not { } receiving || ValueText(receiving) is not { } who
                    || Argument(call, "amount") is not { } stacks || ValueText(stacks) is not { } amount)
                    return null;
                var forTarget = powerTargets.TryGetValue(Key(instance), out var trackedTarget)
                    ? $" (with target={trackedTarget})"
                    : "";
                return new Effect($"attempt to apply {amount} {PowerLabel(applied)} to {who}{forTarget}", AddedPower: applied);
            }
            if (effect == "Remove")
            {
                if (generic is not null && args.Length >= 1 && ValueText(args[0]) is { } targetWho)
                    return targetWho is "self" or "holder"
                        ? new Effect($"remove {PowerLabel(generic)}")
                        : new Effect($"remove {PowerLabel(generic)} from {targetWho}");
                if (args.Length == 1 && PowerValue(args[0], power) is { } label)
                    return new Effect($"remove {label}");
                return null;
            }
            if (effect == "Decrement" && args.Length == 1)
                return PowerValue(args[0], power) is { } label
                    ? new Effect($"attempt to subtract one {label} stack")
                    : null;
            if (effect == "TickDownDuration" && args.Length == 1)
                return PowerValue(args[0], power) is { } label
                    ? new Effect($"subtract one {label} stack, or clear its skip flag if set")
                    : null;
            if (effect == "ModifyAmount" && args.Length > 2)
                return PowerValue(args[1], power) is { } label && ValueText(args[2]) is { } delta
                    ? new Effect($"change {label} by {delta}")
                    : null;
            return null;
        }

        if (type == typeof(DynamicVar) && effect == "set_BaseValue" && args is [var assigned])
            return DynamicKey(call.Receiver) is { } key && ValueText(assigned) is { } amount
                ? new Effect($"set parameter {key} to {amount}")
                : null;

        if (type == typeof(CreatureCmd))
        {
            if (effect == "Stun" && args.Length >= 1 && IsOwner(args[0]))
            {
                if (args.Length == 1 || args[^1] is Constant { Data: null })
                    return new Effect("attempt to stun the holder for one action");
                var nextMoveArg = args[^1];
                var followUp = nextMoveArg is Constant { Data: string literal } ? literal : MoveIdFromStateId(nextMoveArg);
                if (followUp is not null && ShowableMove(followUp))
                {
                    _forcedMoves.Add(followUp);
                    return new Effect($"attempt to stun the holder for one action, then → {followUp}");
                }
                return null;
            }
            if (effect is "GainBlock" or "Heal" or "SetMaxAndCurrentHp" or "SetMaxHp" && args.Length >= 2)
            {
                if (ValueText(args[0]) is not { } who)
                    return null;
                string? amount = null;
                var unpoweredBlock = false;
                if (effect == "GainBlock" && args[1] is Call { Method.Name: "get_Block" }
                    && power is not null && !MutableDynamicKeys(power).Contains("Block")
                    && ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(power)) is { } canonical
                    && canonical.DynamicVars.TryGetValue("Block", out var blockDyn) && blockDyn is BlockVar blockVar)
                {
                    amount = Number(blockVar.BaseValue);
                    unpoweredBlock = blockVar.Props.HasFlag(ValueProp.Unpowered);
                }
                else
                {
                    amount = ValueText(args[1]);
                }
                if (amount is null)
                    return null;
                var blockKind = unpoweredBlock ? "unpowered" : "base";
                // GainBlock returns zero if the receiver is dead or combat is ending.
                if (effect == "GainBlock" && power is not null && IsOwner(args[0])
                    && caller.Name is "AfterAttack" or "AfterCardPlayed")
                    return new Effect($"if the holder is alive and combat continues: {who} gains {blockKind} {amount} Block");
                return new Effect(effect switch
                {
                    "GainBlock" => $"{who} gains {blockKind} {amount} Block",
                    "Heal" => $"{who} heals {amount} HP",
                    "SetMaxHp" => $"set {who}'s maximum HP to {amount}",
                    _ => $"set {who}'s maximum and current HP to {amount}",
                });
            }
            if (effect == "Kill" && args.FirstOrDefault() is { } target)
            {
                if (ValueText(target) is not { } who)
                    return null;
                var forced = Argument(call, "force") is Constant { Data: 1 or true } ? " (ignoring death prevention)" : "";
                return new Effect($"kill {who}{forced}");
            }
            if (effect == "Damage" && Argument(call, "target", "targets") is { } struck && Argument(call, "amount") is { } dealt)
            {
                if (ValueText(struck) is not { } who || ValueText(dealt) is not { } amount)
                    return null;
                var props = Argument(call, "props") is Constant { Data: int rawProps } ? (ValueProp)rawProps : (ValueProp)0;
                return new Effect($"deal {amount}{DamageFlags(props)} damage to {who}");
            }
            if (effect == "Escape" && args.FirstOrDefault() is { } escaping)
                return ValueText(escaping) is { } who ? new Effect($"{who} escapes combat") : null;
            if (effect == "Add")
            {
                var summonedType = generic is not null && typeof(MonsterModel).IsAssignableFrom(generic)
                    ? generic
                    : args.FirstOrDefault() is { } inst ? SummonedMonsterType(inst) : null;
                if (summonedType is not null)
                    return new Effect($"summon {ModelLabel(summonedType)}", AddedSummon: summonedType);
            }
            if (effect is "TriggerAnim" or "ChangeCreatureAnim")
                recognized = false;
            return null;
        }

        if (effect == "set_HpDisplay" && type == typeof(Creature))
        {
            if (call.Receiver is { } receiver && ValueText(receiver) is { } who
                && args is [Constant { Data: (int)NativeHpDisplay.InfiniteWithoutNumbers }])
                return new Effect($"display {who}'s HP as infinite without numbers");
            if (args is [Constant { Data: int }])
            {
                recognized = false;
                return null;
            }
        }

        if (effect == "SetMoveImmediate" && type == typeof(MonsterModel))
        {
            var validReceiver = IsSelfOrOwnedMonster(call.Receiver)
                || power is not null && call.Receiver is Call { Method.Name: "get_Monster", Receiver: var c } && IsOwner(c);
            var force = args switch
            {
                [_] => 0,
                [_, Constant { Data: int f }] => f,
                _ => -1,
            };
            if (!validReceiver || force is not (0 or 1) || MoveId(args[0]) is not { } id)
                return null;
            if (ShowableMove(id))
            {
                return new Effect(force == 1
                    ? $"immediately force the next move to {id}, bypassing the current move's leave condition"
                    : $"if the current move allows transition: immediately replace it with {id}", ForcedMove: id);
            }
            // A zero-intent pass state (e.g., DEAD_MOVE) skips one enemy action before its public follow-up.
            if (monster.MoveStateMachine?.States.GetValueOrDefault(id) is MoveState { Intents.Count: 0 } passMove
                && HasNoGameplayEffects(passMove)
                && (passMove.FollowUpState ?? (passMove.FollowUpStateId is { } fid ? monster.MoveStateMachine.States.GetValueOrDefault(fid) : null)) is MoveState nextMove
                && EnemyMechanismReader.IsPublicMove(nextMove))
            {
                return new Effect($"immediately set next move to pass one turn, then → {nextMove.Id}", ForcedMove: id);
            }
            return null;
        }

        // Monster or power scalar state (bool or gameplay int).
        if (call.Receiver == Self && effect.StartsWith("set_", StringComparison.Ordinal) && args is [var assignedValue])
        {
            var propName = effect[4..];
            if (type is not null && BackingField(type, (MethodInfo)call.Method) is { } backing && !IsGameplayField(backing))
            {
                recognized = false;
                return null;
            }
            if (args is [Constant { Data: int flag }] && flag is 0 or 1
                && (flags.Contains(propName) || power is not null && type == power
                    && call.Method.GetParameters() is [{ ParameterType: var flagType }] && flagType == typeof(bool)))
                return new Effect($"{propName} = {(flag == 1 ? "true" : "false")}", ChangesGuard: caller.Name == "AfterAttack");
            if (power is null && call.Method.GetParameters() is [{ ParameterType: var intType }] && intType == typeof(int)
                && type is not null && BackingField(type, (MethodInfo)call.Method) is { } intBacking && IsGameplayField(intBacking))
            {
                if (_choiceCursors.Contains(intBacking)
                    && assignedValue is Operation { Operator: "add", Right: Constant { Data: 1 } })
                    return new Effect("increment choice rounds completed", ChangesGuard: true);
                if (ValueText(assignedValue) is { } intText)
                {
                    if (caller.Name == "AfterAddedToRoom")
                        _initializedMonsterInts.Add(propName);
                    else
                        _trackedMonsterInts.Add((propName, intBacking));
                    return new Effect($"{propName} = {intText}", ChangesGuard: true);
                }
            }
        }

        if (call.Method is MethodInfo casterHelper && IsCasterRegistrationHelper(casterHelper)
            && type is not null && args is [var caster] && ValueText(caster) is { } casterWho
            && PowerHolderFromInstance(call.Receiver, powerHolders) is { } powerHolder)
            return new Effect($"register {casterWho} as a caster on {Possessive(powerHolder)} {ModelLabel(type)}");

        if (type is not null && typeof(CardModel).IsAssignableFrom(type) && effect == "FakeUpgrade"
            && args.Length == 0 && call.Receiver is not null && CardUpgradeDamageDelta(type, (MethodInfo)call.Method) is { } upgradeDelta
            && ValueText(call.Receiver) is { } upgradedTarget)
        {
            return new Effect($"increase turn-end damage of {upgradedTarget} by {Number(upgradeDelta)}", AddedCard: type);
        }

        if (type == typeof(CardPileCmd) && effect == "AddToCombatAndPreview" && generic is not null
            && typeof(CardModel).IsAssignableFrom(generic))
        {
            if (Argument(call, "target", "targets") is not { } owner || ValueText(owner) is not { } who
                || Argument(call, "count") is not { } added || ValueText(added) is not { } count
                || Numeric(Argument(call, "pileType")) is not { } pileNum)
                return null;
            var pile = (int)pileNum;
            var position = Numeric(Argument(call, "position")) is { } place ? (CardPilePosition)(int)place : CardPilePosition.Bottom;
            var where = (PileType)pile == PileType.Draw && position != CardPilePosition.Bottom ? $" ({position.ToString().ToLowerInvariant()})" : "";
            return new Effect($"add {count} {ModelLabel(generic)} to {Possessive(who)} {((PileType)pile).ToString().ToLowerInvariant()} pile{where}", AddedCard: generic);
        }

        if (type == typeof(CardPileCmd) && effect == "AddGeneratedCardToCombat")
            return ReadGeneratedCard(call);

        if (type == typeof(CardCmd))
        {
            if (effect == "PreviewCardPileAdd")
            {
                recognized = false;
                return null;
            }
            if (effect is "Afflict" or "AfflictAndPreview" && generic is not null && args.Length >= 2)
                return ValueText(args[0]) is { } cards && ValueText(args[1]) is { } amount
                    ? new Effect($"attempt to apply {ModelLabel(generic)} ({amount}) to {cards}")
                    : null;
            if (effect == "ClearAffliction" && args.Length == 1)
                return ValueText(args[0]) is { } cards
                    ? new Effect($"clear the affliction on {cards}")
                    : null;
            if (effect == "Downgrade" && args.Length == 1)
                return ValueText(args[0]) is { } cards
                    ? new Effect($"downgrade {cards}")
                    : null;
            return null;
        }

        if (type == typeof(AfflictionModel) && effect == "set_Amount" && call.Receiver is not null)
            return ValueText(call.Receiver) is { } cards && ValueText(args[0]) is { } amount
                ? new Effect($"set the stacks of {cards} to {amount}")
                : null;

        if (effect == "Execute" && type?.Name == "AttackCommand")
            return call.Receiver is Call attack ? Attack(attack) : null;

        if (power is null && ReadImmediateChoice(call, caller) is { } choice)
            return new Effect(choice);

        recognized = type is not null && type.Namespace == typeof(CreatureCmd).Namespace
            && type.Name is not ("SfxCmd" or "VfxCmd" or "TalkCmd" or "ThinkCmd" or "Cmd" or "DamageCmd");
        return null;
    }

    private decimal? CardUpgradeDamageDelta(Type cardType, MethodInfo method)
    {
        if (ModelDb.GetByIdOrNull<CardModel>(ModelDb.GetId(cardType)) is not { HasTurnEndInHandEffect: true })
            return null;
        var body = Body(method);
        if (body.Incomplete || body.Writes.Count > 0 || body.FieldSteps.Count > 0)
            return null;
        var upgrade = body.Calls.FirstOrDefault(s => s.Call.Method.Name == "UpgradeValueBy" && s.Call.Arguments.Length == 1
            && s.Call.Receiver is Call { Method.Name: "get_Damage" or "get_HpLoss" });
        return upgrade is not null ? Numeric(upgrade.Call.Arguments[0]) : null;
    }

    private bool HasNoGameplayEffects(MoveState move)
    {
        var performField = AccessTools.Field(typeof(MoveState), "_onPerform");
        if (performField?.GetValue(move) is not Delegate handler)
            return true;
        var body = Body(handler.Method);
        if (body.Incomplete || body.Writes.Count > 0 || body.FieldSteps.Any(s => IsGameplayField(s.Assignment.Target.Member)))
            return false;
        return body.Calls.All(s => IsBenignReadOrPlumbingCall(s.Call.Method));
    }

    private bool IsCasterRegistrationHelper(MethodInfo method)
    {
        if (method.ReturnType != typeof(void) || method.DeclaringType is not { } owner
            || !typeof(PowerModel).IsAssignableFrom(owner) || method.GetParameters() is not [{ ParameterType: var paramType }]
            || paramType != typeof(Creature))
            return false;
        var body = Body(method);
        if (body.Incomplete || body.Writes.Count > 0 || body.FieldSteps.Count > 0)
            return false;
        var mutations = body.Calls.Where(s => !IsBenignReadOrPlumbingCall(s.Call.Method)).ToArray();
        return mutations is [{ Call: { Method.Name: "Add", Receiver: Field f, Arguments: [Input] } }]
            && InternalDataField(f) is not null;
    }

    private string? PowerHolderFromInstance(Value? receiver, IReadOnlyDictionary<string, string> powerHolders) => receiver switch
    {
        not null when powerHolders.TryGetValue(Key(receiver), out var tracked) => tracked,
        Call { Method.Name: "GetPower", Receiver: { } holder } => ValueText(holder),
        Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call { Method.Name: "Apply" } applyCall } }
            when Argument(applyCall, "target") is { } target => ValueText(target),
        _ => null,
    };

    private static Test[] StripImpliedTypeFilter(Value action, Test[] conditions)
    {
        if (action is Call { Method.Name: "FakeUpgrade", Receiver: Operation { Operator: "isinst" or "castclass",
                Left: Call { Method.Name: "get_Current" } elem, Right: Constant { Data: Type cardType } } }
            && typeof(CardModel).IsAssignableFrom(cardType))
        {
            return conditions.Where(t => !(t is { Expected: true, Value: Operation { Operator: "isinst", Left: var tested, Right: Constant { Data: Type ct } } }
                && tested == elem && ct == cardType)).ToArray();
        }
        return conditions;
    }

    internal static string Possessive(string who)
        => who.EndsWith("s", StringComparison.Ordinal) ? $"{who}'" : $"{who}'s";

    private static Type? SummonedMonsterType(Value? value) => value switch
    {
        Operation { Operator: "castclass" or "isinst", Left: var inner } => SummonedMonsterType(inner),
        Call { Method.Name: "ToMutable", Receiver: var canonical } => SummonedMonsterType(canonical),
        Call { Method: { Name: "Monster", IsGenericMethod: true } lookup } when lookup.DeclaringType == typeof(ModelDb)
            => lookup.GetGenericArguments()[0],
        _ => null,
    };

    // Overloads order their operands differently, so bind by parameter name.
    private static Value? Argument(Call call, params string[] names)
        => call.Method.GetParameters().FirstOrDefault(parameter => names.Contains(parameter.Name)) is { } match
            && match.Position < call.Arguments.Length
            ? call.Arguments[match.Position]
            : null;

    private Effect? Attack(Call receiver)
    {
        Value? amount = null;
        Value hits = new Constant(1);
        for (var call = receiver; ; call = (Call)call.Receiver)
        {
            if (call.Method.DeclaringType == typeof(DamageCmd) && call.Method.Name == "Attack")
                amount = call.Arguments.FirstOrDefault();
            if (call.Method.Name == "WithHitCount" && call.Arguments.Length != 0)
                hits = call.Arguments[0];
            if (call.Receiver is not Call)
                break;
        }
        if (amount is null || ValueText(amount) is not { } damage || ValueText(hits) is not { } count)
            return null;
        var size = count == "1" ? damage : $"{damage}×{count}";
        return new Effect($"base attack {size}", Attack: true);
    }

    private string? MoveIdFromStateId(Value value)
        => value is Call { Method.Name: "get_StateId", Receiver: { } state } ? MoveId(state) : null;

    private string? MoveId(Value value)
    {
        if (value is Call { Method: MethodInfo getter, Receiver: var receiver, Arguments.Length: 0 }
            && getter.Name.StartsWith("get_", StringComparison.Ordinal)
            && IsSelfOrOwnedMonster(receiver)
            && Body(getter).Returns is [{ Value: Field { Receiver: Input { Name: "this" } } field, Conditions.Length: 0 }]
            && field.Member.DeclaringType?.IsInstanceOfType(monster) == true)
            return (field.Member.GetValue(monster) as MoveState)?.Id;
        return null;
    }

    private bool IsSelfOrOwnedMonster(Value? receiver)
        => receiver == Self
            || receiver is Operation { Operator: "castclass" or "isinst", Left: Call { Method.Name: "get_Monster" }, Right: Constant { Data: Type t } }
                && t.IsInstanceOfType(monster)
            || receiver is Input { Type: { } paramType } && paramType.IsInstanceOfType(monster);

    private string? PowerValue(Value value, Type? power)
        => value switch
        {
            Input { Name: "this" } when power is not null => PowerLabel(power),
            Call call when call.Method.Name == "GetPower" && call.Method.DeclaringType == typeof(Creature)
                && call.Method.IsGenericMethod && IsOwner(call.Receiver) => PowerLabel(call.Method.GetGenericArguments()[0]),
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator",
                Receiver: Call { Method.Name: "ToList", Arguments: [Call { Method: { Name: "GetPowerInstances", IsGenericMethod: true } inst, Receiver: var who }] } } }
                when IsOwner(who) => $"each {PowerLabel(inst.GetGenericArguments()[0])}",
            _ => null,
        };

    private static string PowerLabel(Type type) => ModelLabel(type);

    // A command can apply a power instance that the caller created from the canonical model.
    private static Type? AppliedPower(Value value) => value switch
    {
        Operation { Operator: "castclass" or "isinst", Left: var inner } => AppliedPower(inner),
        Call { Method.Name: "ToMutable", Receiver: { } canonical } => AppliedPower(canonical),
        Call { Method: { Name: "Power", IsGenericMethod: true } lookup } when lookup.DeclaringType == typeof(ModelDb)
            => lookup.GetGenericArguments()[0],
        Call { Method: { Name: "GetPower", IsGenericMethod: true } existing } when existing.DeclaringType == typeof(Creature)
            => existing.GetGenericArguments()[0],
        _ => null,
    };

    private static bool IsOwner(Value? value)
        => value is Call { Receiver: Input { Name: "this" }, Method.Name: "get_Owner" or "get_Creature" };

    private static string? DynamicKey(Value? value)
        => value is Call { Method.DeclaringType: var type, Method.Name: "get_Item", Arguments: [Constant { Data: string key }],
            Receiver: Call { Method.Name: "get_DynamicVars", Receiver: Input { Name: "this" } } }
            && type == typeof(DynamicVarSet) ? key : null;

    private bool ShowableMove(string id)
        => monster.MoveStateMachine?.States.GetValueOrDefault(id) is MoveState move && EnemyMechanismReader.IsPublicMove(move);

    private string? BooleanGetter(Value value)
        => value is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" } }
            && getter.ReturnType == typeof(bool) && getter.Name.StartsWith("get_", StringComparison.Ordinal)
            && (getter.DeclaringType?.IsInstanceOfType(monster) == true || getter.DeclaringType is { } owner && owner == _hookOwner)
            ? getter.Name[4..]
            : null;

    private string? BooleanField(Value value)
    {
        if (value is not Field { Receiver: Input { Name: "this" } } field || field.Member.FieldType != typeof(bool)
            || field.Member.DeclaringType is not { } type || !type.IsInstanceOfType(monster))
            return null;
        if (_flagNames.TryGetValue(field.Member, out var cached))
            return cached;

        var name = field.Member.Name.TrimStart('_');
        var property = name.Length > 0 ? AccessTools.Property(type, char.ToUpperInvariant(name[0]) + name[1..]) : null;
        var alias = property?.GetMethod is { } getter && Body(getter) is { Incomplete: false,
            Returns: [{ Value: Field { Receiver: Input { Name: "this" }, Member: var backing }, Conditions.Length: 0 }] }
            && backing == field.Member ? property.Name : null;
        _flagNames.Add(field.Member, alias);
        return alias;
    }

    private static bool IsModelDbType(Type type)
        => typeof(MonsterModel).IsAssignableFrom(type) && type != typeof(MonsterModel)
            || typeof(PowerModel).IsAssignableFrom(type) && type != typeof(PowerModel)
            || typeof(CardModel).IsAssignableFrom(type) && type != typeof(CardModel)
            || typeof(RelicModel).IsAssignableFrom(type) && type != typeof(RelicModel)
            || typeof(AfflictionModel).IsAssignableFrom(type) && type != typeof(AfflictionModel);

    private static string ModelLabel(Type type)
    {
        var id = ModelDb.GetId(type);
        var title = ModelDb.GetByIdOrNull<AbstractModel>(id) switch
        {
            PowerModel power => power.Title,
            AfflictionModel affliction => affliction.Title,
            MonsterModel monster => MegaCrit.Sts2.Core.Nodes.NGame.Instance is not null
                || monster.GetType().GetProperty(nameof(MonsterModel.Title))?.DeclaringType == typeof(MonsterModel)
                    ? monster.Title
                    : MonsterModel.L10NMonsterLookup($"{monster.Id.Entry}.name"),
            RelicModel relic => relic.Title,
            CardModel card => card.TitleLocString,
            null when typeof(MonsterModel).IsAssignableFrom(type) => MonsterModel.L10NMonsterLookup($"{id.Entry}.name"),
            null when typeof(PowerModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("powers", $"{id.Entry}.title"),
            null when typeof(CardModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("cards", $"{id.Entry}.title"),
            _ => null,
        };
        return title?.Exists() == true ? $"{title.GetFormattedText()} ({id})" : id.ToString();
    }

    private string? RenderReturns(MethodInfo method, Func<Value, string?> render)
    {
        var body = Body(method);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        var candidates = new List<GuardedEffects>();
        foreach (var result in body.Returns)
        {
            if (Guard(result.Conditions) is not { } guard)
                return null;
            if (guard == Never)
                continue;
            if (render(result.Value) is not { } text)
                return null;
            candidates.Add(new(result.Conditions, guard, [new Effect(text)]));
        }
        return string.Join("; ", RenderBranches(candidates).Select(effect => effect.Text).Distinct());
    }

    private string? Guard(Test[] tests)
    {
        var conditions = new List<string>();
        foreach (var test in tests)
        {
            if (Condition(test) is not { } text)
                return null;
            if (text == Never)
                return Never;
            if (text.Length != 0)
                conditions.Add(text);
        }
        var distinct = conditions.Distinct().ToList();
        // Drop `<subject> exists` when `<subject> has ...` already appears in the same guard.
        distinct.RemoveAll(item => item.EndsWith(" exists", StringComparison.Ordinal)
            && distinct.Any(other => other != item && other.StartsWith(item[..^" exists".Length] + " has ", StringComparison.Ordinal)));
        // Drop an abstract monster-family check when the guard already equates event target with holder of that family.
        if (distinct.Contains("holder=event target") || distinct.Contains("event target=holder"))
        {
            for (var baseType = monster.GetType().BaseType; baseType is not null && baseType != typeof(MonsterModel); baseType = baseType.BaseType)
                if (baseType.IsAbstract)
                    distinct.Remove($"event target is {ModelLabel(baseType)}");
        }
        return string.Join(" and ", distinct);
    }

    // A guard that can never hold marks a path the current run cannot take.
    private const string Never = "never";

    private static string When(string guard, string text)
        => guard.Length == 0 ? text
            : text.StartsWith("if ", StringComparison.Ordinal) ? $"if {guard} and {text[3..]}"
            : $"if {guard}: {text}";

    private static bool IsDreadTest(Test test)
        => test.Value is Call { Method.DeclaringType: var type, Method.Name: "IsDread" } && type == typeof(AscensionHelper);

    private static bool IsCurrentRun(Test test) => !IsDreadTest(test) || test.Expected == AscensionHelper.IsDread();

    private string? Condition(Test test)
    {
        var value = test.Value;
        var expected = test.Expected;
        if (IsDreadTest(test))
            return IsCurrentRun(test) ? "" : "never";
        if (value is Call { Method.DeclaringType.Name: "LocalContext", Method.Name: "IsMe" })
            return expected ? "" : Never;
        if (CombatCondition(test) is { } combat)
            return combat;
        if (value is Operation { Operator: "ceq", Right: Constant { Data: 0 or null } } negation
            && (negation.Left is Call { Method: MethodInfo { ReturnType: var type } } && type == typeof(bool)
                || negation.Left is Operation { Operator: "ceq" or "cgt" or "cgt.un" or "clt" or "clt.un" }
                || negation.Left is Operation { Operator: "isinst" }))
            return Condition(test with { Value = negation.Left, Expected = !expected });
        if (value is Operation { Operator: "cgt.un", Left: Operation { Operator: "isinst" } positiveTypeCheck, Right: Constant { Data: 0 or null } })
            return Condition(test with { Value = positiveTypeCheck, Expected = expected });
        if (value is Operation { Operator: "isinst", Left: var operand, Right: Constant { Data: Type target } }
            && IsModelDbType(target) && ValueText(operand) is { } entity)
            return $"{entity} {(expected ? "is" : "is not")} {ModelLabel(target)}";
        if (value is Operation operation && operation.Right is { } right)
        {
            var op = operation.Operator switch
            {
                "beq" or "ceq" => expected ? "=" : "≠",
                "bne.un" => expected ? "≠" : "=",
                "bgt" or "cgt" or "cgt.un" => expected ? ">" : "≤",
                "bge" => expected ? "≥" : "<",
                "ble" => expected ? "≤" : ">",
                "blt" or "clt" => expected ? "<" : "≥",
                _ => null,
            };
            if (op is not null && Numeric(operation.Left) is { } leftNumber && Numeric(right) is { } rightNumber)
                return op switch
                {
                    "=" => leftNumber == rightNumber,
                    "≠" => leftNumber != rightNumber,
                    ">" => leftNumber > rightNumber,
                    "≤" => leftNumber <= rightNumber,
                    "≥" => leftNumber >= rightNumber,
                    _ => leftNumber < rightNumber,
                } ? "" : Never;
            if (operation.Left is Call { Method.Name: "get_NextMove", Receiver: Input { Name: "this" } }
                && MoveId(right) is { } move && ShowableMove(move) && op is "=" or "≠")
                return $"next move{op}{move}";
            if (operation.Left is Input { Name: "side" } && Numeric(right) is { } side && op is not null)
                return $"turn side{op}{SideLabel((CombatSide)side)}";
            if (operation.Left is Call { Method.Name: "get_CurrentSide" } && Numeric(right) is { } acting && op is "=" or "≠")
                return $"acting side{op}{SideLabel((CombatSide)acting)}";
            if (operation.Left is Call { Method.Name: "get_Side", Receiver: var creature } && IsOwner(creature)
                && Numeric(right) is { } ownerSide && op is "=" or "≠")
                return $"holder's side{op}{SideLabel((CombatSide)ownerSide)}";
            if (operation.Left is Call { Method.Name: "get_Side", Receiver: Input { Name: "creature" } }
                && right is Call { Method.Name: "get_Side", Receiver: var ownerSideCreature } && IsOwner(ownerSideCreature)
                && op is "=" or "≠")
                return $"event target's side{op}holder's side";
            if (operation.Left is Call { Method.Name: "get_Type", Receiver: var typedCard }
                && Numeric(right) is { } cardType && (CardType)cardType == CardType.Skill && op is "=" or "≠")
            {
                var subject = typedCard is Call { Method.Name: "get_Card" } ? "played card" : "that card";
                return op == "=" ? $"{subject} is a Skill" : $"{subject} is not a Skill";
            }
            if (operation.Left is Call { Method.Name: "get_Affliction" } && right is Constant { Data: null } && op is "=" or "≠")
                return op == "=" ? "that card has no affliction" : "that card already has an affliction";
            if (operation.Left is Input { Name: "power" } && right == Self && op is "=" or "≠")
                return op == "=" ? "this power changed" : "a different power changed";
            if (operation.Left is Call { Method: { Name: "GetRelic" or "GetPower", IsGenericMethod: true } lookup, Receiver: { } holder }
                && right is Constant { Data: null } && op is "=" or "≠" && ValueText(holder) is { } who)
                return $"{who} {(op == "≠" ? "has" : "lacks")} {ModelLabel(lookup.GetGenericArguments()[0])}";
            if (ValueText(operation.Left) is { } leftText && ValueText(right) is { } rightText && op is not null)
                return $"{leftText}{op}{rightText}";
            return null;
        }

        if (value is Call call)
        {
            var name = call.Method.Name;
            if (name == "Contains" && call.Arguments is [Input { Name: "participants" }, Call { Method.Name: "get_Owner" }])
                return expected ? "holder participates this turn" : "holder does not participate this turn";
            if (name is "HasPower" or "GetPower" && call.Method.DeclaringType == typeof(Creature) && call.Method.IsGenericMethod
                && IsOwner(call.Receiver))
                return $"{(expected ? "has" : "lacks")} {PowerLabel(call.Method.GetGenericArguments()[0])}";
            if (name == "IsPoweredAttack")
                return expected ? "hit has Move without Unpowered" : "hit is not a powered attack";
            if (name == "IsPoweredCardOrMonsterMoveBlock")
                return expected ? "block has Move without Unpowered" : "block is not from a powered card or move";
            if (name == "get_UnblockedDamage")
                return expected ? "nonzero HP lost" : "no HP lost";
            if (name == "get_Affliction")
                return expected ? "that card already has an affliction" : "that card has no affliction";
            if (name == "get_IsPlayer" && call.Receiver is Call { Method.Name: "get_Current",
                Receiver: Call { Method.Name: "GetEnumerator",
                    Receiver: Call { Method.Name: "ToList", Arguments: [Call { Method.Name: "get_Allies" }] } } })
                return expected ? "" : "ally is not a player"; // The card collection expression already quantifies over player combat decks.
            if (name == "get_CombatState" && call.Receiver is Input { Name: "oldOwner" })
                return expected ? "former holder still attached to combat" : "former holder detached from combat";
            if (name == "get_IsAlive" && call.Receiver is Call { Method.Name: "get_Creature" })
                return expected ? "self alive" : "self dead";
            if (name == "get_IsDead" && call.Receiver is { } deadCheck && ValueText(deadCheck) is { } deadWho)
                return expected ? $"{deadWho} is dead" : $"{deadWho} is alive";
            if (name == "get_IsPlayer" && call.Receiver is { } isPlayerRec && ValueText(isPlayerRec) is { } isPlayerWho)
                return expected ? $"{isPlayerWho} is a player" : $"{isPlayerWho} is not a player";
            if (name == "get_IsEnemy" && IsOwner(call.Receiver))
                return expected ? "holder is an enemy" : "holder is not an enemy";
            if (IsFreeSlotSearch(call))
                return expected ? "an encounter slot is empty" : "no encounter slot is empty";
            if (name == "get_Player" && call.Method.DeclaringType == typeof(Creature) && ValueText(call.Receiver) is { } playerWho)
                return expected ? $"{playerWho} is a player" : $"{playerWho} is not a player";
            if (BooleanGetter(call) is { } property)
            {
                if (call.Method.DeclaringType is { } ownerType && BackingField(ownerType, (MethodInfo)call.Method) is { } bField
                    && !IsGameplayField(bField))
                    return "";
                return $"{property}={expected.ToString().ToLowerInvariant()}";
            }
        }

        if (BooleanField(value) is { } field)
        {
            if (value is Field f && !IsGameplayField(f.Member))
                return "";
            return $"{field}={expected.ToString().ToLowerInvariant()}";
        }

        // AfterDeath sets this when death is prevented, regardless of whether a corpse stays in combat.
        if (value is Input { Name: "wasRemovalPrevented" })
            return expected ? "death was prevented" : "death was not prevented";

        // A truth test on an object reference checks that it exists.
        if ((value is Input { Type.IsValueType: false } || value is Call { Method: MethodInfo { ReturnType.IsValueType: false } })
            && ValueText(value) is { } reference)
            return expected ? $"{reference}≠none" : $"{reference}=none";

        return NativeRuleBody.TryTruth(value, out var truth)
            ? (truth == expected ? "" : "never")
            : null;
    }

    // Summoners look for an encounter slot that no enemy occupies.
    private bool IsFreeSlotSearch(Call call)
        => call is { Method.Name: "LastOrDefault" or "FirstOrDefault", Arguments: [Call { Method.Name: "get_Slots" },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo predicate }] }] }
            && Body(predicate) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "All",
                Arguments: [Call { Method.Name: "get_Enemies" }, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo occupant }] }] },
                Conditions.Length: 0 }] }
            && Body(occupant) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "op_Inequality",
                Arguments: [Call { Method.Name: "get_SlotName" }, _] }, Conditions.Length: 0 }] };

    private static string SideLabel(CombatSide side) => side switch
    {
        CombatSide.Enemy => "enemy",
        CombatSide.Player => "player",
        _ => side.ToString(),
    };

    private string? ValueText(Value? value)
    {
        if (value is null)
            return null;
        if (Numeric(value) is { } number)
            return Number(number);
        if (CombatValue(value) is { } combat)
            return combat;

        return value switch
        {
            Input { Name: "this" } => "self",
            Input { Name: "oldOwner" } => "former holder",
            Input { Name: "targets" } => "player targets",
            Input { Name: "target" } => "target",
            Input { Name: "dealer" } => "damage dealer",
            Input { Name: "cardSource" } => "source card",
            Input { Name: "amount" } => "amount",
            Input { Name: "power" } => "changed power",
            Input { Name: "card" } => "that card",
            Input { Name: "creature" } => "event target",
            Constant { Data: null } => "none",
            Constant { Data: string text } => text,
            Operation { Operator: "castclass" or "isinst", Left: Input { Name: "card" } } => "that card",
            Operation { Operator: "castclass" or "isinst", Left: var inner, Right: Constant { Data: Type castType } }
                when typeof(CardModel).IsAssignableFrom(castType) && ValueText(inner) is { } innerCards
                => $"each {ModelLabel(castType)} in {(innerCards.StartsWith("that card in ", StringComparison.Ordinal) ? innerCards["that card in ".Length..] : innerCards)}",
            Operation { Operator: "castclass" or "isinst", Left: var innerMonster, Right: Constant { Data: Type mType } }
                when typeof(MonsterModel).IsAssignableFrom(mType) && ValueText(innerMonster) is { } whoMonster
                => whoMonster,
            Call { Method.Name: "get_Amount", Receiver: Input { Name: "this" } } => "this power's current stacks",
            Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card" } } } => "card player",
            Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card" } } => "card player",
            Call { Method.Name: "get_Creature", Receiver: Input { Name: "this" } } => "self",
            Call { Method.Name: "get_Owner", Receiver: Input { Name: "this" } } => "holder",
            Call { Method.Name: "get_Monster", Receiver: Input { Name: "creature" } } => "event target",
            Call { Method.Name: "get_Monster", Receiver: var ownerRec } when IsOwner(ownerRec) => "holder",
            Call { Method.Name: "get_Monster", Method.DeclaringType: var mCreature, Receiver: { } mRec }
                when mCreature == typeof(Creature) => ValueText(mRec),
            Call { Method.Name: "get_RoundNumber" } => "round number",
            Call { Method.Name: "get_TurnNumber", Receiver: Call { Method.Name: "get_PlayerCombatState",
                Receiver: Call { Method.Name: "get_Player", Receiver: var creature } } } when IsOwner(creature)
                => "holder's turn number",
            Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_Players", Receiver: Call { Method.Name: "get_RunState" } } }
                => "number of players in the run",
            Call { Method.Name: "get_IsPlayer" } => "holder is a player",
            Call { Method.Name: "get_Card" } => "played card",
            Call { Method.Name: "get_Affliction", Receiver: Call { Method.Name: "get_Current",
                Receiver: Call { Method.Name: "GetEnumerator", Receiver: not Call { Method.Name: "Where" } } } } => "that card's affliction",
            Call { Method.Name: "get_Affliction", Receiver: { } card } => ValueText(card) is { } label
                ? $"{Possessive(label)} affliction"
                : null,
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: Call { Method.Name: "Where" } filter } } => FilteredSet(filter),
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: Call { Method.Name: "get_AllCards" } allCards } }
                when ValueText(allCards) is { } cardsLabel => $"that card in {cardsLabel}",
            // A loop body describes each element of the collection it iterates.
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: { } collection } } => ValueText(collection),
            Call { Method.Name: "NewReadOnlySingleElementList" or ".ctor", Arguments: [var singleCard] }
                when ValueText(singleCard) is { } singleLabel => singleLabel,
            Call { Method.Name: "get_AllCards", Receiver: Call { Method.Name: "get_PlayerCombatState", Receiver: { } player } }
                when ValueText(player) is { } who => $"{Possessive(who)} combat cards",
            Call { Method.Name: "get_Applier", Receiver: Input { Name: "this" } } => "this power's applier",
            Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } } => "that card's owner",
            Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call { Method: { Name: "Add", IsGenericMethod: true } added } } }
                when added.DeclaringType == typeof(CreatureCmd) => $"the summoned {ModelLabel(added.GetGenericArguments()[0])}",
            Call { Method.Name: "NextInt", Method.DeclaringType.Name: "Rng", Arguments: [var low, var high] }
                when ValueText(low) is { } least && Numeric(high) is { } bound => $"random {least}–{Number(bound - 1)}",
            Call { Method.Name: "ScaleHpForMultiplayer", Arguments: [var hp, ..] } when ValueText(hp) is { } amount
                => $"{amount} (scaled for multiplayer)",
            Call { Method.Name: "get_BaseValue" or "get_IntValue", Method.DeclaringType: var type, Receiver: var variable }
                when typeof(DynamicVar).IsAssignableFrom(type) && DynamicKey(variable) is { } key => $"parameter {key}",
            Call { Method.Name: "op_Increment", Method.DeclaringType: var type, Arguments: [var inner] }
                when type == typeof(decimal) && ValueText(inner) is { } incText => $"({incText} + 1)",
            Call { Method.Name: "op_Decrement", Method.DeclaringType: var type, Arguments: [var inner] }
                when type == typeof(decimal) && ValueText(inner) is { } decText => $"({decText} − 1)",
            Call { Method.Name: "op_Implicit", Method.DeclaringType: var type, Arguments: [var amount] } when type == typeof(decimal)
                => ValueText(amount),
            Call { Method.Name: "op_UnaryNegation", Method.DeclaringType: var type, Arguments: [var delta] }
                when type == typeof(decimal) && ValueText(delta) is { } text => $"−{text}",
            Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_Players", Receiver: Input { Name: "combatState" } } }
                => "number of players in combat",
            Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_Players",
                Receiver: Call { Method.Name: "get_CombatState", Receiver: Input { Name: "this" } } } }
                => "number of players in combat",
            Call { Method.Name: "get_Player", Method.DeclaringType: var type, Receiver: { } creature } when type == typeof(Creature)
                => ValueText(creature),
            Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: { } model } }
                when ValueText(model) is { } owner => $"{owner}'s owner",
            Call { Method.DeclaringType: var type, Arguments: [var left, var right] } arithmetic when type == typeof(decimal)
                && Arithmetic(arithmetic.Method.Name) is { } symbol => Formula(left, symbol, right),
            Operation { Right: { } right } operation when Arithmetic(operation.Operator) is { } symbol
                => Formula(operation.Left, symbol, right),
            _ => null,
        };

        string? Formula(Value left, string symbol, Value right)
            => ValueText(left) is { } a && ValueText(right) is { } b ? $"({a} {symbol} {b})" : null;
    }

    private string? FilteredSet(Call filter)
    {
        if (filter.Arguments is not [var source, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo selector }] }])
            return null;
        if (Body(selector).Returns is not [{ Value: var test, Conditions.Length: 0 }])
            return null;
        if (source is Call { Method.Name: "ToList", Arguments: [var collection] })
            source = collection;
        if (source is Call { Method.Name: "GetTeammatesOf", Arguments: [var focus] } && IsOwner(focus)
            && test is Operation { Operator: "ceq", Right: Constant { Data: 0 },
                Left: Operation { Operator: "ceq", Left: Input, Right: var excluded } } && IsOwner(excluded))
            return "each other teammate";
        if (source is not Call { Method.Name: "get_AllCards" })
            return null;
        if (test is Call { Method.Name: "get_IsUpgraded", Receiver: Input })
            return "each upgraded combat card";
        if (test is Operation { Operator: "ceq", Left: Call { Method.Name: "get_Type" }, Right: Constant { Data: int cardType } }
            && (CardType)cardType == CardType.Skill)
            return "each Skill in combat";
        if (test is Operation { Operator: "cgt.un", Left: Operation { Operator: "isinst", Right: Constant { Data: Type type } }, Right: Constant { Data: null } }
            && typeof(AfflictionModel).IsAssignableFrom(type))
            return $"each combat card with {ModelLabel(type)}";
        return null;
    }

    private decimal? Numeric(Value? value, int depth = 0)
    {
        if (value is null || depth >= 8)
            return null;
        switch (value)
        {
            case Constant { Data: int or long or float or double or decimal } literal:
                return Convert.ToDecimal(literal.Data, CultureInfo.InvariantCulture);
            case Field { Member.DeclaringType: var type, Receiver: null } field when type == typeof(decimal):
                return (decimal?)field.Member.GetValue(null);
            case Call { Method.Name: "get_Count", Receiver: Field { Member: var table, Receiver: null } }
                when _choiceRoundCounts.TryGetValue(table, out var rounds):
                return rounds;
            case Call { Method.DeclaringType: var type, Arguments: [var arg] } wrapper when type == typeof(decimal)
                && wrapper.Method.Name is ".ctor" or "op_Implicit":
                return Numeric(arg, depth + 1);
            // The compiler emits decimal literals such as 0.75m through the five-part constructor.
            case Call { Method: ConstructorInfo { DeclaringType: var type }, Arguments: [Constant { Data: int low }, Constant { Data: int middle },
                Constant { Data: int high }, Constant { Data: int negative }, Constant { Data: int scale }] } when type == typeof(decimal):
                return new decimal(low, middle, high, negative != 0, (byte)scale);
            case Call { Method.DeclaringType: var type, Method.Name: "GetValueIfAscension", Arguments: [Constant { Data: int level }, var high, var low] }
                when type == typeof(AscensionHelper):
                return Numeric(AscensionHelper.HasAscension((AscensionLevel)level) ? high : low, depth + 1);
            case Call { Method.DeclaringType: var type, Method.Name: "IsDread", Receiver: null }
                when type == typeof(AscensionHelper):
                return AscensionHelper.IsDread() ? 1m : 0m;
            case Operation { Operator: "ceq" or "cgt" or "cgt.un" or "clt" or "clt.un", Left: var left, Right: { } right } cmp
                when Numeric(left, depth + 1) is { } cmpLeft && Numeric(right, depth + 1) is { } cmpRight:
                return cmp.Operator switch
                {
                    "ceq" => cmpLeft == cmpRight ? 1m : 0m,
                    "cgt" or "cgt.un" => cmpLeft > cmpRight ? 1m : 0m,
                    _ => cmpLeft < cmpRight ? 1m : 0m,
                };
            case Call { Method: MethodInfo getter, Receiver: var receiver, Arguments.Length: 0 }
                when (receiver == Self || receiver is null && getter.IsStatic)
                    && getter.Name.StartsWith("get_", StringComparison.Ordinal) && getter.ReturnType is { } returnType
                    && (returnType == typeof(int) || returnType == typeof(decimal) || returnType == typeof(float))
                    && getter.DeclaringType?.IsInstanceOfType(monster) == true:
                return NumericGetter(getter, depth + 1);
            case Operation { Operator: "neg" } negation:
                return -Numeric(negation.Left, depth + 1);
            case Operation { Right: null } conversion when Numeric(conversion.Left, depth + 1) is { } valueBeforeConversion:
                return conversion.Operator switch
                {
                    "conv.i4" when valueBeforeConversion is >= int.MinValue and <= int.MaxValue => decimal.Truncate(valueBeforeConversion),
                    "conv.i8" when valueBeforeConversion is >= long.MinValue and <= long.MaxValue => decimal.Truncate(valueBeforeConversion),
                    "conv.r4" => (decimal)(float)valueBeforeConversion,
                    "conv.r8" => (decimal)(double)valueBeforeConversion,
                    _ => null,
                };
            case Call { Method.DeclaringType: var dynType, Method.Name: "get_BaseValue" or "get_IntValue",
                Receiver: Call { Method.Name: "get_Strength" } }
                when typeof(DynamicVar).IsAssignableFrom(dynType) && _hookOwner is { } strOwner
                && !MutableDynamicKeys(strOwner).Contains("Strength") && !MutableDynamicKeys(strOwner).Contains("StrengthPower")
                && ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(strOwner)) is { } strPower:
                return strPower.DynamicVars.Strength.BaseValue;
            case Call { Method.DeclaringType: var type, Method.Name: "get_BaseValue" or "get_IntValue", Receiver: var variable }
                when typeof(DynamicVar).IsAssignableFrom(type) && _hookOwner is { } power
                && DynamicKey(variable) is { } key && !MutableDynamicKeys(power).Contains(key):
                // A power's static parameters come from its canonical model; mutable runtime counters stay symbolic.
                return ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(power))?.DynamicVars.TryGetValue(key, out var parameter) == true
                    ? parameter.BaseValue
                    : null;
            case Call { Method.DeclaringType: var type, Arguments: [var left, var right] } arithmetic when type == typeof(decimal)
                && Arithmetic(arithmetic.Method.Name) is { } symbol:
                return Calculate(symbol, Numeric(left, depth + 1), Numeric(right, depth + 1));
            case Operation { Right: { } right } operation when Arithmetic(operation.Operator) is { } symbol:
                return Calculate(symbol, Numeric(operation.Left, depth + 1), Numeric(right, depth + 1));
            default:
                return null;
        }
    }

    private static string? Arithmetic(string operation) => operation switch
    {
        "add" or "op_Addition" => "+",
        "sub" or "op_Subtraction" => "−",
        "mul" or "op_Multiply" => "×",
        "div" or "op_Division" => "÷",
        _ => null,
    };

    private static decimal? Calculate(string symbol, decimal? left, decimal? right) => symbol switch
    {
        "+" => left + right,
        "−" => left - right,
        "×" => left * right,
        "÷" when right is not 0 => left / right,
        _ => null,
    };

    private decimal? NumericGetter(MethodInfo getter, int depth)
    {
        var body = Body(getter);
        if (body.Incomplete || body.Returns.Any(result => !result.Conditions.All(IsDreadTest)))
            return null;
        var active = body.Returns.Where(result => result.Conditions.All(IsCurrentRun)).ToArray();
        return active is [{ } result] ? Numeric(result.Value, depth) : null;
    }

    private static string Number(object? value)
        => Convert.ToDecimal(value, CultureInfo.InvariantCulture).ToString("0.########", CultureInfo.InvariantCulture);

    private static IEnumerable<Test[]> MergeTests(IEnumerable<Test[]> input)
    {
        var variants = input.Where(path => !path.Where(test => IsStable(test.Value)).GroupBy(TestKey)
                .Any(group => group.Any(test => test.Expected) && group.Any(test => !test.Expected)))
            .Select(path => path.DistinctBy(test => (TestKey(test), test.Expected)).ToArray())
            .DistinctBy(path => string.Join("|", path.Select(test => $"{test.Expected}:{TestKey(test)}"))).ToList();
        for (var left = 0; left < variants.Count; left++)
        {
            for (var right = left + 1; right < variants.Count; right++)
            {
                var a = variants[left];
                var b = variants[right];
                if (a.Length != b.Length)
                    continue;
                var extraA = a.Where(test => !b.Any(other => other.Expected == test.Expected && TestKey(other) == TestKey(test))).ToArray();
                var extraB = b.Where(test => !a.Any(other => other.Expected == test.Expected && TestKey(other) == TestKey(test))).ToArray();
                if (extraA is not [{ } first] || extraB is not [{ } second]
                    || first.Expected == second.Expected || TestKey(first) != TestKey(second))
                    continue;
                variants[left] = a.Where(test => test != first).ToArray();
                variants.RemoveAt(right);
                left = -1;
                break;
            }
        }
        return variants.DistinctBy(path => string.Join("|", path.Select(test => $"{test.Expected}:{TestKey(test)}")));
    }

    // A power cannot change owners. The creature's side and player binding stay fixed.
    // Other getters can observe a different value at a later instruction.
    private static bool IsStable(Value value) => value switch
    {
        Constant => true,
        Input { Name: not ("?" or "async_state") } => true,
        Operation { Right: { } right } operation => IsStable(operation.Left) && IsStable(right),
        Call { Method.Name: "get_Owner", Method.DeclaringType: var type, Receiver: Input { Name: "this" } }
            when type == typeof(PowerModel) => true,
        Call { Method.Name: "get_Side" or "get_Player", Method.DeclaringType: var type, Receiver: { } receiver }
            when type == typeof(Creature) => IsStable(receiver),
        _ => false,
    };

    private static string TestKey(Test test) => IsStable(test.Value) ? Key(test.Value) : $"{test.Offset}:{Key(test.Value)}";

    private static string Key(Value? value) => value switch
    {
        Constant { Data: string text } => JsonSerializer.Serialize(text),
        Constant { Data: { } data } => $"[{data.GetType().FullName}:{data}]",
        Constant => "[null]",
        Input input => input.Name,
        LocalAddress addr => $"&local[{addr.Slot}]",
        Field field => $"{Key(field.Receiver)}.{field.Member.DeclaringType?.Name}.{field.Member.Name}",
        Assignment assignment => $"{Key(assignment.Target)} := {Key(assignment.Assigned)}",
        Call call => $"{Key(call.Receiver)}.{call.Method.DeclaringType?.FullName}.{call.Method}({string.Join(",", call.Arguments.Select(Key))})",
        Operation operation => $"({Key(operation.Left)} {operation.Operator} {Key(operation.Right)})",
        _ => "",
    };
}
