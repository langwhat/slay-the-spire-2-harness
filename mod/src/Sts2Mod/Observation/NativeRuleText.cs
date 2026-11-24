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
internal sealed partial class NativeRuleText(MonsterModel? monster = null)
{
    private readonly Dictionary<MethodInfo, NativeRuleBody> _bodies = [];
    private readonly HashSet<Type> _powers = [];
    private readonly HashSet<Type> _readPowers = [];
    private readonly HashSet<Type> _emittedPowers = [];
    private readonly HashSet<string> _forcedMoves = [];
    private readonly HashSet<Type> _summons = [];
    private readonly HashSet<string> _branchFlags = [];
    private readonly Dictionary<FieldInfo, string?> _flagNames = [];
    private readonly HashSet<string> _gaps = [];
    private readonly HashSet<string> _initializedMonsterInts = [];

    private static readonly (string Method, string Timing)[] Hooks =
    [
        ("BeforeApplied", "Before this power is applied"),
        ("AfterAddedToRoom", "On entering combat"),
        ("AfterRoomEntered", "On entering a room"),
        ("BeforeCombatStart", "Before combat starts"),
        ("BeforeSideTurnStart", "Before side turn starts"),
        ("AfterSideTurnStart", "After side turn starts"),
        ("AfterPlayerTurnStart", "After player turn starts"),
        ("AfterSideTurnStartLate", "At side turn start, late"),
        ("AfterBlockCleared", "After Block expires at turn start"),
        ("AfterEnergyReset", "After turn-start Energy reset"),
        ("AfterEnergyResetLate", "After turn-start Energy reset (late)"),
        ("BeforeHandDraw", "Before turn-start hand draw"),
        ("AfterAutoPrePlayPhaseEntered", "At start of turn before card play"),
        ("AfterCardEnteredCombat", "After a card enters combat"),
        ("AfterCardDrawn", "After drawing a card"),
        ("BeforeCardPlayed", "Before card play"),
        ("AfterCardPlayed", "After each card play resolves"),
        ("AfterCardExhausted", "After a card is exhausted"),
        ("AfterCardDiscarded", "After a card is discarded"),
        ("AfterShuffle", "After shuffling draw pile"),
        ("AfterCardChangedPilesLate", "After a card changes piles, late"),
        ("AfterOrbChanneled", "After channeling an orb"),
        ("AfterOrbEvoked", "After an orb is evoked"),
        ("AfterStarsGained", "After gaining Stars"),
        ("AfterStarsSpent", "After spending Stars"),
        ("AfterForge", "After forging"),
        ("AfterEnergySpent", "After spending Energy on a card"),
        ("AfterBlockGained", "After gaining block"),
        ("AfterPreventingBlockClear", "After preventing block clear"),
        ("AfterDamageReceived", "After damage resolves"),
        ("AfterDeath", "On death event"),
        ("BeforeSideTurnEndVeryEarly", "At side turn end, very early"),
        ("BeforeSideTurnEndEarly", "At side turn end, early"),
        ("BeforeSideTurnEnd", "Before side turn ends"),
        ("AfterAutoPostPlayPhaseEntered", "At end of turn before hand discard"),
        ("BeforeFlush", "Before end-of-turn hand discard"),
        ("AfterFlush", "After end-of-turn hand discard"),
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
        ("AfterModifyingBlockAmount", "After modifying Block"),
        ("AfterModifyingPowerAmountGiven", "After modifying power stacks given"),
        ("AfterModifyingHpLostBeforeOsty", "After modifying HP lost"),
        ("AfterModifyingHpLostAfterOsty", "After modifying HP lost"),
        ("AfterCombatVictory", "After combat victory"),
        ("AfterCombatEnd", "After combat ends"),
    ];

    internal IReadOnlySet<string> ForcedMoves => _forcedMoves;
    internal IReadOnlySet<Type> Summons => _summons;
    internal IReadOnlySet<Type> ReadPowers => _readPowers;
    internal IReadOnlySet<Type> EmittedPowers => _emittedPowers;
    internal string Label(Type model) => ModelLabel(model);
    internal IReadOnlySet<string> Gaps => _gaps;
    internal void Gap(string location) => _gaps.Add(location);

    internal string MoveLabel(string id)
    {
        if (monster is null)
            return id;
        var key = id.Split("_MOVE", StringSplitOptions.None)[0];
        foreach (var entry in MonsterLocEntries(monster.GetType(), monster.Id.Entry))
        {
            var title = MonsterModel.L10NMonsterLookup($"{entry}.moves.{key}.title");
            if (title.Exists())
                return $"{title.GetFormattedText()} ({id})";
        }
        return id;
    }

    private static IEnumerable<string> MonsterLocEntries(Type monsterType, string primaryEntry)
    {
        yield return primaryEntry;
        for (var baseType = monsterType.BaseType; baseType is not null && baseType != typeof(MonsterModel); baseType = baseType.BaseType)
        {
            var snake = System.Text.RegularExpressions.Regex.Replace(baseType.Name, "(?<=[a-z0-9])(?=[A-Z])", "_").ToUpperInvariant();
            if (snake != primaryEntry)
                yield return snake;
        }
    }

    internal string? ReadCondition(MethodInfo method)
    {
        var body = Body(method);
        foreach (var result in body.Returns)
        {
            if (BooleanGetter(result.Value) is { } getter)
                _branchFlags.Add(getter);
            if (result.Value is Operation { Operator: "ceq", Left: var left }
                && BooleanGetter(left) is { } inner)
                _branchFlags.Add(inner);
            foreach (var cond in result.Conditions)
            {
                if (BooleanGetter(cond.Value) is { } condGetter)
                    _branchFlags.Add(condGetter);
            }
        }
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        if (body.Returns.Count == 1 && body.Returns[0].Conditions.Length == 0)
            return Condition(new Test(body.Returns[0].Value, true));

        var truePaths = new List<Test[]>();
        var falsePaths = new List<Test[]>();
        foreach (var result in body.Returns)
        {
            if (TryTruth(result.Value, out var constantBool))
            {
                (constantBool ? truePaths : falsePaths).Add(result.Conditions);
            }
            else
            {
                truePaths.Add([.. result.Conditions, new Test(result.Value, true)]);
                falsePaths.Add([.. result.Conditions, new Test(result.Value, false)]);
            }
        }
        if (truePaths.Count == 0)
            return null;

        var others = falsePaths.Concat(body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        var clauses = new List<string>();
        foreach (var path in NativeRuleGuards.Minimize(truePaths, others, TestKey) ?? MergeTests(truePaths))
        {
            if (Guard(path) is not { } guard)
                return null;
            if (guard == Never)
                continue;
            if (guard.Length == 0)
                return "";
            if (!clauses.Contains(guard, StringComparer.Ordinal))
                clauses.Add(guard);
        }
        return clauses.Count == 0 ? null : string.Join(" or ", clauses);
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

    internal (List<string> Effects, bool HasGap, bool KillsSelf) ReadMove(MethodInfo method, MoveState move)
    {
        var (effects, incomplete) = ReadEffects(method);
        var intentMismatch = (move.Intents.Any(intent => intent is AttackIntent) && !effects.Any(effect => effect.Attack))
            || (move.Intents.Any(intent => intent is BuffIntent or DebuffIntent or DefendIntent) && effects.Count == 0);
        if (incomplete || intentMismatch)
        {
            Gap(move.Id);
            return ([], true, false);
        }
        return (effects.Select(effect => effect.Text).ToList(), false, effects.Any(effect => effect.KillsSelf));
    }

    internal void ReadHooks(Type type, List<string> lines)
    {
        var power = typeof(PowerModel).IsAssignableFrom(type);
        var modelWithHooks = power || typeof(RelicModel).IsAssignableFrom(type) || typeof(AfflictionModel).IsAssignableFrom(type);
        var previousOwner = _hookOwner;
        _hookOwner = modelWithHooks ? type : null;
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
        var isRelic = typeof(RelicModel).IsAssignableFrom(type);
        var gapsBefore = _gaps.Count;
        var targetLines = (power || isRelic) ? new List<string>() : lines;
        var powerPrefix = power ? PowerLabel(type) + " · " : "";
        var handled = new HashSet<string>(Hooks.Select(hook => hook.Method), StringComparer.Ordinal);
        var preHandled = new HashSet<string>(StringComparer.Ordinal);
        if (power || isRelic)
        {
            if (power)
            {
                ReadSpecialPowerHooks(type, powerPrefix, preHandled, targetLines);
                ReadDamageCapHooks(type, powerPrefix, preHandled, targetLines);
            }
            ReadPlayerResourceHooks(type, powerPrefix, preHandled, targetLines);
            ReadPowerReceptionHooks(type, powerPrefix, preHandled, targetLines);
            handled.UnionWith(preHandled);
        }

        if (power && MutableDynamicKeys(type) is { Count: > 0 } mutableKeys
            && TryGetCanonicalPower(type) is { } canonicalPower)
        {
            var nonZeroDefaults = mutableKeys.Order(StringComparer.Ordinal)
                .Where(k => k != "DisplayAmount" && canonicalPower.DynamicVars.TryGetValue(k, out var v) && v.BaseValue != 0m)
                .Select(k => $"counter {HumanizeLower(k)} starts at {Number(canonicalPower.DynamicVars[k].BaseValue)}")
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
            var (effects, hookIncomplete) = ReadEffects(method, power ? type : null);
            if (!hookIncomplete && effects.Count != 0)
            {
                var trigger = effects.Select(effect => effect.Trigger).FirstOrDefault(trigger => trigger is not null) ?? timing;
                var bodyText = string.Join("; ", effects.Select(effect => effect.Text));
                (trigger, bodyText) = SimplifyTurnSideTrigger(trigger, bodyText, effects.Count);
                targetLines.Add($"{powerPrefix}{trigger}: {bodyText}");
            }
        }

        var includeMultiplayer = IsMultiplayer();
        foreach (var (name, neutral, timing, format) in ValueHooks)
        {
            if (preHandled.Contains(name) || FindOverride(type, name) is not { } hook)
                continue;
            handled.Add(name);
            if (name == "GetScaledAmountForMultiplayer" && !includeMultiplayer)
                continue;
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
                targetLines.Add($"{powerPrefix}{QueryLabel(query.Name)}: {string.Join("; ", rendered)}");
        }

        foreach (var unhandled in NativeHookDispatch.Overrides(type))
            if (!handled.Contains(unhandled.Name))
                Gap($"{unhandled.DeclaringType?.Name}.{unhandled.Name}");
        foreach (var unread in NativeHookDispatch.Unread)
            Gap(unread);

        if ((power || isRelic) && _gaps.Count == gapsBefore)
        {
            lines.AddRange(targetLines);
            if (power && targetLines.Count > 0)
                _emittedPowers.Add(type);
        }
    }

    private const BindingFlags Declared = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    private static readonly Type[] BaseModels = [typeof(AbstractModel), typeof(MonsterModel), typeof(PowerModel), typeof(RelicModel), typeof(AfflictionModel), typeof(EnchantmentModel)];

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
            var minimized = (NativeRuleGuards.Minimize(reaching, others, TestKey) ?? MergeTests(reaching)).ToArray();
            var pathGuards = new List<(Test[] Path, string Guard)>();
            foreach (var path in minimized)
            {
                if (Guard(path) is not { } guard)
                    return null;
                if (guard == Never)
                    continue;
                if (!pathGuards.Any(prev => prev.Guard == guard))
                    pathGuards.Add((path, guard));
            }
            if (pathGuards.Count == 0)
                continue;
            if (pathGuards.Any(pg => pg.Guard.Length == 0))
            {
                rendered.Add(new([], "", [new Effect(group.Key)]));
            }
            else if (pathGuards.Count == 1)
            {
                rendered.Add(new(pathGuards[0].Path, pathGuards[0].Guard, [new Effect(group.Key)]));
            }
            else
            {
                var combinedGuard = CombineDisjunctivePaths(pathGuards);
                if (combinedGuard is null)
                    return null;
                rendered.Add(new([], combinedGuard, [new Effect(group.Key)]));
            }
        }
        return RenderBranches(rendered).Select(effect => effect.Text).Distinct().ToList();
    }

    // Factor disjunctions at the Test[] level (never by splitting rendered strings, which can contain " and ").
    private string? CombineDisjunctivePaths(List<(Test[] Path, string Guard)> pathGuards)
    {
        var first = pathGuards[0].Path;
        var commonKeys = new HashSet<(string Key, bool Expected)>();
        var commonTests = new List<Test>();
        foreach (var test in first)
        {
            var sig = (TestKey(test), test.Expected);
            if (commonKeys.Contains(sig))
                continue;
            if (pathGuards.Skip(1).All(pg => pg.Path.Any(other => TestKey(other) == sig.Item1 && other.Expected == sig.Expected)))
            {
                commonKeys.Add(sig);
                commonTests.Add(test);
            }
        }

        var commonGuard = commonTests.Count > 0 ? Guard(commonTests.ToArray()) : "";
        if (commonGuard is null || commonGuard == Never)
            return null;

        var remainders = new List<string>();
        foreach (var (path, _) in pathGuards)
        {
            var remTests = path.Where(t => !commonKeys.Contains((TestKey(t), t.Expected))).ToArray();
            var remGuard = Guard(remTests);
            if (remGuard is null || remGuard == Never)
                return null;
            if (remGuard.Length == 0)
                return commonGuard;
            var wrapped = remTests.Length > 1 && remGuard.Contains(" and ", StringComparison.Ordinal)
                ? $"({remGuard})"
                : remGuard;
            if (!remainders.Contains(wrapped, StringComparer.Ordinal))
                remainders.Add(wrapped);
        }

        if (remainders.Count == 0)
            return commonGuard;
        var disjunction = remainders.Count == 1 ? remainders[0] : $"({string.Join(" or ", remainders)})";
        return commonGuard.Length == 0
            ? string.Join(" or ", remainders)
            : $"{commonGuard} and {disjunction}";
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

    private static bool IsMultiplayer()
        => (MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState()?.Players?.Count ?? 1) > 1;

    private static string PlayerTargetsLabel()
        => IsMultiplayer() ? "player targets" : "the player";

    private static string Humanize(string name)
    {
        var words = System.Text.RegularExpressions.Regex.Replace(name, "(?<=[a-z])(?=[A-Z])", " ").ToLowerInvariant();
        return char.ToUpperInvariant(words[0]) + words[1..];
    }

    private static string HumanizeLower(string name)
        => System.Text.RegularExpressions.Regex.Replace(name, "(?<=[a-z0-9])(?=[A-Z])", " ").ToLowerInvariant();

    private static string QueryLabel(string methodName) => methodName switch
    {
        "ShouldClearBlock" => "Block expires at turn start",
        "ShouldStopCombatFromEnding" => "Prevents combat from ending",
        "ShouldOwnerDeathTriggerFatal" => "Owner death triggers Fatal",
        "ShouldPowerBeRemovedAfterOwnerDeath" => "Removed on owner death",
        "ShouldCreatureBeRemovedFromCombatAfterDeath" => "Removed from combat on death",
        "ShouldAllowHitting" => "Targetable by hits",
        "ShouldPlay" => "Can play card",
        "ShouldDraw" => "Can draw cards",
        "ShouldFlush" => "End-of-turn hand discard",
        "ShouldProcurePotion" => "Can obtain potions",
        "ShouldDisableRemainingRestSiteOptions" => "Disable remaining rest site options after choosing one",
        "ShouldGainGold" => "Can gain Gold",
        "ShouldTakeExtraTurn" => "Takes an extra turn",
        "ShouldGenerateTreasure" => "Generates treasure",
        _ => Humanize(methodName),
    };

    private static (string Trigger, string Body) SimplifyTurnSideTrigger(string trigger, string body, int effectCount)
    {
        if (effectCount != 1)
            return (trigger, body);
        foreach (var (ownerGuard, rewrittenTrigger) in new[]
        {
            ("if player=owner: ", trigger switch
            {
                "After player turn starts" => "After owner's turn starts",
                "Before turn-start hand draw" => "Before owner's turn-start hand draw",
                "After turn-start Energy reset" => "After owner's turn-start Energy reset",
                "At end of turn before hand discard" => "At end of owner's turn before hand discard",
                _ => null,
            }),
            ("if event target=owner: ", trigger == "After Block expires at turn start"
                ? "After owner's Block expires at turn start"
                : null),
            ("if room is a combat room: ", trigger == "On entering a room"
                ? "On entering combat"
                : null),
        })
        {
            if (rewrittenTrigger is not null && body.StartsWith(ownerGuard, StringComparison.Ordinal))
                return (rewrittenTrigger, body[ownerGuard.Length..]);
        }
        foreach (var (sideGuard, sideWord) in new[] { ("if turn side=enemy: ", "enemy"), ("if turn side=player: ", "player") })
        {
            if (!body.StartsWith(sideGuard, StringComparison.Ordinal))
                continue;
            var rewritten = trigger switch
            {
                "Before side turn starts" => $"Before {sideWord} turn starts",
                "After side turn starts" => $"After {sideWord} turn starts",
                "At side turn start, late" => $"At {sideWord} turn start, late",
                "At side turn end, very early" => $"At {sideWord} turn end, very early",
                "At side turn end, early" => $"At {sideWord} turn end, early",
                "Before side turn ends" => $"Before {sideWord} turn ends",
                "After side turn ends" => $"After {sideWord} turn ends",
                "At side turn end, late" => $"At {sideWord} turn end, late",
                _ => null,
            };
            if (rewritten is not null)
                return (rewritten, body[sideGuard.Length..]);
        }
        return (trigger, body);
    }

    internal void ReadPowerHooks(List<string> lines, ISet<Type>? hoistedPowers = null)
    {
        var includeMultiplayer = IsMultiplayer();
        while (_powers.Except(_readPowers).OrderBy(type => type.FullName, StringComparer.Ordinal).FirstOrDefault() is { } power)
        {
            _readPowers.Add(power);
            var perPowerLines = new List<string>();
            var gapsBefore = _gaps.Count;
            var cardsBefore = new HashSet<Type>(_generatedCards);
            ReadHooks(power, perPowerLines);
            if (hoistedPowers is not null
                && _gaps.Count == gapsBefore
                && PowerMechanismReader.IsStandaloneReadable(power, includeMultiplayer))
            {
                hoistedPowers.Add(power);
                _emittedPowers.Remove(power);
                _generatedCards.IntersectWith(cardsBefore);
            }
            else
            {
                lines.AddRange(perPowerLines);
            }
        }
        // Emit initializers for monster int counters read in moves/branches that were not set in AfterAddedToRoom.
        var canonicalMonster = monster is not null ? ModelDb.GetByIdOrNull<MonsterModel>(monster.Id) : null;
        foreach (var (name, defaultVal) in _trackedMonsterNullableDefaults.OrderBy(entry => entry.Name, StringComparer.Ordinal))
        {
            if (_initializedMonsterInts.Add(name))
                lines.Insert(0, $"Initial {HumanizeLower(name)}: {defaultVal}");
        }
        foreach (var (name, field) in _trackedMonsterInts.OrderBy(entry => entry.Name, StringComparer.Ordinal))
        {
            if (_choiceCursors.Contains(field))
                continue;
            if (_initializedMonsterInts.Add(name))
            {
                var initialVal = canonicalMonster is not null && field.DeclaringType?.IsInstanceOfType(canonicalMonster) == true
                    && field.GetValue(canonicalMonster) is int rawInit ? rawInit : 0;
                lines.Insert(0, $"Initial {HumanizeLower(name)}: {initialVal}");
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
        Type? AddedPower = null, Type? AddedCard = null, Type? AddedSummon = null, string? ForcedMove = null, bool KillsSelf = false);

    private sealed record GuardedEffects(Test[] Path, string Guard, Effect[] Effects, string? Repetitions = null, Loop? Loop = null, ForeachLoop? ForeachLoop = null);

    private IEnumerable<Effect> RenderBranches(List<GuardedEffects> effects)
    {
        for (var start = 0; start < effects.Count;)
        {
            var first = effects[start];
            if (first.Loop is { } activeLoop && first.Repetitions is { } loopCount)
            {
                var span = 1;
                while (start + span < effects.Count && effects[start + span].Loop == activeLoop)
                    span++;
                var innerItems = effects.Skip(start).Take(span)
                    .Select(item => item with { Repetitions = null, Loop = null })
                    .ToList();
                var innerRendered = RenderBranches(GroupGuardedChanges(CollapseRepeatedUnrolled(innerItems))).ToList();
                if (innerRendered.Count == 1)
                    yield return Combine(innerRendered, $"{loopCount} times: {innerRendered[0].Text}");
                else if (innerRendered.Count > 1)
                    yield return Combine(innerRendered, $"{loopCount} times: ({string.Join("; ", innerRendered.Select(e => e.Text))})");
                start += span;
                continue;
            }
            if (FindExclusiveBranch(effects, start) is { } branch)
            {
                yield return branch.Effect;
                start = branch.End;
                continue;
            }
            if (first.Repetitions is null && first.Guard.Length != 0)
            {
                var span = 1;
                while (start + span < effects.Count
                    && effects[start + span].Repetitions is null
                    && effects[start + span].Loop is null
                    && effects[start + span].Guard == first.Guard)
                {
                    span++;
                }
                if (span > 1 || (first.Effects.Length > 1 && first.Effects.Any(effect => effect.ChangesGuard)))
                {
                    var groupedEffects = effects.Skip(start).Take(span).SelectMany(item => item.Effects).ToArray();
                    yield return Combine(groupedEffects, When(first.Guard, $"({string.Join("; ", groupedEffects.Select(effect => effect.Text))})"));
                    start += span;
                    continue;
                }
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
    {
        var list = effects.ToArray();
        return new(text,
            list.Any(effect => effect.Attack),
            list.Select(effect => effect.Trigger).FirstOrDefault(trigger => trigger is not null),
            list.Any(effect => effect.ChangesGuard),
            KillsSelf: list.Any(effect => effect.KillsSelf));
    }

    private sealed record EffectStep(int Index, Value Action, Test[] Conditions, int Epoch = 0, int Order = 0);
    private sealed record MutablePowerTarget(string InstanceKey, int Epoch, int Order, Test[] Conditions, string? TargetText);

    private (List<Effect> Effects, bool Incomplete) ReadEffects(MethodInfo method, Type? power = null,
        int depth = 0, bool onOwnedMonster = false, IReadOnlyDictionary<string, Value>? bindings = null)
    {
        var body = Body(method);
        var incomplete = false;
        if (body.Incomplete)
        {
            MarkIncomplete(method.Name);
            return ([], true);
        }
        if (power is null && depth == 0 && bindings is null && TryReadSpecialMove(method, body) is { } specialEffects)
            return (specialEffects, false);

        foreach (var field in body.Writes)
            if (IsGameplayField(field))
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
            var enclosingLoopGroups = body.Loops.Where(l => group.Key.Index >= l.Start && group.Key.Index < l.End)
                .GroupBy(l => (l.Start, l.End))
                .OrderBy(g => g.Key.Start)
                .ToArray();
            var enclosingForeachGroups = body.ForeachRanges
                .Where(r => group.Key.Index >= r.Start && group.Key.Index < r.End)
                .GroupBy(r => (r.Start, r.End))
                .OrderByDescending(g => g.Key.Start)
                .ToArray();
            var foreachLoop = enclosingForeachGroups.Length > 0 ? enclosingForeachGroups[0].First() : null;
            var loop = enclosingLoopGroups.Length > 0 ? enclosingLoopGroups[^1].First() : null;
            string? repetitions = null;
            if (enclosingLoopGroups.Length > 0)
            {
                var counts = new List<string>(enclosingLoopGroups.Length);
                var unreadableLoop = false;
                foreach (var loopGroup in enclosingLoopGroups)
                {
                    var distinctTexts = loopGroup.Select(l => ValueText(l.Count)).Distinct(StringComparer.Ordinal).ToArray();
                    if (distinctTexts is [{ } singleCount])
                        counts.Add(singleCount);
                    else
                    {
                        unreadableLoop = true;
                        break;
                    }
                }
                if (unreadableLoop)
                {
                    MarkIncomplete($"{method.Name}/loop-count");
                    continue;
                }
                repetitions = counts.Count == 1 ? counts[0] : $"({string.Join(" × ", counts)})";
            }
            var rendered = new List<GuardedEffects>();
            var alternatives = group.GroupBy(step => Key(step.Action));
            foreach (var alternative in alternatives)
            {
                var matching = alternative.Where(step => step.Conditions.All(IsCurrentRun) && !IsUnchangedAssignment(step)).ToArray();
                if (matching.Length == 0)
                    continue;
                var action = matching[0].Action;
                var call = action as Call;
                if (call is not null && call.Method.Name == "set_Target" && call.Method.DeclaringType == typeof(PowerModel))
                {
                    continue;
                }
                if (call is not null && call.Method.Name.StartsWith("set_", StringComparison.Ordinal)
                    && SummonedMonsterType(call.Receiver) is not null)
                {
                    continue;
                }
                if (call is not null && call.Method.IsSpecialName && call.Method.DeclaringType == typeof(Creature)
                    && call.Method.Name.StartsWith("add_", StringComparison.Ordinal))
                {
                    if (!IsPresentationEventSubscription(call, method.DeclaringType))
                        MarkIncomplete($"{method.Name}/{call.Method.Name}");
                    continue;
                }

                var recognized = true;
                var effect = call is not null ? DescribeEffect(call, method, power, flags, powerTargets, powerHolders, matching, out recognized)
                    : action is Assignment assignment ? DescribeSlotAssignment(assignment, method) : null;

                List<Effect> extracted;
                MethodInfo? inlinedHelper = null;
                if (effect is not null)
                    extracted = [effect];
                else if (call is not null && !recognized && depth < 2 && ReadableHelper(call, method, power, matching.Select(step => step.Conditions)) is { } helper)
                {
                    inlinedHelper = helper.Method;
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
                // When enclosing foreach loops execute an effect that does not reference their enumerated item,
                // preserve every such loop's per-element repetition explicitly (innermost to outermost).
                if (enclosingForeachGroups.Length > 0)
                {
                    var originalTexts = extracted.Select(item => item.Text).ToArray();
                    var foreachFailed = false;
                    foreach (var feGroup in enclosingForeachGroups)
                    {
                        var sampleFe = feGroup.First();
                        var boundCollection = bindings is not null && sampleFe.Collection is not null
                            ? Bind(sampleFe.Collection, bindings)
                            : sampleFe.Collection;
                        var boundColKey = boundCollection is not null ? Key(boundCollection) : null;
                        var referencesThis = bindings is null
                            ? feGroup.Any(fe => (inlinedHelper is null
                                    ? ReferencesEnumerator(action, fe.Enumerator)
                                    : HelperUsesEnumerator(inlinedHelper, call!, fe.Enumerator))
                                || matching.Any(s => s.Conditions.Any(c => ReferencesEnumerator(c.Value, fe.Enumerator))))
                            : boundColKey is not null && ((inlinedHelper is null
                                    ? ReferencesCollectionEnumerator(action, boundColKey)
                                    : HelperUsesCollectionEnumerator(inlinedHelper, call!, boundColKey))
                                || matching.Any(s => s.Conditions.Any(c => ReferencesCollectionEnumerator(c.Value, boundColKey))));
                        if (referencesThis)
                            continue;
                        if (boundCollection is null || ValueText(boundCollection) is not { } colText)
                        {
                            MarkIncomplete($"{method.Name}/foreach-count");
                            foreachFailed = true;
                            break;
                        }
                        if ((boundCollection is Input { Name: "targets" } || boundCollection is Call { Method.Name: "get_Players" or "GetOpponentsOf" })
                            && originalTexts.Any(t => t.Contains(colText, StringComparison.Ordinal)))
                            continue;
                        extracted = extracted.Select(item => item with { Text = $"for each item in {colText}: {item.Text}" }).ToList();
                    }
                    if (foreachFailed)
                        continue;
                }
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
                var pathGuards = new List<(Test[] Path, string Guard)>();
                var guardFailed = false;
                foreach (var path in minimized ?? MergeTests(reaching))
                {
                    if (Guard(path) is not { } guard)
                    {
                        MarkIncomplete($"{method.Name}/condition");
                        guardFailed = true;
                        continue;
                    }
                    if (guard == Never)
                        continue;
                    if (!pathGuards.Any(prev => prev.Guard == guard))
                        pathGuards.Add((path, guard));
                }
                if (guardFailed || pathGuards.Count == 0)
                    continue;

                GuardedEffects combinedGuardEffect;
                if (pathGuards.Any(pg => pg.Guard.Length == 0))
                {
                    combinedGuardEffect = new([], "", extracted.ToArray(), repetitions, loop, foreachLoop);
                }
                else if (pathGuards.Count == 1)
                {
                    combinedGuardEffect = new(pathGuards[0].Path, pathGuards[0].Guard, extracted.ToArray(), repetitions, loop, foreachLoop);
                }
                else
                {
                    var joinedGuard = string.Join(" or ", pathGuards.Select(pg =>
                        pg.Guard.Contains(" and ", StringComparison.Ordinal) ? $"({pg.Guard})" : pg.Guard));
                    combinedGuardEffect = new([], joinedGuard, extracted.ToArray(), repetitions, loop, foreachLoop);
                }

                if (!rendered.Any(previous => previous.Guard == combinedGuardEffect.Guard && previous.Effects.SequenceEqual(extracted)))
                {
                    rendered.Add(combinedGuardEffect);
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
            // Equivalent paths can reach one instruction. Separate instructions still run separately.
            effects.AddRange(rendered);
        }
        return (RenderBranches(GroupGuardedChanges(CollapseSameEnumeratorTakenCards(CollapseRepeatedUnrolled(effects)), method)).ToList(), incomplete);

        void MarkIncomplete(string location)
        {
            incomplete = true;
            Gap(location);
        }
    }

    private bool HelperUsesEnumerator(MethodInfo helper, Call call, Call enumerator)
    {
        var helperBody = Body(helper);
        if (!helperBody.ReadsParameters)
            return false;
        var parameters = helper.GetParameters();
        var enumParamNames = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i < parameters.Length && i < call.Arguments.Length; i++)
        {
            if (parameters[i].Name is { } pName && ReferencesEnumerator(call.Arguments[i], enumerator))
                enumParamNames.Add(pName);
        }
        return enumParamNames.Count > 0 && BodyReferencesAnyInput(helperBody, enumParamNames);
    }

    private bool HelperUsesCollectionEnumerator(MethodInfo helper, Call call, string collectionKey)
    {
        var helperBody = Body(helper);
        if (!helperBody.ReadsParameters)
            return false;
        var parameters = helper.GetParameters();
        var enumParamNames = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i < parameters.Length && i < call.Arguments.Length; i++)
        {
            if (parameters[i].Name is { } pName && ReferencesCollectionEnumerator(call.Arguments[i], collectionKey))
                enumParamNames.Add(pName);
        }
        return enumParamNames.Count > 0 && BodyReferencesAnyInput(helperBody, enumParamNames);
    }

    private bool BodyReferencesAnyInput(NativeRuleBody body, HashSet<string> paramNames)
        => body.Calls.Any(s => !IsBenignReadOrPlumbingCall(s.Call.Method)
                && (ValueReferencesAnyInput(s.Call, paramNames) || s.Conditions.Any(c => ValueReferencesAnyInput(c.Value, paramNames))))
            || body.FieldSteps.Any(f => ValueReferencesAnyInput(f.Assignment, paramNames) || f.Conditions.Any(c => ValueReferencesAnyInput(c.Value, paramNames)));

    private static bool ValueReferencesAnyInput(Value? value, HashSet<string> paramNames, int depth = 0)
    {
        if (value is null || depth > 8)
            return false;
        return value switch
        {
            Input { Name: var name } => paramNames.Contains(name),
            Call c => ValueReferencesAnyInput(c.Receiver, paramNames, depth + 1) || c.Arguments.Any(a => ValueReferencesAnyInput(a, paramNames, depth + 1)),
            Operation op => ValueReferencesAnyInput(op.Left, paramNames, depth + 1) || ValueReferencesAnyInput(op.Right, paramNames, depth + 1),
            Field f => ValueReferencesAnyInput(f.Receiver, paramNames, depth + 1),
            Assignment a => ValueReferencesAnyInput(a.Target, paramNames, depth + 1) || ValueReferencesAnyInput(a.Assigned, paramNames, depth + 1),
            _ => false,
        };
    }

    private static bool ReferencesEnumerator(Value? value, Call enumerator, int depth = 0)
    {
        if (value is null || depth > 8)
            return false;
        return value switch
        {
            Call { Method.Name: "get_Current", Receiver: Call en } when ReferenceEquals(en, enumerator) => true,
            Call c => ReferencesEnumerator(c.Receiver, enumerator, depth + 1) || c.Arguments.Any(a => ReferencesEnumerator(a, enumerator, depth + 1)),
            Operation op => ReferencesEnumerator(op.Left, enumerator, depth + 1) || ReferencesEnumerator(op.Right, enumerator, depth + 1),
            Field f => ReferencesEnumerator(f.Receiver, enumerator, depth + 1),
            Assignment a => ReferencesEnumerator(a.Target, enumerator, depth + 1) || ReferencesEnumerator(a.Assigned, enumerator, depth + 1),
            _ => false,
        };
    }

    private static bool ReferencesCollectionEnumerator(Value? value, string collectionKey, int depth = 0)
    {
        if (value is null || depth > 8)
            return false;
        return value switch
        {
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: { } col } }
                when Key(col) == collectionKey => true,
            Call c => ReferencesCollectionEnumerator(c.Receiver, collectionKey, depth + 1)
                || c.Arguments.Any(a => ReferencesCollectionEnumerator(a, collectionKey, depth + 1)),
            Operation op => ReferencesCollectionEnumerator(op.Left, collectionKey, depth + 1)
                || ReferencesCollectionEnumerator(op.Right, collectionKey, depth + 1),
            Field f => ReferencesCollectionEnumerator(f.Receiver, collectionKey, depth + 1),
            Assignment a => ReferencesCollectionEnumerator(a.Target, collectionKey, depth + 1)
                || ReferencesCollectionEnumerator(a.Assigned, collectionKey, depth + 1),
            _ => false,
        };
    }

    // When a foreach iterates once over a sampled slice (`each of up to ...`) and performs multiple actions
    // on each element (e.g. AggressionPower: move into hand, then if upgradable upgrade it), bind the sample once.
    private static List<GuardedEffects> CollapseSameEnumeratorTakenCards(List<GuardedEffects> input)
    {
        if (input.Count < 2)
            return input;
        const string marker = "each of up to ";
        var result = new List<GuardedEffects>(input.Count);
        for (var i = 0; i < input.Count;)
        {
            var first = input[i];
            if (first.Repetitions is null && first.ForeachLoop is not null && first.Effects.Length == 1
                && first.Effects[0].Text.IndexOf(marker, StringComparison.Ordinal) is >= 0 and var startIdx)
            {
                var subject = ExtractEachOfSubject(first.Effects[0].Text, startIdx);
                if (subject is not null && i + 1 < input.Count)
                {
                    var next = input[i + 1];
                    if (next.Repetitions is null && next.ForeachLoop is not null
                        && next.ForeachLoop.Start == first.ForeachLoop.Start && next.ForeachLoop.End == first.ForeachLoop.End
                        && next.Effects.Length == 1
                        && next.Effects[0].Text.Contains(subject, StringComparison.Ordinal)
                        && (next.Guard == first.Guard
                            || (first.Guard.Length > 0 && next.Guard.StartsWith(first.Guard + " and ", StringComparison.Ordinal)
                                && next.Guard.Contains(subject, StringComparison.Ordinal))))
                    {
                        var firstRewritten = first.Effects[0].Text.Replace(subject, "it", StringComparison.Ordinal);
                        var secondRewritten = next.Effects[0].Text.Replace(subject, "it", StringComparison.Ordinal);
                        var extraGuard = next.Guard.Length > first.Guard.Length
                            ? next.Guard[(first.Guard.Length + " and ".Length)..].Replace(subject, "it", StringComparison.Ordinal)
                            : "";
                        var secondClause = extraGuard.Length > 0 ? $"if {extraGuard}: {secondRewritten}" : secondRewritten;
                        var samplePhrase = subject["each of ".Length..];
                        var combinedText = $"for each of {samplePhrase}: ({firstRewritten}; {secondClause})";
                        result.Add(first with { Effects = [Combine([first.Effects[0], next.Effects[0]], combinedText)] });
                        i += 2;
                        continue;
                    }
                }
            }
            result.Add(first);
            i++;
        }
        return result;
    }

    private static string? ExtractEachOfSubject(string text, int startIdx)
    {
        foreach (var stop in new[] { " into ", " to ", " is " })
        {
            var endIdx = text.IndexOf(stop, startIdx, StringComparison.Ordinal);
            if (endIdx > startIdx)
                return text[startIdx..endIdx];
        }
        return null;
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
        var addSteps = body.Calls
            .Where(step => step.Call.Method.DeclaringType == typeof(CreatureCmd)
                && step.Call.Method.Name == nameof(CreatureCmd.Add)
                && !step.Call.Method.IsGenericMethod
                && step.Call.Arguments.FirstOrDefault() is { } spawned
                && SummonedMonsterType(spawned) is not null)
            .ToArray();
        if (addSteps.Length == 0)
            return;

        foreach (var monsterGroup in addSteps.GroupBy(step => SummonedMonsterType(step.Call.Arguments[0])!))
        {
            var monsterType = monsterGroup.Key;
            var perAddFields = new List<HashSet<FieldInfo>>();
            foreach (var addStep in monsterGroup)
            {
                var spawnedKey = Key(addStep.Call.Arguments[0]);
                var setFields = new HashSet<FieldInfo>();
                foreach (var step in body.Calls)
                {
                    if (step.Epoch > addStep.Epoch || (step.Epoch == addStep.Epoch && step.Order >= addStep.Order))
                        continue;
                    if (step.Call.Receiver is null || Key(step.Call.Receiver) != spawnedKey)
                        continue;
                    if (step.Call.Method is not MethodInfo { Name: var name } setter || !name.StartsWith("set_", StringComparison.Ordinal))
                        continue;
                    if (!ConditionsImply(addStep.Conditions, step.Conditions))
                        continue;
                    if (step.Call.Arguments is [Constant { Data: 1 or true }]
                        && BackingField(monsterType, setter) is { } backing && backing.FieldType == typeof(bool)
                        && IsPlainBackingFieldSetter(setter, backing))
                    {
                        setFields.Add(backing);
                    }
                    else if (BackingField(monsterType, setter) is { FieldType: var ft } bf
                        && ft == typeof(bool) && IsGameplayField(bf))
                    {
                        Gap($"summon-config:{monsterType.Name}.{name}");
                    }
                }
                perAddFields.Add(setFields);
            }

            if (perAddFields.Count == 0)
                continue;

            var commonFields = new HashSet<FieldInfo>(perAddFields[0]);
            var allFields = new HashSet<FieldInfo>(perAddFields[0]);
            foreach (var set in perAddFields.Skip(1))
            {
                commonFields.IntersectWith(set);
                allFields.UnionWith(set);
            }
            if (commonFields.Count != allFields.Count)
                Gap($"summon-config:{monsterType.Name}");

            foreach (var backing in commonFields)
            {
                if (!_summonTrueFlags.TryGetValue(monsterType, out var list))
                {
                    list = [];
                    _summonTrueFlags[monsterType] = list;
                }
                if (!list.Contains(backing))
                    list.Add(backing);
            }
        }
    }

    private bool IsPlainBackingFieldSetter(MethodInfo setter, FieldInfo backing)
    {
        var setterBody = Body(setter);
        if (setterBody.Incomplete || setterBody.Writes.Count > 0)
            return false;
        if (!setterBody.FieldSteps.All(fs => fs.Assignment.Target.Member == backing))
            return false;
        return setterBody.Calls.All(c => c.Call.Method.Name is "AssertMutable" or "get_IsMutable");
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

    private List<MutablePowerTarget> CollectMutablePowerTargets(NativeRuleBody body)
    {
        var list = new List<MutablePowerTarget>();
        foreach (var step in body.Calls)
        {
            if (step.Call.Method.Name == "set_Target" && step.Call.Method.DeclaringType == typeof(PowerModel)
                && step.Call.Receiver is { } instance && step.Call.Arguments is [var target])
            {
                // Preserve unresolved target assignments (TargetText = null) so Apply fails closed
                // instead of silently treating an unreadable set_Target as absent.
                list.Add(new MutablePowerTarget(Key(instance), step.Epoch, step.Order, step.Conditions, ValueText(target)));
            }
        }
        return list;
    }

    private static bool ResolveMutablePowerTarget(
        IReadOnlyList<MutablePowerTarget> targets,
        string instanceKey,
        EffectStep[] matching,
        out string? resolvedTarget)
    {
        resolvedTarget = null;
        var relevant = targets.Where(t => t.InstanceKey == instanceKey).ToArray();
        if (relevant.Length == 0)
            return true;

        var perStepTargets = new HashSet<string?>(StringComparer.Ordinal);
        foreach (var step in matching)
        {
            var compatible = relevant
                .Where(t => (t.Epoch < step.Epoch || (t.Epoch == step.Epoch && t.Order < step.Order))
                    && ConditionsCompatible(t.Conditions, step.Conditions))
                .OrderByDescending(t => (t.Epoch, t.Order))
                .ToArray();
            if (compatible.Length == 0)
            {
                perStepTargets.Add(null);
                continue;
            }
            // If any reaching assignment had an unresolved target expression, fail closed.
            if (compatible.Any(t => t.TargetText is null))
                return false;
            var distinctOnPath = compatible.Select(t => t.TargetText!).Distinct(StringComparer.Ordinal).ToArray();
            if (distinctOnPath.Length > 1 && compatible.Any(t => ConditionsImply(step.Conditions, t.Conditions) && t.TargetText != compatible[0].TargetText))
                return false;
            var latestImplied = compatible.FirstOrDefault(t => ConditionsImply(step.Conditions, t.Conditions));
            if (latestImplied is not null)
            {
                perStepTargets.Add(latestImplied.TargetText);
            }
            else if (distinctOnPath.Length == 1)
            {
                perStepTargets.Add(distinctOnPath[0]);
            }
            else
            {
                return false;
            }
        }

        if (perStepTargets.Count > 1)
            return false;
        resolvedTarget = perStepTargets.FirstOrDefault();
        return true;
    }

    private static bool ConditionsCompatible(Test[] left, Test[] right)
        => !ConditionsContradict(left, right);

    private static bool ConditionsContradict(Test[] left, Test[] right)
    {
        foreach (var a in left)
        {
            var ka = TestKey(a);
            foreach (var b in right)
            {
                if (TestKey(b) == ka && a.Expected != b.Expected)
                    return true;
            }
        }
        return false;
    }

    private static bool ConditionsImply(Test[] stronger, Test[] weaker)
    {
        foreach (var req in weaker)
        {
            var key = TestKey(req);
            if (!stronger.Any(s => TestKey(s) == key && s.Expected == req.Expected))
                return false;
        }
        return true;
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
        ArrayValue array => array with
        {
            Length = Bind(array.Length, bindings),
            Elements = array.Elements.Select(e => Bind(e, bindings)).ToArray(),
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
        var onOwner = power is not null && !helper.IsVirtual && monster is not null && type.IsInstanceOfType(monster)
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

    private bool IsModelEffect(MethodBase method)
    {
        if (method is not MethodInfo info || info.DeclaringType is not { } type
            || IsPurePresentationMember(info) || IsBenignReadOrPlumbingCall(info))
            return false;
        if (typeof(EncounterModel).IsAssignableFrom(type))
            return false;
        if (info.IsSpecialName)
        {
            if (!info.Name.StartsWith("set_", StringComparison.Ordinal))
                return false;
            if (typeof(AbstractModel).IsAssignableFrom(type))
                return BackingField(type, info) is not { } backing || IsGameplayField(backing);
            return type == typeof(Creature) || type == typeof(MegaCrit.Sts2.Core.Entities.Players.Player)
                || typeof(ICombatState).IsAssignableFrom(type);
        }
        if (info.ReturnType != typeof(Task) && info.ReturnType != typeof(void))
            return false;
        return typeof(AbstractModel).IsAssignableFrom(type)
            || type == typeof(Creature)
            || type == typeof(MegaCrit.Sts2.Core.Entities.Players.Player)
            || typeof(ICombatState).IsAssignableFrom(type)
            || type == typeof(MegaCrit.Sts2.Core.Rooms.CombatRoom)
            || type == typeof(CardEnergyCost);
    }

    private Effect? DescribeEffect(Call call, MethodInfo caller, Type? power, IReadOnlySet<string> flags,
        IReadOnlyList<MutablePowerTarget> powerTargets, IReadOnlyDictionary<string, string> powerHolders,
        EffectStep[] matching, out bool recognized)
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

        if (DescribeCorridorEffect(call, caller, power, out var corridorRecognized) is { } corridorEffect)
            return corridorEffect;
        if (corridorRecognized)
            return null;
        if (effect.StartsWith("set_", StringComparison.Ordinal) && SummonedMonsterType(call.Receiver) is not null)
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
                if (!ResolveMutablePowerTarget(powerTargets, Key(instance), matching, out var trackedTarget))
                    return null;
                var forTarget = trackedTarget is not null
                    ? $" (with target={trackedTarget})"
                    : "";
                return new Effect($"attempt to apply {amount} {PowerLabel(applied)} to {who}{forTarget}", AddedPower: applied);
            }
            if (effect == "Remove")
            {
                if (generic is not null && args.Length >= 1 && ValueText(args[0]) is { } targetWho)
                    return targetWho is "self" or "owner"
                        ? new Effect($"remove {PowerLabel(generic)}")
                        : new Effect($"remove {PowerLabel(generic)} from {targetWho}");
                if (args.Length == 1 && PowerValue(args[0], power) is { } label)
                    return new Effect($"remove {label}");
                return null;
            }
            if (effect == "Decrement" && args.Length == 1)
                return PowerValue(args[0], power) is { } label
                    ? new Effect($"attempt to subtract 1 {label} stack")
                    : null;
            if (effect == "TickDownDuration" && args.Length == 1)
                return PowerValue(args[0], power) is { } label
                    ? new Effect($"subtract 1 {label} stack (or clear its skip-next-tick flag)")
                    : null;
            if (effect == "ModifyAmount" && args.Length > 2)
                return PowerValue(args[1], power) is { } label && ValueText(args[2]) is { } delta
                    ? new Effect($"change {label} by {delta}")
                    : null;
            return null;
        }

        if (type == typeof(DynamicVar) && effect == "set_BaseValue" && args is [var assigned])
        {
            if (DynamicKey(call.Receiver) is not { } key)
                return null;
            if (key == "DisplayAmount")
            {
                recognized = false;
                return null;
            }
            return ValueText(assigned) is { } amount
                ? new Effect($"set counter {HumanizeLower(key)} to {amount}", ChangesGuard: true)
                : null;
        }

        if (type == typeof(CreatureCmd))
        {
            if (effect == "Stun" && args.Length >= 1 && IsOwner(args[0]))
            {
                if (args.Length == 1 || args[^1] is Constant { Data: null })
                    return new Effect("attempt to stun owner for one action");
                var nextMoveArg = args[^1];
                var followUp = nextMoveArg is Constant { Data: string literal } ? literal : MoveIdFromStateId(nextMoveArg);
                if (followUp is not null && ShowableMove(followUp))
                {
                    _forcedMoves.Add(followUp);
                    return new Effect($"attempt to stun owner for one action, then → {followUp}");
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
                    && _hookOwner is { } blockOwner && !MutableDynamicKeys(blockOwner).Contains("Block")
                    && TryGetCanonicalDynamicVars(blockOwner) is { } blockVars
                    && blockVars.TryGetValue("Block", out var blockDyn) && blockDyn is BlockVar blockVar)
                {
                    amount = Number(blockVar.BaseValue);
                    unpoweredBlock = blockVar.Props.HasFlag(ValueProp.Unpowered);
                }
                else
                {
                    amount = ValueText(args[1]);
                    if (effect == "GainBlock" && args.Length >= 3 && Numeric(args[2]) is { } rawBlockProps
                        && ((ValueProp)(int)rawBlockProps).HasFlag(ValueProp.Unpowered))
                    {
                        unpoweredBlock = true;
                    }
                }
                if (amount is null)
                    return null;
                var blockKind = unpoweredBlock ? "unpowered" : "base";
                // GainBlock returns zero if the receiver is dead or combat is ending.
                if (effect == "GainBlock" && power is not null && IsOwner(args[0])
                    && caller.Name is "AfterAttack" or "AfterCardPlayed")
                    return new Effect($"if owner is alive and combat continues: {who} gains {blockKind} {amount} Block");
                return new Effect(effect switch
                {
                    "GainBlock" => $"{who} gains {blockKind} {amount} Block",
                    "Heal" => $"{who} heals {amount} HP",
                    "SetMaxHp" => $"set {who}'s maximum HP to {amount}",
                    _ => $"set {who}'s maximum and current HP to {amount}",
                });
            }
            if (effect == "LoseBlock" && args.Length >= 2)
            {
                var lbTarget = Argument(call, "creature", "target") ?? args[0];
                var lbAmountArg = Argument(call, "amount") ?? args[1];
                if (ValueText(lbTarget) is { } lbWho && Numeric(lbAmountArg) is { } lbAmt)
                    return new Effect(lbAmt >= 999999m ? $"remove all Block from {lbWho}" : $"{lbWho} loses {Number(lbAmt)} Block");
                if (ValueText(lbTarget) is { } lbWhoDyn && ValueText(lbAmountArg) is { } lbAmtDyn)
                    return new Effect($"{lbWhoDyn} loses {lbAmtDyn} Block");
            }
            if (effect is "LoseMaxHp" or "GainMaxHp")
            {
                var hpTarget = Argument(call, "creature", "target") ?? args.FirstOrDefault();
                var hpAmtArg = Argument(call, "amount") ?? args.ElementAtOrDefault(1);
                if (hpTarget is not null && hpAmtArg is not null && ValueText(hpTarget) is { } hpWho && ValueText(hpAmtArg) is { } hpAmt)
                    return new Effect(effect == "LoseMaxHp" ? $"{hpWho} loses {hpAmt} maximum HP" : $"{hpWho} gains {hpAmt} maximum HP");
            }
            if (effect == "Kill" && args.FirstOrDefault() is { } target)
            {
                if (ValueText(target) is not { } who)
                    return null;
                var forced = Argument(call, "force") is Constant { Data: 1 or true } ? " (ignoring death prevention)" : "";
                return new Effect($"kill {who}{forced}", KillsSelf: who is "self" or "owner");
            }
            if (effect == "Damage" && Argument(call, "target", "targets") is { } struck)
            {
                if (ValueText(struck) is not { } who)
                    return null;
                if (Argument(call, "amount") is { } dealt)
                {
                    if (ValueText(dealt) is not { } amount || ResolveDamageProps(Argument(call, "props"), power) is not { } props)
                        return null;
                    return new Effect($"deal {amount}{DamageFlags(props)} damage to {who}");
                }
                if (Argument(call, "damageVar") is { } dmgVarArg)
                {
                    var key = DynamicKey(dmgVarArg)
                        ?? (dmgVarArg is Call { Method.Name: "get_Damage" } ? "Damage" : null);
                    if (key is null)
                        return null;
                    string? dmgAmt = null;
                    var dmgProps = ValueProp.Unpowered;
                    var dmgOwner = power ?? _hookOwner;
                    if (dmgOwner is not null && TryGetCanonicalDynamicVars(dmgOwner) is { } dmgVars
                        && dmgVars.TryGetValue(key, out var dv) && dv is DamageVar canonicalDmgVar)
                    {
                        dmgProps = canonicalDmgVar.Props;
                        if (!MutableDynamicKeys(dmgOwner).Contains(key) && canonicalDmgVar.BaseValue > 0m)
                            dmgAmt = Number(canonicalDmgVar.BaseValue);
                    }
                    dmgAmt ??= $"counter {HumanizeLower(key)}";
                    return new Effect($"deal {dmgAmt}{DamageFlags(dmgProps)} damage to {who}");
                }
            }
            if (effect == "Escape" && args.FirstOrDefault() is { } escaping)
                return ValueText(escaping) is { } who ? new Effect($"{who} escapes combat") : null;
            if (effect == "Add")
            {
                var summonedType = generic is not null && typeof(MonsterModel).IsAssignableFrom(generic)
                    ? generic
                    : args.FirstOrDefault() is { } inst ? SummonedMonsterType(inst) : null;
                if (summonedType is not null)
                {
                    var overrides = DescribeSpawnOverrides(call, caller) ?? "";
                    return new Effect($"summon {ModelLabel(summonedType)}{overrides}", AddedSummon: summonedType);
                }
            }
            if (effect is "TriggerAnim" or "ChangeCreatureAnim")
                recognized = false;
            return null;
        }

        if (effect == "SetCurrentHpInternal" && type == typeof(Creature) && call.Receiver is not null && args is [var curHpArg]
            && ValueText(call.Receiver) is { } curHpWho && ValueText(curHpArg) is { } curHpVal)
        {
            return new Effect($"set {Possessive(curHpWho)} current HP to {curHpVal}");
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
            if (monster?.MoveStateMachine?.States.GetValueOrDefault(id) is MoveState { Intents.Count: 0 } passMove
                && HasNoGameplayEffects(passMove)
                && (passMove.FollowUpState ?? (passMove.FollowUpStateId is { } fid ? monster.MoveStateMachine.States.GetValueOrDefault(fid) : null)) is MoveState nextMove
                && EnemyMechanismReader.IsPublicMove(nextMove))
            {
                return new Effect($"immediately set next move to pass one turn, then → {nextMove.Id}", ForcedMove: id);
            }
            return null;
        }

        // Monster, power, or relic scalar state (bool or gameplay int).
        if (call.Receiver == Self && effect.StartsWith("set_", StringComparison.Ordinal) && args is [var assignedValue])
        {
            var propName = effect[4..];
            var backing = type is not null ? BackingField(type, (MethodInfo)call.Method) : null;
            if (backing is not null && !IsGameplayField(backing))
            {
                recognized = false;
                return null;
            }
            if (args is [Constant { Data: int flag }] && flag is 0 or 1
                && (flags.Contains(propName)
                    || backing is not null && backing.FieldType == typeof(bool)
                    || (power is not null && type == power || type is not null && typeof(RelicModel).IsAssignableFrom(type))
                        && call.Method.GetParameters() is [{ ParameterType: var flagType }] && flagType == typeof(bool)))
                return new Effect($"{HumanizeLower(propName)} = {(flag == 1 ? "true" : "false")}", ChangesGuard: true);
            if ((power is null || (type is not null && typeof(RelicModel).IsAssignableFrom(type)))
                && call.Method.GetParameters() is [{ ParameterType: var intType }] && intType == typeof(int)
                && type is not null && BackingField(type, (MethodInfo)call.Method) is { } intBacking && IsGameplayField(intBacking))
            {
                if (_choiceCursors.Contains(intBacking)
                    && assignedValue is Operation { Operator: "add", Right: Constant { Data: 1 } })
                    return new Effect("increment choice rounds completed", ChangesGuard: true);
                if (ValueText(assignedValue) is { } intText)
                {
                    if (power is null && !typeof(RelicModel).IsAssignableFrom(type))
                    {
                        if (caller.Name == "AfterAddedToRoom")
                            _initializedMonsterInts.Add(propName);
                        else
                            _trackedMonsterInts.Add((propName, intBacking));
                    }
                    return new Effect($"{HumanizeLower(propName)} = {intText}", ChangesGuard: true);
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
            var handOverflow = (PileType)pile == PileType.Hand ? " (overflows to discard pile if hand is full)" : "";
            return new Effect($"add {count} {ModelLabel(generic)} to {Possessive(who)} {((PileType)pile).ToString().ToLowerInvariant()} pile{where}{handOverflow}", AddedCard: generic);
        }

        if (type == typeof(CardPileCmd) && effect is "AddGeneratedCardToCombat" or "AddGeneratedCardsToCombat")
            return ReadGeneratedCard(call);

        if (type == typeof(CardPileCmd) && effect == "Add" && args.Length >= 2
            && ValueText(args[0]) is { } movedCards
            && Numeric(Argument(call, "newPileType") ?? args[1]) is { } destPileNum
            && Enum.IsDefined(typeof(PileType), (int)destPileNum))
        {
            var destPile = (PileType)(int)destPileNum;
            var destPos = Numeric(Argument(call, "position")) is { } rawDestPos ? (CardPilePosition)(int)rawDestPos : CardPilePosition.Bottom;
            var destWhere = destPile == PileType.Hand
                ? "into hand (overflows to discard pile if hand is full)"
                : destPile == PileType.Draw && destPos != CardPilePosition.Bottom
                    ? $"to {PileLocationLabel(destPile)} ({destPos.ToString().ToLowerInvariant()})"
                    : $"to {PileLocationLabel(destPile)}";
            return new Effect($"move {movedCards} {destWhere}");
        }

        if (type == typeof(CardPileCmd) && effect == "Draw")
        {
            var drawCountArg = Argument(call, "count");
            var drawPlayerArg = Argument(call, "player") ?? args.LastOrDefault();
            var drawCount = drawCountArg is not null ? ValueText(drawCountArg) : "1";
            var drawWho = drawPlayerArg is not null ? ValueText(drawPlayerArg) : "owner";
            if (drawCount is not null && drawWho is not null)
                return new Effect($"{drawWho} draws {drawCount} {(drawCount == "1" ? "card" : "cards")}");
        }

        if (type == typeof(CardPileCmd) && effect == "AutoPlayFromDrawPile")
        {
            var apPlayerArg = Argument(call, "player") ?? args.ElementAtOrDefault(1);
            var apCountArg = Argument(call, "count") ?? args.ElementAtOrDefault(2);
            var apPosArg = Argument(call, "position") ?? args.ElementAtOrDefault(3);
            var forceExhaustArg = Argument(call, "forceExhaust") ?? args.ElementAtOrDefault(4);
            string exhaustSuffix;
            if (forceExhaustArg is null || forceExhaustArg is Constant { Data: 0 or false })
                exhaustSuffix = "";
            else if (forceExhaustArg is Constant { Data: 1 or true })
                exhaustSuffix = $" (setting ExhaustOnNextPlay so non-Power non-dupe cards default to {KeywordLabel(CardKeyword.Exhaust)} after play unless overridden by a destination modifier)";
            else
                return null;
            if (apPlayerArg is not null && apCountArg is not null && ValueText(apPlayerArg) is { } apWho && ValueText(apCountArg) is { } apCnt)
            {
                var posLabel = Numeric(apPosArg) is { } rawPos ? ((CardPilePosition)(int)rawPos).ToString().ToLowerInvariant() + " " : "";
                return new Effect($"auto-play the {posLabel}{apCnt} card(s) of {Possessive(apWho)} draw pile{exhaustSuffix}");
            }
        }

        if (type == typeof(CardPileCmd) && effect == "ShuffleIfNecessary")
        {
            var shufPlayer = Argument(call, "player") ?? args.ElementAtOrDefault(1);
            var shufWho = shufPlayer is not null ? ValueText(shufPlayer) : "owner";
            if (shufWho is not null)
                return new Effect($"shuffle {Possessive(shufWho)} discard pile into draw pile if draw pile is empty");
        }

        if (type == typeof(CardSelectCmd) && effect is "FromHand" or "FromHandForDiscard" or "FromCombatPile")
        {
            if (IsConsumedCardSelection(call, caller))
            {
                recognized = false;
                return null;
            }
            return null;
        }

        if (type == typeof(PlayerCmd))
        {
            if (effect == "GainEnergy" && args.Length >= 2
                && ValueText(args[0]) is { } enAmt && ValueText(args[1]) is { } enWho)
                return new Effect($"{enWho} gains {enAmt} Energy");
            if (effect == "LoseEnergy" && args.Length >= 2
                && ValueText(args[0]) is { } lostEnAmt && ValueText(args[1]) is { } lostEnWho)
                return new Effect($"{lostEnWho} loses {lostEnAmt} Energy");
            if (effect == "GainStars" && args.Length >= 2
                && ValueText(args[0]) is { } starAmt && ValueText(args[1]) is { } starWho)
                return new Effect($"{starWho} gains {starAmt} {(starAmt == "1" ? "Star" : "Stars")}");
            if (effect == "GainGold" && args.Length >= 2
                && ValueText(args[0]) is { } goldAmt && ValueText(args[1]) is { } goldWho)
                return new Effect($"{goldWho} gains {goldAmt} Gold");
        }

        if (type == typeof(OrbCmd))
        {
            if (effect == "Channel" && generic is not null && typeof(OrbModel).IsAssignableFrom(generic))
            {
                var orbPlayer = Argument(call, "player") ?? args.ElementAtOrDefault(1);
                var orbWho = orbPlayer is not null ? ValueText(orbPlayer) : "owner";
                if (orbWho is not null)
                    return new Effect($"{orbWho} channels 1 {ModelLabel(generic)}");
            }
            if (effect == "Channel" && generic is null)
            {
                var orbArg = Argument(call, "orb") ?? args.ElementAtOrDefault(1);
                var orbPlayer = Argument(call, "player") ?? args.ElementAtOrDefault(2);
                var orbWho = orbPlayer is not null ? ValueText(orbPlayer) : "owner";
                if (orbWho is not null && DescribeOrbValue(orbArg) is { } orbLabel)
                    return new Effect($"{orbWho} channels {orbLabel}");
            }
            if (effect == "Passive")
            {
                var orbArg = Argument(call, "orb") ?? args.ElementAtOrDefault(1);
                var affectedByHooks = Argument(call, "countAffectedByHooks") is Constant { Data: 1 or true };
                if (orbArg is not null && ValueText(orbArg) is { } orbTarget)
                {
                    var hookNote = affectedByHooks ? " (affected by passive trigger count modifiers)" : "";
                    return new Effect($"trigger the passive effect of {orbTarget}{hookNote}");
                }
            }
            if (effect == "AddSlots")
            {
                var slotPlayer = Argument(call, "player") ?? args.ElementAtOrDefault(0);
                var slotAmt = Argument(call, "amount") ?? args.ElementAtOrDefault(1);
                if (slotPlayer is not null && slotAmt is not null
                    && ValueText(slotPlayer) is { } slotWho && ValueText(slotAmt) is { } slotCount)
                    return new Effect($"{slotWho} gains {slotCount} orb {(slotCount == "1" ? "slot" : "slots")}");
            }
            if (effect == "EvokeLast" || effect == "EvokeNext")
            {
                var orbPlayer = Argument(call, "player") ?? args.ElementAtOrDefault(1);
                var orbWho = orbPlayer is not null ? ValueText(orbPlayer) : "owner";
                if (orbWho is not null)
                    return new Effect($"{orbWho} evokes their {(effect == "EvokeLast" ? "last" : "next")} orb");
            }
        }

        if (type == typeof(ForgeCmd) && effect == "Forge")
        {
            var forgeAmt = Argument(call, "amount") ?? args.ElementAtOrDefault(0);
            var forgePlayer = Argument(call, "player") ?? args.ElementAtOrDefault(1);
            if (forgeAmt is not null && forgePlayer is not null
                && ValueText(forgeAmt) is { } forgeAmountText && ValueText(forgePlayer) is { } forgeWho)
                return new Effect($"{forgeWho} forges {forgeAmountText}");
        }

        if (type == typeof(MegaCrit.Sts2.Core.Rooms.CombatRoom) && effect == "AddExtraReward" && args.Length >= 2)
        {
            if (ValueText(args[0]) is { } rewardWho && args[1] is Call { Method: ConstructorInfo { DeclaringType: var rewardType }, Arguments: var rArgs })
            {
                if (rewardType == typeof(MegaCrit.Sts2.Core.Rewards.GoldReward) && rArgs.Length >= 1 && ValueText(rArgs[0]) is { } goldAmt)
                    return new Effect($"add an extra {goldAmt} Gold reward for {rewardWho}");
                if (rewardType == typeof(MegaCrit.Sts2.Core.Rewards.CardRemovalReward))
                    return new Effect($"add an extra card removal reward for {rewardWho}");
            }
        }

        if (type == typeof(CardEnergyCost) && effect == "AddUntilPlayed" && args is [var costDeltaArg, ..]
            && call.Receiver is Call { Method.Name: "get_EnergyCost", Receiver: var costTarget }
            && ValueText(costTarget) is { } costTargetText)
        {
            if (Numeric(costDeltaArg) is { } costDeltaNum)
            {
                var verb = costDeltaNum < 0m ? $"reduce the Energy cost of {costTargetText} by {Number(-costDeltaNum)}" : $"increase the Energy cost of {costTargetText} by {Number(costDeltaNum)}";
                return new Effect($"{verb} until played");
            }
            if (ValueText(costDeltaArg) is { } costDeltaText)
                return new Effect($"change the Energy cost of {costTargetText} by {costDeltaText} until played");
        }

        if (type == typeof(OstyCmd) && effect == "Summon")
        {
            var ostyWho = Argument(call, "summoner", "player") is { } sumArg ? ValueText(sumArg) : "owner";
            var ostyHp = Argument(call, "amount", "hp") is { } hpArg ? ValueText(hpArg) : (args.Length >= 3 ? ValueText(args[2]) : null);
            if (ostyWho is not null && ostyHp is not null)
                return new Effect($"{ostyWho} summons {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} with {ostyHp} HP (or if already alive, increases its max HP and heals it by {ostyHp})");
        }

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
            if (effect == "Upgrade" && args.Length >= 1 && ValueText(args[0]) is { } upgradedCards)
                return new Effect($"upgrade {upgradedCards}");
            if (effect == "Discard" && args.Length >= 2 && ValueText(args[1]) is { } discardedCards)
                return new Effect($"discard {discardedCards}");
            if (effect == "Exhaust" && args.Length >= 2 && ValueText(args[1]) is { } exhaustedCards)
                return new Effect($"exhaust {exhaustedCards}");
            if (effect == "TransformToRandom" && args.Length >= 1 && ValueText(args[0]) is { } transformedCards)
                return new Effect($"transform {transformedCards} into a random card");
            if (effect == "AutoPlay" && args.Length >= 2 && ValueText(args[1]) is { } autoCard)
                return new Effect($"auto-play {autoCard}");
            if (effect == "ApplyKeyword" && args.Length >= 2)
            {
                if (ValueText(args[0]) is not { } targetCard)
                    return null;
                if (args[1] is not ArrayValue { Elements.Length: > 0 } keywordArray)
                    return null;
                var keywords = new List<string>(keywordArray.Elements.Length);
                foreach (var elem in keywordArray.Elements)
                {
                    if (elem is Constant { Data: int rawKw } && Enum.IsDefined(typeof(CardKeyword), rawKw))
                        keywords.Add(KeywordLabel((CardKeyword)rawKw));
                    else
                        return null;
                }
                return new Effect($"apply {string.Join(", ", keywords)} to {targetCard}");
            }
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
        Call { Method.Name: "get_Monster", Receiver: Call { Method: { Name: "Add", IsGenericMethod: true } add } }
            when add.DeclaringType == typeof(CreatureCmd)
            => add.GetGenericArguments()[0],
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
        if (monster is not null
            && value is Call { Method: MethodInfo getter, Receiver: var receiver, Arguments.Length: 0 }
            && getter.Name.StartsWith("get_", StringComparison.Ordinal)
            && IsSelfOrOwnedMonster(receiver)
            && Body(getter).Returns is [{ Value: Field { Receiver: Input { Name: "this" } } field, Conditions.Length: 0 }]
            && field.Member.DeclaringType?.IsInstanceOfType(monster) == true)
            return (field.Member.GetValue(monster) as MoveState)?.Id;
        return null;
    }

    private bool IsSelfOrOwnedMonster(Value? receiver)
        => receiver == Self
            || monster is not null && receiver is Operation { Operator: "castclass" or "isinst", Left: Call { Method.Name: "get_Monster" }, Right: Constant { Data: Type t } }
                && t.IsInstanceOfType(monster)
            || monster is not null && receiver is Input { Type: { } paramType } && paramType.IsInstanceOfType(monster);

    private string? PowerValue(Value value, Type? power)
        => value switch
        {
            Input { Name: "this" } when power is not null => PowerLabel(power),
            Call call when call.Method.Name == "GetPower" && call.Method.DeclaringType == typeof(Creature)
                && call.Method.IsGenericMethod && IsOwner(call.Receiver) => PowerLabel(call.Method.GetGenericArguments()[0]),
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator",
                Receiver: Call { Method.Name: "ToList", Arguments: [Call { Method: { Name: "GetPowerInstances", IsGenericMethod: true } inst, Receiver: var who }] } } }
                when IsOwner(who) => $"each {PowerLabel(inst.GetGenericArguments()[0])}",
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator",
                Receiver: Call { Method.Name: "ToList", Arguments: [Call { Method.Name: "Where",
                    Arguments: [Call { Method.Name: "get_Powers", Receiver: var pWho },
                        Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }] } } }
                when IsOwner(pWho) && ExcludedPowerLabels(pred) is { Count: > 0 } excluded
                => $"each power on {(power is null ? "self" : "owner")} except {string.Join(" and ", excluded)}",
            _ => null,
        };

    private List<string> ExcludedPowerLabels(MethodInfo pred)
    {
        var labels = new List<string>();
        var body = Body(pred);
        if (body.Incomplete)
            return labels;
        foreach (var ret in body.Returns)
        {
            foreach (var pt in FindPowerTypesInExpr(ret.Value).Concat(ret.Conditions.SelectMany(c => FindPowerTypesInExpr(c.Value))))
            {
                var label = PowerLabel(pt);
                if (!labels.Contains(label, StringComparer.Ordinal))
                    labels.Add(label);
            }
        }
        return labels;
    }

    private static IEnumerable<Type> FindPowerTypesInExpr(Value value) => value switch
    {
        Operation { Operator: "isinst", Right: Constant { Data: Type t } } when typeof(PowerModel).IsAssignableFrom(t) => [t],
        Operation op => FindPowerTypesInExpr(op.Left).Concat(op.Right is { } r ? FindPowerTypesInExpr(r) : []),
        Call call => (call.Receiver is { } rec ? FindPowerTypesInExpr(rec) : []).Concat(call.Arguments.SelectMany(FindPowerTypesInExpr)),
        _ => [],
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
        => value is Call { Receiver: Input { Name: "this" }, Method.Name: "get_Owner" or "get_Creature" }
            || value is Call { Method.Name: "get_Creature", Receiver: Call { Receiver: Input { Name: "this" }, Method.Name: "get_Owner" } };

    private ValueProp? ResolveDamageProps(Value? propsArg, Type? power)
    {
        if (propsArg is null)
            return (ValueProp)0;
        if (propsArg is Constant { Data: int rawProps })
            return (ValueProp)rawProps;
        var owner = power ?? _hookOwner;
        if (propsArg is Call { Method.Name: "get_Props", Receiver: var rec } && owner is not null)
        {
            var inner = rec is Operation { Operator: "castclass" or "isinst", Left: var castInner } ? castInner : rec;
            if (DynamicKey(inner) is { } key && TryGetCanonicalDynamicVars(owner) is { } vars
                && vars.TryGetValue(key, out var dv) && dv is DamageVar dmgVar)
                return dmgVar.Props;
        }
        return null;
    }

    private static string? DynamicKey(Value? value)
    {
        if (value is Operation { Operator: "castclass" or "isinst", Left: var inner })
            value = inner;
        if (value is Call { Method.DeclaringType: var type, Receiver: Call { Method.Name: "get_DynamicVars", Receiver: Input { Name: "this" } } } dvCall
            && type == typeof(DynamicVarSet))
        {
            if (dvCall.Method.Name == "get_Item" && dvCall.Arguments is [Constant { Data: string key }])
                return key;
            if (dvCall.Method.Name.StartsWith("get_", StringComparison.Ordinal) && dvCall.Arguments.Length == 0)
                return dvCall.Method.Name[4..];
        }
        return null;
    }

    private bool ShowableMove(string id)
        => monster?.MoveStateMachine?.States.GetValueOrDefault(id) is MoveState move && EnemyMechanismReader.IsPublicMove(move);

    private string? BooleanGetter(Value value)
        => value is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" } }
            && getter.ReturnType == typeof(bool) && getter.Name.StartsWith("get_", StringComparison.Ordinal)
            && (monster is not null && getter.DeclaringType?.IsInstanceOfType(monster) == true || getter.DeclaringType is { } owner && owner == _hookOwner)
            ? getter.Name[4..]
            : null;

    private string? BooleanField(Value value)
    {
        if (monster is null || value is not Field { Receiver: Input { Name: "this" } } field || field.Member.FieldType != typeof(bool)
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

    private static RelicModel? TryGetCanonicalRelic(Type type)
    {
        if (!typeof(RelicModel).IsAssignableFrom(type))
            return null;
        try
        {
            return ModelDb.GetByIdOrNull<RelicModel>(ModelDb.GetId(type));
        }
        catch
        {
            return null;
        }
    }

    private static DynamicVarSet? TryGetCanonicalDynamicVars(Type type)
    {
        if (TryGetCanonicalPower(type) is { } p)
            return p.DynamicVars;
        if (TryGetCanonicalRelic(type) is { } r)
            return r.DynamicVars;
        return null;
    }

    private static PowerModel? TryGetCanonicalPower(Type type)
    {
        try
        {
            return ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(type));
        }
        catch
        {
            return null;
        }
    }

    private static bool IsModelDbType(Type type)
        => typeof(MonsterModel).IsAssignableFrom(type) && type != typeof(MonsterModel)
            || typeof(PowerModel).IsAssignableFrom(type) && type != typeof(PowerModel)
            || typeof(CardModel).IsAssignableFrom(type) && type != typeof(CardModel)
            || typeof(RelicModel).IsAssignableFrom(type) && type != typeof(RelicModel)
            || typeof(PotionModel).IsAssignableFrom(type) && type != typeof(PotionModel)
            || typeof(EnchantmentModel).IsAssignableFrom(type) && type != typeof(EnchantmentModel)
            || typeof(OrbModel).IsAssignableFrom(type) && type != typeof(OrbModel)
            || typeof(EventModel).IsAssignableFrom(type) && type != typeof(EventModel)
            || typeof(AfflictionModel).IsAssignableFrom(type) && type != typeof(AfflictionModel);

    internal static string ModelLabel(Type type)
    {
        var id = ModelDb.GetId(type);
        MegaCrit.Sts2.Core.Localization.LocString? title = null;
        try
        {
            title = ModelDb.GetByIdOrNull<AbstractModel>(id) switch
            {
                PowerModel power => MegaCrit.Sts2.Core.Nodes.NGame.Instance is not null
                    || power.GetType().GetProperty(nameof(PowerModel.Title))?.DeclaringType == typeof(PowerModel)
                        ? power.Title
                        : new MegaCrit.Sts2.Core.Localization.LocString("powers", $"{power.Id.Entry}.title"),
                AfflictionModel affliction => affliction.Title,
                EnchantmentModel enchantment => enchantment.Title,
                OrbModel orb => orb.Title,
                PotionModel potion => potion.Title,
                EventModel ev => ev.Title,
                MonsterModel monster => MegaCrit.Sts2.Core.Nodes.NGame.Instance is not null
                    || monster.GetType().GetProperty(nameof(MonsterModel.Title))?.DeclaringType == typeof(MonsterModel)
                        ? monster.Title
                        : MonsterModel.L10NMonsterLookup($"{monster.Id.Entry}.name"),
                EncounterModel encounter => encounter.Title,
                RelicModel relic => relic.Title,
                CardModel card => card.TitleLocString,
                _ => null,
            };
            if (title is not null && !title.Exists())
                title = null;
        }
        catch
        {
            title = null;
        }

        if (title is null)
        {
            if (typeof(MonsterModel).IsAssignableFrom(type))
            {
                foreach (var entry in MonsterLocEntries(type, id.Entry))
                {
                    var candidate = MonsterModel.L10NMonsterLookup($"{entry}.name");
                    if (candidate.Exists())
                    {
                        title = candidate;
                        break;
                    }
                }
            }
            else
            {
                title = type switch
                {
                    _ when typeof(PowerModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("powers", $"{id.Entry}.title"),
                    _ when typeof(CardModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("cards", $"{id.Entry}.title"),
                    _ when typeof(RelicModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("relics", $"{id.Entry}.title"),
                    _ when typeof(PotionModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("potions", $"{id.Entry}.title"),
                    _ when typeof(EnchantmentModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("enchantments", $"{id.Entry}.title"),
                    _ when typeof(OrbModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("orbs", $"{id.Entry}.title"),
                    _ when typeof(EventModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("events", $"{id.Entry}.title"),
                    _ when typeof(AfflictionModel).IsAssignableFrom(type) => new MegaCrit.Sts2.Core.Localization.LocString("afflictions", $"{id.Entry}.title"),
                    _ => null,
                };
            }
        }

        try
        {
            if (title?.Exists() == true)
                return $"{title.GetFormattedText()} ({id})";
            if (ModelDb.GetByIdOrNull<AbstractModel>(id) is CardModel cardModel)
            {
                var cardTitle = cardModel.Title;
                if (!string.IsNullOrWhiteSpace(cardTitle)
                    && !string.Equals(cardTitle, id.Entry, StringComparison.Ordinal)
                    && !cardTitle.Contains("MISSING", StringComparison.OrdinalIgnoreCase))
                {
                    return $"{cardTitle} ({id})";
                }
            }
        }
        catch
        {
            // Fall back to stable ID when dynamic localization placeholders require a live owner.
        }
        return id.ToString();
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
        // Drop `<subject> is in a pile` when `<subject> is in <Pile>` already appears in the same guard.
        distinct.RemoveAll(item => item.EndsWith(" is in a pile", StringComparison.Ordinal)
            && distinct.Any(other => other != item && other.StartsWith(item[..^" is in a pile".Length] + " is in ", StringComparison.Ordinal)));
        // Drop an abstract monster-family check when the guard already equates event target with owner of that family.
        if (monster is not null && (distinct.Contains("owner=event target") || distinct.Contains("event target=owner")))
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
        var prevCycle = _getterCycleDetected;
        _getterCycleDetected = false;
        var combat = CombatCondition(test);
        var hitCycle = _getterCycleDetected;
        _getterCycleDetected = prevCycle || hitCycle;
        if (hitCycle)
            return null;
        if (combat is not null)
            return combat;
        if (value is Operation { Operator: "ceq", Right: Constant { Data: 0 or null } } negation
            && (negation.Left is Call { Method: MethodInfo { ReturnType: var type } } && type == typeof(bool)
                || negation.Left is Operation { Operator: "ceq" or "cgt" or "cgt.un" or "clt" or "clt.un" }
                || negation.Left is Operation { Operator: "isinst" }))
            return Condition(test with { Value = negation.Left, Expected = !expected });
        if (value is Operation { Operator: "cgt.un", Left: Operation { Operator: "isinst" } positiveTypeCheck, Right: Constant { Data: 0 or null } })
            return Condition(test with { Value = positiveTypeCheck, Expected = expected });
        if (value is Operation { Operator: "isinst", Left: Constant { Data: null } })
            return expected ? Never : "";
        if (value is Operation { Operator: "isinst", Left: Input { Name: "room" }, Right: Constant { Data: Type roomType } }
            && typeof(MegaCrit.Sts2.Core.Rooms.AbstractRoom).IsAssignableFrom(roomType))
        {
            var roomLabel = roomType.Name == "CombatRoom" ? "a combat room" : HumanizeLower(roomType.Name);
            return $"room {(expected ? "is" : "is not")} {roomLabel}";
        }
        if (value is Operation { Operator: "rem", Left: var remCondL, Right: { } remCondR }
            && ValueText(remCondL) is { } rcl && ValueText(remCondR) is { } rcr)
        {
            return expected ? $"({rcl} mod {rcr})≠0" : $"({rcl} mod {rcr})=0";
        }
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
            if ((IsOwner(operation.Left) && right is Constant { Data: null }
                    || operation.Left is Constant { Data: null } && IsOwner(right))
                && op is "=" or "≠")
                return op == "≠" ? "" : Never;
            if (operation.Left is Call { Method.Name: "get_GameMode" }
                && Numeric(right) is { } gameModeVal && op is "=" or "≠")
            {
                var currentMode = (int)(MegaCrit.Sts2.Core.Runs.RunManager.Instance.DebugOnlyGetState()?.GameMode
                    ?? MegaCrit.Sts2.Core.Runs.GameMode.Standard);
                var matchesMode = op == "=" ? currentMode == (int)gameModeVal : currentMode != (int)gameModeVal;
                return matchesMode ? "" : Never;
            }
            if (operation.Left is Call { Method.Name: "get_NextMove", Receiver: Input { Name: "this" } }
                && MoveId(right) is { } move && ShowableMove(move) && op is "=" or "≠")
                return $"next move{op}{move}";
            if (operation.Left is Input { Name: "side" } && Numeric(right) is { } side && op is not null)
                return $"turn side{op}{SideLabel((CombatSide)side)}";
            if (operation.Left is Call { Method.Name: "get_CurrentSide" } && Numeric(right) is { } acting && op is "=" or "≠")
                return $"acting side{op}{SideLabel((CombatSide)acting)}";
            if (operation.Left is Call { Method.Name: "get_Side", Receiver: var creature } && IsOwner(creature)
                && Numeric(right) is { } ownerSide && op is "=" or "≠")
                return $"owner's side{op}{SideLabel((CombatSide)ownerSide)}";
            if (operation.Left is Call { Method.Name: "get_Side", Receiver: Input { Name: "creature" or "target" } }
                && right is Call { Method.Name: "get_Side", Receiver: var ownerSideCreature } && IsOwner(ownerSideCreature)
                && op is "=" or "≠")
                return $"event target's side{op}owner's side";
            if (operation.Left is Call { Method.Name: "get_Type", Receiver: var typedCard }
                && Numeric(right) is { } cardType && Enum.IsDefined(typeof(CardType), (int)cardType) && op is "=" or "≠"
                && typedCard is not Call { Method.Name: "get_Pile" })
            {
                var subject = typedCard is Call { Method.Name: "get_Card" } ? "played card" : "that card";
                var kindName = ((CardType)(int)cardType).ToString();
                var article = kindName == "Attack" ? "an" : "a";
                return op == "=" ? $"{subject} is {article} {kindName}" : $"{subject} is not {article} {kindName}";
            }
            if (UnwrapPileTypeExpr(operation.Left) is { } pileCheckedCard
                && Numeric(right) is { } rawPileType && Enum.IsDefined(typeof(PileType), (int)rawPileType) && op is "=" or "≠")
            {
                var subject = pileCheckedCard is Call { Method.Name: "get_Card" } ? "played card" : "that card";
                var pileName = ((PileType)(int)rawPileType).ToString();
                return op == "=" ? $"{subject} is in {pileName}" : $"{subject} is not in {pileName}";
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
            if (call.Method.DeclaringType == typeof(decimal) && call.Arguments is [var decLeft, var decRight])
            {
                var decOp = name switch
                {
                    "op_Equality" => "ceq",
                    "op_Inequality" => "bne.un",
                    "op_GreaterThan" => "cgt",
                    "op_GreaterThanOrEqual" => "bge",
                    "op_LessThan" => "clt",
                    "op_LessThanOrEqual" => "ble",
                    _ => null,
                };
                if (decOp is not null)
                    return Condition(test with { Value = new Operation(decOp, decLeft, decRight) });
            }
            if (name == "Contains" && call.Arguments is [Input { Name: "participants" }, var participant] && IsOwner(participant))
                return expected ? "owner participates this turn" : "owner does not participate this turn";
            if (name == "Contains" && call.Arguments is [Call { Method.Name: "get_Tags", Receiver: var taggedCard }, Constant { Data: int rawTag }]
                && Enum.IsDefined(typeof(CardTag), rawTag))
            {
                var tagSubject = taggedCard is Call { Method.Name: "get_Card" } ? "played card" : "that card";
                return $"{tagSubject} {(expected ? "has" : "lacks")} tag {(CardTag)rawTag}";
            }
            if (name == "Contains" && call.Receiver is Call { Method.Name: "get_Keywords", Receiver: var kwCard }
                && call.Arguments is [Constant { Data: int rawKw }] && Enum.IsDefined(typeof(CardKeyword), rawKw))
            {
                var kwSubject = kwCard is Call { Method.Name: "get_Card" } ? "played card" : "that card";
                return $"{kwSubject} {(expected ? "has" : "lacks")} keyword {(CardKeyword)rawKw}";
            }
            if (name == "Contains" && call.Arguments is [Call { Method.Name: "get_Pets", Receiver: var petOwner }, var petCandidate]
                && IsOwner(petOwner) && ValueText(petCandidate) is { } petWho)
                return expected ? $"{petWho} is owner's pet" : $"{petWho} is not owner's pet";
            if (name == nameof(Enum.HasFlag) && call.Method.DeclaringType == typeof(Enum)
                && call.Receiver is Input { Name: "props" } && call.Arguments is [Constant { Data: int rawFlag }])
            {
                var flag = (ValueProp)rawFlag;
                var flagLabel = flag switch
                {
                    ValueProp.Unpowered => expected ? "unpowered" : "powered",
                    ValueProp.Unblockable => expected ? "unblockable" : "blockable",
                    ValueProp.Move => expected ? "from a move or card" : "not from a move or card",
                    _ => $"{(expected ? "has" : "lacks")} {flag} property",
                };
                return flagLabel;
            }
            if (name == "IsCardOrMonsterMove")
                return expected ? "from a card or move" : "not from a card or move";
            if (name == "get_WasFullyBlocked")
                return expected ? "hit was fully blocked" : "hit was not fully blocked";
            if (name is "HasPower" or "GetPower" && call.Method.DeclaringType == typeof(Creature) && call.Method.IsGenericMethod)
            {
                if (IsOwner(call.Receiver))
                    return $"{(expected ? "has" : "lacks")} {PowerLabel(call.Method.GetGenericArguments()[0])}";
                if (call.Receiver is not null && ValueText(call.Receiver) is { } powerHolderWho)
                    return $"{powerHolderWho} {(expected ? "has" : "lacks")} {PowerLabel(call.Method.GetGenericArguments()[0])}";
            }
            if (name == "IsPoweredAttack")
                return expected ? "powered attack" : "not a powered attack";
            if (name == "IsPoweredCardOrMonsterMoveBlock")
                return expected ? "powered Block" : "not powered Block";
            if (name == "get_UnblockedDamage")
                return expected ? "nonzero HP lost" : "no HP lost";
            if (name == "get_AmountOnTurnStart" && call.Receiver == Self)
                return expected ? "present at turn start" : "not present at turn start";
            if (name == "get_Affliction")
                return expected ? "that card already has an affliction" : "that card has no affliction";
            if (name == "get_IsPlayer" && call.Receiver is Call { Method.Name: "get_Current",
                Receiver: Call { Method.Name: "GetEnumerator",
                    Receiver: Call { Method.Name: "ToList", Arguments: [Call { Method.Name: "get_Allies" }] } } })
                return expected ? "" : "ally is not a player"; // The card collection expression already quantifies over player combat decks.
            if (name == "get_CombatState" && call.Receiver is Input { Name: "oldOwner" })
                return expected ? "former owner still attached to combat" : "former owner detached from combat";
            if (name == "get_HasValue" && call.Method.DeclaringType is { IsGenericType: true } nullableDecl
                && nullableDecl.GetGenericTypeDefinition() == typeof(Nullable<>))
            {
                if (call.Receiver is Constant { Data: 0 })
                    return expected ? Never : "";
                if (call.Receiver is Call { Method: ConstructorInfo })
                    return expected ? "" : Never;
            }
            if (name == "GetValueOrDefault" && call.Method.DeclaringType is { IsGenericType: true } nullableValDecl
                && nullableValDecl.GetGenericTypeDefinition() == typeof(Nullable<>))
            {
                if (call.Receiver is Constant { Data: 0 })
                    return expected ? Never : "";
                if (call.Receiver is Call { Method: ConstructorInfo, Arguments: [var wrappedVal] })
                    return Condition(test with { Value = wrappedVal });
            }
            if (name == "get_Count" && ValueText(call) is { } countCondText)
                return expected ? $"{countCondText}>0" : $"{countCondText}=0";
            if (name == "GetWithModifiers" && call.Receiver is Call { Method.Name: "get_EnergyCost" } && ValueText(call) is { } modEnergyText)
                return expected ? $"{modEnergyText}≠0" : $"{modEnergyText}=0";
            if (name is "get_EnergySpent" or "get_EnergyValue" && call.Receiver is Call { Method.Name: "get_Resources" } && ValueText(call) is { } resEnergyText)
                return expected ? $"{resEnergyText}≠0" : $"{resEnergyText}=0";
            if (name == "get_IsLastInSeries")
                return expected ? "played card is the last play in its series" : "played card is not the last play in its series";
            if (name == "get_IsAutoPlay")
                return expected ? "played card is auto-played" : "played card is not auto-played";
            if (name == "get_CostsX" && call.Receiver is Call { Method.Name: "get_EnergyCost", Receiver: var xCard }
                && ValueText(xCard) is { } xCardLabel)
                return expected ? $"{xCardLabel} has X Energy cost" : $"{xCardLabel} does not have X Energy cost";
            if (name == "get_Pile" && call.Method.DeclaringType == typeof(CardModel))
            {
                var pileSubject = call.Receiver is Call { Method.Name: "get_Card" } ? "played card" : "that card";
                return expected ? $"{pileSubject} is in a pile" : $"{pileSubject} has no pile";
            }
            if (name == "get_IsUpgradable" && call.Receiver is { } upgCheck && ValueText(upgCheck) is { } upgWhat)
                return expected ? $"{upgWhat} is upgradable" : $"{upgWhat} is not upgradable";
            if (name == "get_IsAlive" && call.Receiver is Call { Method.Name: "get_Creature" })
                return expected ? "self alive" : "self dead";
            if (name == "get_IsAlive" && call.Receiver is { } aliveCheck && ValueText(aliveCheck) is { } aliveWho)
                return expected ? $"{aliveWho} is alive" : $"{aliveWho} is dead";
            if (name == "get_IsDead" && call.Receiver is { } deadCheck && ValueText(deadCheck) is { } deadWho)
                return expected ? $"{deadWho} is dead" : $"{deadWho} is alive";
            if (name == "get_IsPlayer" && call.Receiver is { } isPlayerRec && ValueText(isPlayerRec) is { } isPlayerWho)
                return expected ? $"{isPlayerWho} is a player" : $"{isPlayerWho} is not a player";
            if (name == "get_IsEnemy" && call.Receiver is { } isEnemyRec && ValueText(isEnemyRec) is { } isEnemyWho)
                return expected ? $"{isEnemyWho} is an enemy" : $"{isEnemyWho} is not an enemy";
            if (name == "get_IsMonster" && call.Receiver is { } isMonsterRec && ValueText(isMonsterRec) is { } isMonsterWho)
                return expected ? $"{isMonsterWho} is a monster/pet" : $"{isMonsterWho} is not a monster/pet";
            if (IsFreeSlotSearch(call))
                return expected ? "an encounter slot is empty" : "no encounter slot is empty";
            if (name == "get_Player" && call.Method.DeclaringType == typeof(Creature) && ValueText(call.Receiver) is { } playerWho)
                return expected ? $"{playerWho} is a player" : $"{playerWho} is not a player";
            if (BooleanGetter(call) is { } property && call.Method is MethodInfo boolGetter)
            {
                if (boolGetter.DeclaringType is { } ownerType && BackingField(ownerType, boolGetter) is { } bField)
                {
                    if (!IsGameplayField(bField))
                        return "";
                    return $"{property}={expected.ToString().ToLowerInvariant()}";
                }
                // For power/relic computed bool properties without a backing field (e.g., ShrinkPower.IsInfinite,
                // VelvetChoker.ShouldPreventCardPlay), inline their single-return body or fail closed.
                if (monster is null)
                {
                    if (_expandingGetters.Count < MaxGetterDepth && _expandingGetters.Add(boolGetter))
                    {
                        try
                        {
                            var gBody = Body(boolGetter);
                            if (!gBody.Incomplete && gBody.Returns is [{ Conditions.Length: 0, Value: var retVal }])
                                return Condition(new Test(retVal, expected));
                        }
                        finally
                        {
                            _expandingGetters.Remove(boolGetter);
                        }
                    }
                    return null;
                }
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

        // CardFactory.GetDistinctForCombat(...).FirstOrDefault() != null
        if (value is Call { Method.Name: "FirstOrDefault" } firstCall && UnwrapCardFactoryCall(firstCall) is not null)
            return expected ? "a matching card exists in the pool" : "no matching card exists in the pool";

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
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo predicate }] }, ..] }
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
            Input { Name: "oldOwner" } => "former owner",
            Input { Name: "originalTarget" } => "original target",
            Input { Name: "targets" } => PlayerTargetsLabel(),
            Input { Name: "target" } => "target",
            Input { Name: "dealer" } => "attacker",
            Input { Name: "cardSource" } => "source card",
            Input { Name: "cardPlay" } => "card play",
            Input { Name: "amount" } => "amount",
            Input { Name: "power" } => "changed power",
            Input { Name: "canonicalPower" } => "applied power",
            Input { Name: "card" } => "that card",
            Input { Name: "creature" } => "event target",
            Input { Name: "player" } => "player",
            Input { Name: "shuffler" } => "shuffler",
            Input { Name: "creator" } => "card creator",
            Input { Name: "applier" } => "applier",
            Input { Name: "giver" } => "power giver",
            Input { Name: "orb" } => "that orb",
            Input { Name: "gainer" } => "Star gainer",
            Input { Name: "spender" } => "Star spender",
            Input { Name: "forger" } => "forger",
            Input { Name: "source" } => "source",
            Input { Name: "retainedCards" } => "retained cards",
            Input { Name: "delta" } => "HP change",
            Constant { Data: null } => "none",
            Constant { Data: string text } => text,
            Field { Member: { Name: var capName, FieldType: var capType, DeclaringType: { } capDecl } }
                when capDecl.IsDefined(typeof(System.Runtime.CompilerServices.CompilerGeneratedAttribute), inherit: false)
                    && capName is "forger" or "player" or "card" or "target" or "amount" or "gainer" or "spender" or "shuffler"
                => ValueText(new Input(capName, capType)),
            Operation { Operator: "neg", Left: var negInner } when ValueText(negInner) is { } negText => $"−{negText}",
            Operation { Operator: "conv.r8" or "conv.r4" or "conv.i4", Left: var convInner } => ValueText(convInner),
            Operation { Operator: "castclass" or "isinst", Left: Input { Name: "card" } } => "that card",
            Operation { Operator: "castclass" or "isinst", Left: var inner, Right: Constant { Data: Type castType } }
                when typeof(CardModel).IsAssignableFrom(castType) && ValueText(inner) is { } innerCards
                => $"each {ModelLabel(castType)} in {(innerCards.StartsWith("that card in ", StringComparison.Ordinal) ? innerCards["that card in ".Length..] : innerCards)}",
            Operation { Operator: "castclass" or "isinst", Left: var innerMonster, Right: Constant { Data: Type mType } }
                when typeof(MonsterModel).IsAssignableFrom(mType) && ValueText(innerMonster) is { } whoMonster
                => whoMonster,
            Call { Method.Name: "get_Amount", Receiver: Input { Name: "this" } } => "stacks",
            Call { Method.Name: "get_AmountOnTurnStart", Receiver: Input { Name: "this" } } => "stacks present at turn start",
            Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card" } } } => "card player",
            Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card" } } => "card player",
            Call { Method.Name: "get_Owner", Method.DeclaringType: var pOwnerType, Receiver: Input { Name: "power" } }
                when pOwnerType == typeof(PowerModel) => "changed power's owner",
            Call { Method.Name: "get_Creature", Receiver: Input { Name: "this" } } => "self",
            Call { Method.Name: "get_Owner", Receiver: Input { Name: "this" } } => "owner",
            Call { Method.Name: "get_Creature", Receiver: var playerRec } when ValueText(playerRec) is { } playerWho => playerWho,
            Call { Method.Name: "GetResolved", Receiver: Call { Method.Name: "get_EnergyCost", Receiver: var costCard } }
                when ValueText(costCard) is { } costCardLabel => $"{Possessive(costCardLabel)} Energy cost",
            Call { Method.Name: "GetWithModifiers", Receiver: Call { Method.Name: "get_EnergyCost", Receiver: var modCostCard }, Arguments: var modCostArgs }
                when ValueText(modCostCard) is { } modCostCardLabel && DescribeCostModifierScope(modCostArgs.FirstOrDefault()) is { } modScope
                => $"{Possessive(modCostCardLabel)} {modScope} Energy cost",
            Call { Method.Name: "get_StarsSpent", Receiver: Call { Method.Name: "get_Resources" } } => "Stars spent on played card",
            Call { Method.Name: "get_EnergyValue", Receiver: Call { Method.Name: "get_Resources" } } => "played card's Energy value",
            Call { Method.Name: "get_EnergySpent", Receiver: Call { Method.Name: "get_Resources" } } => "Energy spent on played card",
            Call { Method.Name: "get_Attacker", Method.DeclaringType: var atkCmdType } when atkCmdType == typeof(MegaCrit.Sts2.Core.Commands.Builders.AttackCommand) => "attacker",
            Call { Method.Name: "get_Owner", Method.DeclaringType: var orbOwnerType, Receiver: Input { Name: "orb" } }
                when orbOwnerType == typeof(OrbModel) => "that orb's owner",
            Call { Method.Name: "get_Owner", Method.DeclaringType: var csOwnerType, Receiver: Input { Name: "cardSource" } }
                when csOwnerType == typeof(CardModel) => "source card's owner",
            Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_Orbs" } } => "owner's orb count",
            Call { Method.Name: "get_Item", Receiver: Call { Method.Name: "get_Orbs" }, Arguments: [Constant { Data: 0 }] } => "owner's first (next) orb",
            Call { Method.Name: "get_Orbs" } => "each orb in owner's orb queue",
            Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: nameof(Enumerable.GroupBy), Arguments: [Call { Method.Name: "get_Orbs" }, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo orbKeySelector }] }] }] }
                when IsOrbIdentityKeySelector(orbKeySelector)
                => "number of distinct orb types currently in owner's orb queue",
            Call { Method.Name: "FloorToInt", Method.DeclaringType.Name: "Mathf", Arguments: [var floorInner] }
                when ValueText(floorInner) is { } floorText => $"floor({floorText})",
            Call { Method.Name: "get_Count", Receiver: var countedList } when DescribeCardSelectionValue(countedList) is not null
                => "chosen card count",
            Call { Method.Name: "get_Count", Receiver: var poolList } when IsFilteredUnlockedCardPool(poolList)
                => "matching unlocked card pool size",
            Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "ToList", Arguments: [Call { Method.Name: nameof(Enumerable.Where) } filteredList] } }
                when FilteredSet(filteredList) is { } filteredLabel => $"count of {filteredLabel}",
            Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call selectCall } }
                when DescribeCardSelectionValue(selectCall) is { } selectedText => selectedText,
            Call { Method.Name: "Pow", Method.DeclaringType: var mathPow, Arguments: [var powBase, var powExp] }
                when mathPow == typeof(Math) && ValueText(powBase) is { } baseText && ValueText(powExp) is { } expText
                => $"{baseText}^{expText}",
            Call { Method.Name: "op_Explicit", Method.DeclaringType: var decExp, Arguments: [var decExpArg] }
                when decExp == typeof(decimal) => ValueText(decExpArg),
            Call { Method.Name: "get_Monster", Receiver: Input { Name: "creature" } } => "event target",
            Call { Method.Name: "get_Monster", Receiver: var ownerRec } when IsOwner(ownerRec) => "owner",
            Call { Method.Name: "get_Monster", Method.DeclaringType: var mCreature, Receiver: { } mRec }
                when mCreature == typeof(Creature) => ValueText(mRec),
            Call { Method.Name: "get_RoundNumber" } => "round number",
            Call { Method.Name: "get_TurnNumber", Receiver: Call { Method.Name: "get_PlayerCombatState",
                Receiver: Call { Method.Name: "get_Player", Receiver: var creature } } } when IsOwner(creature)
                => "owner's turn number",
            Call { Method.Name: "get_TurnNumber", Receiver: Call { Method.Name: "get_PlayerCombatState",
                Receiver: var ownerPlayer } } when IsOwner(ownerPlayer)
                => "owner's turn number",
            Call { Method.Name: "get_Count", Receiver: Call { Method.Name: "get_Players", Receiver: Call { Method.Name: "get_RunState" } } }
                => "number of players in the run",
            Call { Method.Name: "get_IsPlayer" } => "owner is a player",
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
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: Call { Method.Name: "Take" } taken } }
                when DescribedTakenCards(taken) is { } takenEach => $"each of {takenEach}",
            Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: { } collection } } => ValueText(collection),
            Call { Method.Name: "Take" } takenCall when DescribedTakenCards(takenCall) is { } takenCards => takenCards,
            Call { Method.Name: "NextItem", Method.DeclaringType.Name: "Rng", Arguments: [var nextItems] }
                when ValueText(nextItems) is { } itemsLabel => itemsLabel == "all hittable enemies"
                    ? "a random hittable enemy"
                    : nextItems is Call { Method: MethodInfo { ReturnType: var retT } } && typeof(IEnumerable<Creature>).IsAssignableFrom(retT)
                        ? $"a random target from {itemsLabel}"
                        : $"a random card from {itemsLabel}",
            Call { Method.Name: "get_Cards", Receiver: Call { Method.Name: "GetPile", Arguments: [Constant { Data: int rawPile }, var pileOwner] } }
                when Enum.IsDefined(typeof(PileType), rawPile) && ValueText(pileOwner) is { } pileWho
                => $"{Possessive(pileWho)} {PileLocationLabel((PileType)rawPile)}",
            Call { Method.Name: "ToList", Arguments: [var listInner] } => ValueText(listInner),
            Call { Method.Name: "Where" } filterCall => FilteredSet(filterCall),
            Call { Method.Name: "NewReadOnlySingleElementList" or ".ctor", Arguments: [var singleCard] }
                when ValueText(singleCard) is { } singleLabel => singleLabel,
            Call { Method.Name: "get_AllCards", Receiver: Call { Method.Name: "get_PlayerCombatState", Receiver: { } player } }
                when ValueText(player) is { } who => $"{Possessive(who)} combat cards",
            Call { Method.Name: "get_Applier", Receiver: Input { Name: "this" } } => "this power's applier",
            Call { Method.Name: "get_Owner", Receiver: Input { Name: "card" } } => "that card's player",
            Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call { Method: { Name: "Add", IsGenericMethod: true } added } } }
                when added.DeclaringType == typeof(CreatureCmd) => $"the summoned {ModelLabel(added.GetGenericArguments()[0])}",
            Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call { Method: { Name: "Add", IsGenericMethod: false } addedInst } } }
                when addedInst.DeclaringType == typeof(CreatureCmd) => "the summoned creature",
            Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_PetOwner", Receiver: Input { Name: "dealer" } } }
                => "attacker's owner",
            Call { Method.Name: "NextInt", Method.DeclaringType.Name: "Rng", Arguments: [var low, var high] }
                when ValueText(low) is { } least && Numeric(high) is { } bound => $"random {least}–{Number(bound - 1)}",
            Call { Method.Name: "ScaleHpForMultiplayer", Arguments: [var hp, ..] } when ValueText(hp) is { } amount
                => $"{amount} (scaled for multiplayer)",
            Call { Method.Name: "get_BaseValue" or "get_IntValue", Method.DeclaringType: var type, Receiver: var variable }
                when typeof(DynamicVar).IsAssignableFrom(type) && DynamicKey(variable) is { } key => $"counter {HumanizeLower(key)}",
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
            Operation { Operator: "rem", Left: var remL, Right: { } remR }
                when ValueText(remL) is { } rl && ValueText(remR) is { } rr => $"({rl} mod {rr})",
            Call { Method: MethodInfo relicGetter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                when _hookOwner is not null && typeof(RelicModel).IsAssignableFrom(_hookOwner)
                    && relicGetter.ReturnType == typeof(int) && relicGetter.Name.StartsWith("get_", StringComparison.Ordinal)
                    && relicGetter.DeclaringType == _hookOwner
                    && BackingField(_hookOwner, relicGetter) is { } relicBacking && IsGameplayField(relicBacking)
                => HumanizeLower(relicGetter.Name[4..]),
            Operation { Right: { } right } operation when Arithmetic(operation.Operator) is { } symbol
                => Formula(operation.Left, symbol, right),
            _ => null,
        };

        string? Formula(Value left, string symbol, Value right)
            => ValueText(left) is { } a && ValueText(right) is { } b ? $"({a} {symbol} {b})" : null;
    }

    private bool IsOrbIdentityKeySelector(MethodInfo keySelector)
    {
        if (keySelector.ContainsGenericParameters || Body(keySelector) is not { Incomplete: false, Returns: [{ Conditions.Length: 0, Value: var retVal }] })
            return false;
        var paramName = keySelector.GetParameters().FirstOrDefault()?.Name;
        return retVal is Call { Method.Name: "get_Id" or "GetType", Receiver: Input { Name: var rn } }
            && (paramName is null || rn == paramName);
    }

    private string? FilteredSet(Call filter)
    {
        if (filter.Arguments is not [var source, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo selector }] }])
            return null;
        if (source is Call { Method.Name: "ToList", Arguments: [var collection] })
            source = collection;
        if (source is Call { Method.Name: "get_Cards", Receiver: Call { Method.Name: "GetPile", Arguments: [Constant { Data: int rawPile }, var pileOwner] } }
            && Enum.IsDefined(typeof(PileType), rawPile) && ValueText(pileOwner) is { } pileWho
            && DescribePileCardFilter(selector) is { } pileFilter)
            return $"{pileFilter} in {Possessive(pileWho)} {PileLocationLabel((PileType)rawPile)}";
        if (source is Input { Name: "retainedCards" } && DescribePileCardFilter(selector) is { } retainedFilter)
            return $"{retainedFilter} among retained cards";
        if (source is Input { Name: "targets" } && Body(selector) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "get_IsAlive" }, Conditions.Length: 0 }] })
            return "living evoked targets";
        if (source is Call { Method.Name: "GetOpponentsOf", Arguments: [var oppFocus] } && IsOwner(oppFocus)
            && Body(selector) is { Incomplete: false, Returns: [{ Value: Call { Method.Name: "get_IsAlive" }, Conditions.Length: 0 }] })
            return "all living opponents";
        if (source is Call { Method.Name: "get_Players" }
            && IsOtherLivingPlayerPredicate(selector))
            return "each other living player";
        if (source is Call { Method.Name: "GetTeammatesOf", Arguments: [var tmFocus] } && IsOwner(tmFocus)
            && IsOtherLivingPlayerPredicate(selector))
            return "each other living player";
        if (Body(selector).Returns is not [{ Value: var test, Conditions.Length: 0 }])
            return null;
        if (source is Call { Method.Name: "GetTeammatesOf", Arguments: [var focus] } && IsOwner(focus)
            && test is Operation { Operator: "ceq", Right: Constant { Data: 0 },
                Left: Operation { Operator: "ceq", Left: Input, Right: var excluded } } && IsOwner(excluded))
            return "each other teammate";
        if (source is Call { Method.Name: "get_Enemies" }
            && test is Operation { Operator: "cgt.un", Left: Operation { Operator: "isinst", Right: Constant { Data: Type enemyType } }, Right: Constant { Data: null } }
            && typeof(MonsterModel).IsAssignableFrom(enemyType))
            return $"each {ModelLabel(enemyType)} enemy";
        if (source is not Call { Method.Name: "get_AllCards" })
            return null;
        if (test is Call { Method.Name: "get_IsUpgraded", Receiver: Input })
            return "each upgraded combat card";
        if (test is Operation { Operator: "ceq", Left: Call { Method.Name: "get_Type" }, Right: Constant { Data: int cardType } }
            && Enum.IsDefined(typeof(CardType), cardType))
            return $"each {(CardType)cardType} in combat";
        if (test is Operation { Operator: "cgt.un", Left: Operation { Operator: "isinst", Right: Constant { Data: Type type } }, Right: Constant { Data: null } }
            && typeof(AfflictionModel).IsAssignableFrom(type))
            return $"each combat card with {ModelLabel(type)}";
        return null;
    }

    private static Value? UnwrapPileTypeExpr(Value expr)
    {
        if (expr is Call { Method.Name: "GetValueOrDefault", Receiver: Call { Method: ConstructorInfo, Arguments: [var wrapped] } })
            expr = wrapped;
        if (expr is Call { Method.Name: "get_Type", Receiver: Call { Method.Name: "get_Pile", Receiver: var card } })
            return card;
        return null;
    }

    private static string PileLocationLabel(PileType pile) => pile switch
    {
        PileType.Hand => "hand",
        PileType.Play => "play area",
        _ => $"{pile.ToString().ToLowerInvariant()} pile",
    };

    private string? DescribeOrbValue(Value? orbArg) => orbArg switch
    {
        Call { Method.Name: "ToMutable", Receiver: var inner } => DescribeOrbValue(inner),
        Call { Method.Name: "GetRandomOrb", Method.DeclaringType: var t } when t == typeof(OrbModel) => "1 random orb",
        _ => null,
    };

    private bool IsConsumedCardSelection(Call selectCall, MethodInfo caller)
    {
        var selectKey = Key(selectCall);
        var body = Body(caller);
        return body.Calls.Any(s =>
            ((s.Call.Method.DeclaringType == typeof(CardCmd) && s.Call.Method.Name is "Discard" or "Exhaust" or "TransformToRandom")
                || (s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == "Add"))
            && s.Call.Arguments.Any(a => ContainsCallKey(a, selectKey)));
    }

    private static bool ContainsCallKey(Value? value, string targetKey, int depth = 0)
    {
        if (value is null || depth > 8)
            return false;
        if (value is Call call)
        {
            if (Key(call) == targetKey)
                return true;
            return ContainsCallKey(call.Receiver, targetKey, depth + 1)
                || call.Arguments.Any(a => ContainsCallKey(a, targetKey, depth + 1));
        }
        if (value is Operation op)
            return ContainsCallKey(op.Left, targetKey, depth + 1) || ContainsCallKey(op.Right, targetKey, depth + 1);
        return false;
    }

    private string? DescribeCardSelectionValue(Value? value)
    {
        while (value is Call { Method.Name: "ToList" or "ToArray", Arguments: [var listInner] })
            value = listInner;
        if (value is Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call innerSelect } })
            value = innerSelect;
        if (value is not Call { Method.DeclaringType: var decl, Method.Name: var name } selectCall || decl != typeof(CardSelectCmd))
            return null;
        // Fail closed when a filter predicate is passed; this reader only summarizes unfiltered selections.
        if (Argument(selectCall, "filter") is { } filterArg && filterArg is not Constant { Data: null })
            return null;
        var prefsArg = Argument(selectCall, "prefs");
        string? countText = null;
        if (prefsArg is Call { Method: ConstructorInfo { DeclaringType.Name: "CardSelectorPrefs" }, Arguments: var prefArgs })
        {
            if (prefArgs.Length == 2 && ValueText(prefArgs[1]) is { } exact)
            {
                countText = exact;
            }
            else if (prefArgs.Length >= 3 && ValueText(prefArgs[1]) is { } minText && ValueText(prefArgs[2]) is { } maxText)
            {
                countText = minText == maxText
                    ? minText
                    : minText == "0"
                        ? $"up to {maxText}"
                        : $"{minText}–{maxText}";
            }
        }
        if (countText is null)
            return null;
        var noun = countText == "1" ? "card" : "card(s)";
        var fewerSuffix = countText.StartsWith("up to ", StringComparison.Ordinal) ? "" : " (or all available if fewer)";
        if (name is "FromHand" or "FromHandForDiscard")
            return $"{countText} chosen {noun} from hand{fewerSuffix}";
        if (name == "FromCombatPile" && Argument(selectCall, "pile") is Call { Method.Name: "GetPile", Arguments: [Constant { Data: int rawPile }, var pileOwner] }
            && Enum.IsDefined(typeof(PileType), rawPile) && ValueText(pileOwner) is { } who)
            return $"{countText} chosen {noun} from {Possessive(who)} {PileLocationLabel((PileType)rawPile)}{fewerSuffix}";
        return null;
    }

    private static bool IsFilteredUnlockedCardPool(Value? value)
    {
        while (value is Call { Method.Name: "ToList" or "ToArray" or "Where", Arguments: [var inner, ..] })
            value = inner;
        return value is Call { Method.Name: "GetUnlockedCards" };
    }

    private bool IsOtherLivingPlayerPredicate(MethodInfo selector)
    {
        var body = Body(selector);
        if (body.Incomplete || body.Returns.Count == 0)
            return false;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var checksAlive = false;
        var checksNotSelf = false;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expected && expr is Input)
                continue;
            if (expected && expr is Call { Method.Name: "get_IsAlive" })
            {
                checksAlive = true;
                continue;
            }
            if (expected && expr is Call { Method.Name: "get_IsPlayer" })
                continue;
            if (IsEquality(new Test(expr, expected), false, out var neqL, out var neqR)
                && (IsOwnerPlayerOrCreature(neqL) || IsOwnerPlayerOrCreature(neqR)
                    || neqL is Field { Member.Name: "forger" } || neqR is Field { Member.Name: "forger" }))
            {
                checksNotSelf = true;
                continue;
            }
            return false;
        }
        return checksAlive && checksNotSelf;
    }

    private string? DescribedTakenCards(Call takeCall)
    {
        if (takeCall.Method.Name != nameof(Enumerable.Take) || takeCall.Arguments is not [var src, var countArg]
            || ValueText(countArg) is not { } countText)
            return null;
        // Handle prioritized shuffle-concat pattern (e.g. PowerCell: non-Innate 0-cost cards before Innate 0-cost cards).
        if (src is Call { Method.Name: nameof(Enumerable.Concat), Arguments: [var firstBranch, var secondBranch] }
            && DescribeShuffledBranch(firstBranch) is { } firstDesc
            && DescribeShuffledBranch(secondBranch) is { } secondDesc)
        {
            return $"up to {countText} random cards ({firstDesc}, then {secondDesc})";
        }
        var shuffled = false;
        if (src is Call { Method.Name: "UnstableShuffle" or "StableShuffle", Arguments: [var shuffledSrc, ..] })
        {
            shuffled = true;
            src = shuffledSrc;
        }
        if (src is Call { Method.Name: "ToList", Arguments: [var listSrc] })
            src = listSrc;
        if (ValueText(src) is not { } srcText)
            return null;
        return shuffled ? $"up to {countText} random {srcText}" : $"up to {countText} {srcText}";
    }

    private string? DescribeShuffledBranch(Value branch)
    {
        if (branch is Call { Method.Name: "StableShuffle" or "UnstableShuffle", Arguments: [var inner, ..] })
            branch = inner;
        if (branch is Call { Method.Name: "ToList", Arguments: [var listInner] })
            branch = listInner;
        if (branch is Call { Method.Name: nameof(Enumerable.Where), Arguments: [var source, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }
            && ValueText(source) is { } sourceText
            && DescribePileCardFilter(pred) is { } predText)
        {
            return $"{predText} from {sourceText}";
        }
        return ValueText(branch);
    }

    private string? DescribePileCardFilter(MethodInfo selector)
    {
        var body = Body(selector);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return null;

        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        CardType? cardType = null;
        var excludesUnplayable = false;
        var excludesXCost = false;
        string? costFilter = null;
        var requiredKeywords = new List<string>();
        var excludedKeywords = new List<string>();
        var requiredTags = new List<string>();

        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Call { Method.Name: "get_Type" }, Right: Constant { Data: int rawType } } typeOp
                && Enum.IsDefined(typeof(CardType), rawType))
            {
                var isEq = typeOp.Operator == "bne.un" ? !expected : expected;
                if (!isEq)
                    return null;
                cardType = (CardType)rawType;
                continue;
            }
            if (expr is Call { Method.Name: "get_CostsX" } && !expected)
            {
                excludesXCost = true;
                continue;
            }
            if (expr is Call { Method.Name: "GetWithModifiers", Receiver: Call { Method.Name: "get_EnergyCost" }, Arguments: var bareCostArgs })
            {
                if (costFilter is not null || DescribeCostModifierScope(bareCostArgs.FirstOrDefault()) is not { } scope)
                    return null;
                costFilter = expected ? $"{scope} Energy cost ≠ 0" : $"{scope} Energy cost = 0";
                continue;
            }
            if (expr is Operation { Operator: "ceq" or "beq" or "cgt" or "bgt" or "ble" or "clt" or "blt",
                Left: Call { Method.Name: "GetWithModifiers", Receiver: Call { Method.Name: "get_EnergyCost" }, Arguments: var opCostArgs },
                Right: Constant { Data: int costVal } } costOp)
            {
                if (costFilter is not null || DescribeCostModifierScope(opCostArgs.FirstOrDefault()) is not { } scope)
                    return null;
                if (costOp.Operator is "ceq" or "beq" && expected)
                {
                    costFilter = $"{scope} Energy cost = {costVal}";
                    continue;
                }
                if ((costOp.Operator is "cgt" or "bgt" && expected) || (costOp.Operator == "ble" && !expected))
                {
                    costFilter = $"{scope} Energy cost > {costVal}";
                    continue;
                }
                return null;
            }
            if (expr is Call { Method.Name: "Contains", Receiver: Call { Method.Name: "get_Keywords" }, Arguments: [Constant { Data: int rawKw }] }
                && Enum.IsDefined(typeof(CardKeyword), rawKw))
            {
                var kw = (CardKeyword)rawKw;
                if (!expected && kw == CardKeyword.Unplayable)
                {
                    excludesUnplayable = true;
                    continue;
                }
                if (expected)
                {
                    var label = KeywordLabel(kw);
                    if (!requiredKeywords.Contains(label, StringComparer.Ordinal))
                        requiredKeywords.Add(label);
                    continue;
                }
                var excludedLabel = $"non-{KeywordLabel(kw)}";
                if (!excludedKeywords.Contains(excludedLabel, StringComparer.Ordinal))
                    excludedKeywords.Add(excludedLabel);
                continue;
            }
            if (expr is Call { Method.Name: "Contains", Arguments: [Call { Method.Name: "get_Tags" }, Constant { Data: int rawTag }] }
                && Enum.IsDefined(typeof(CardTag), rawTag) && expected)
            {
                var tagLabel = $"{(CardTag)rawTag}-tagged";
                if (!requiredTags.Contains(tagLabel, StringComparer.Ordinal))
                    requiredTags.Add(tagLabel);
                continue;
            }
            return null;
        }

        var qualifiers = new List<string>();
        if (excludesUnplayable)
            qualifiers.Add($"non-{KeywordLabel(CardKeyword.Unplayable)}");
        if (excludesXCost)
            qualifiers.Add("non-Energy-X-cost");
        qualifiers.AddRange(requiredKeywords);
        qualifiers.AddRange(excludedKeywords);
        qualifiers.AddRange(requiredTags);
        if (cardType is { } kind)
            qualifiers.Add(kind.ToString());
        if (qualifiers.Count == 0 && costFilter is null)
            return null;
        var baseNoun = qualifiers.Count > 0 ? $"{string.Join(" ", qualifiers)} card(s)" : "card(s)";
        return costFilter is not null ? $"{baseNoun} with {costFilter}" : baseNoun;
    }

    private static string? DescribeCostModifierScope(Value? modifierArg)
        => modifierArg is Constant { Data: int rawMod }
            ? (CostModifiers)rawMod switch
            {
                CostModifiers.None => "base",
                CostModifiers.Local => "local",
                CostModifiers.Global => "global",
                CostModifiers.All => "modified",
                _ => null,
            }
            : null;

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
                    && monster is not null && getter.DeclaringType?.IsInstanceOfType(monster) == true:
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
                && TryGetCanonicalDynamicVars(strOwner) is { } strVars:
                return strVars.TryGetValue("StrengthPower", out var spv) ? spv.BaseValue
                    : strVars.TryGetValue("Strength", out var sv) ? sv.BaseValue
                    : null;
            case Call { Method.DeclaringType: var type, Method.Name: "get_BaseValue" or "get_IntValue", Receiver: var variable }
                when typeof(DynamicVar).IsAssignableFrom(type) && _hookOwner is { } dynOwner
                && DynamicKey(variable) is { } key && !MutableDynamicKeys(dynOwner).Contains(key):
                // Static parameters come from the canonical model; mutable runtime counters stay symbolic.
                return TryGetCanonicalDynamicVars(dynOwner)?.TryGetValue(key, out var parameter) == true
                    ? parameter.BaseValue
                    : TryGetCanonicalDynamicVars(dynOwner)?.TryGetValue(key + "Power", out var powerParam) == true
                        ? powerParam.BaseValue
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
        Call { Method.Name: "get_IsAutoPlay", Receiver: { } receiver } => IsStable(receiver),
        _ => false,
    };

    private static string TestKey(Test test) => IsStable(test.Value) ? Key(test.Value) : $"{test.Offset}:{Key(test.Value)}";

    internal static string KeywordLabel(CardKeyword keyword)
    {
        var stableName = keyword.ToString();
        try
        {
            var loc = new MegaCrit.Sts2.Core.Localization.LocString("card_keywords", MegaCrit.Sts2.Core.Helpers.StringHelper.Slugify(stableName) + ".title");
            if (loc.Exists())
            {
                var localized = loc.GetFormattedText().Trim();
                if (localized.Length > 0 && !string.Equals(localized, stableName, StringComparison.OrdinalIgnoreCase))
                    return $"{localized} ({stableName})";
                if (localized.Length > 0)
                    return localized;
            }
        }
        catch
        {
            // Fall back to stable enum identifier when card_keywords loc table is absent.
        }
        return stableName;
    }

    internal static string CallKey(Call call) => Key(call);
    internal static string ValueKey(Value? value) => Key(value);

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
        ArrayValue array => $"[{string.Join(",", array.Elements.Select(Key))}]",
        _ => "",
    };
}
