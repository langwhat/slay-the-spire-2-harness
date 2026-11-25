using System.Collections.Concurrent;
using System.Globalization;
using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Ascension;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.ValueProps;
using Sts2Mod.Core.Observation;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal static class EventMechanismReader
{
    private const BindingFlags DeclaredInstance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

    private static readonly PropertyInfo? OptionChosen = typeof(EventOption).GetProperty(
        "OnChosen",
        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
    private static readonly ConcurrentDictionary<bool, string?> ActCache = new();
    private static readonly ConcurrentDictionary<MethodInfo, NativeRuleBody> BodyCache = new();
    private static readonly ConcurrentDictionary<string, byte> LoggedGaps = new(StringComparer.Ordinal);

    private static void PushWarning(string message)
    {
        if (MegaCrit.Sts2.Core.Nodes.NGame.Instance is not null)
            Godot.GD.PushWarning(message);
    }

    private static NativeRuleBody ReadBody(MethodInfo method)
        => BodyCache.GetOrAdd(method, static m => NativeRuleBody.Read(m));

    internal static bool HasWearyTraveler(RunState run)
    {
        if (RunManager.Instance.IsInProgress)
            return RunManager.Instance.HasAscension(AscensionLevel.WearyTraveler);
        return HasWearyTraveler(run.AscensionLevel, run.GameMode);
    }

    internal static bool HasWearyTraveler(int ascensionLevel, GameMode gameMode)
    {
        if (gameMode == GameMode.Blitz)
        {
            for (var level = 1; level <= ascensionLevel; level++)
            {
                if (AscensionHelper.GetLevelForBlitz(level) == AscensionLevel.WearyTraveler)
                    return true;
            }
            return false;
        }
        return ascensionLevel >= (int)AscensionLevel.WearyTraveler;
    }

    public static string? ReadActTransition(RunState run)
    {
        try
        {
            if (run.CurrentActIndex + 1 >= run.Acts.Count)
                return null;

            var hasWearyTraveler = HasWearyTraveler(run);
            if (ActCache.TryGetValue(hasWearyTraveler, out var cached))
                return cached;

            var text = DescribeActTransition(hasWearyTraveler);
            ActCache[hasWearyTraveler] = text;
            return text;
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd("AncientEventModel.BeforeEventStarted", 0))
                PushWarning($"[sts2agent] Act transition mechanism unavailable: {error.Message}");
            return null;
        }
    }

    internal static string? DescribeActTransition(bool hasWearyTraveler)
    {
        var method = typeof(AncientEventModel).GetMethod("BeforeEventStarted", DeclaredInstance);
        if (method is null)
            return null;

        var body = NativeRuleBody.Read(method);
        if (body.Incomplete)
            return null;

        decimal? wearyFactor = null;
        var hasFullMissingHpBranch = false;
        foreach (var step in body.Calls)
        {
            if (step.Call.Method.DeclaringType != typeof(CreatureCmd) || step.Call.Method.Name != nameof(CreatureCmd.Heal))
                continue;
            if (step.Conditions.Any(c => c is { Expected: true, Value: Operation { Operator: "isinst" } }))
                continue;
            if (step.Call.Arguments.Length < 2)
                continue;

            var healArg = step.Call.Arguments[1];
            var checksWearyTrue = step.Conditions.Any(cond =>
                cond is { Expected: true, Value: Call { Method.Name: "HasAscension", Arguments: [Constant { Data: (int)AscensionLevel.WearyTraveler }] } });
            var checksWearyFalse = step.Conditions.Any(cond =>
                cond is { Expected: false, Value: Call { Method.Name: "HasAscension", Arguments: [Constant { Data: (int)AscensionLevel.WearyTraveler }] } });

            if (checksWearyTrue
                && healArg is Call { Method.Name: "op_Multiply", Arguments: [var baseMissing, var factorArg] }
                && IsMissingHpExpression(baseMissing)
                && ReadDecimalLiteral(factorArg) is { } factor)
            {
                wearyFactor = factor;
            }
            else if (checksWearyFalse && IsMissingHpExpression(healArg))
            {
                hasFullMissingHpBranch = true;
            }
        }

        if (wearyFactor is null || !hasFullMissingHpBranch)
            return null;

        return hasWearyTraveler
            ? $"Entering the next act's opening Ancient room after defeating this act's boss(es) heals floor({wearyFactor.Value.ToString("0.########", CultureInfo.InvariantCulture)} × missing HP)."
            : "Entering the next act's opening Ancient room after defeating this act's boss(es) heals 100% of missing HP.";
    }

    private static bool IsMissingHpExpression(Value expr) => expr switch
    {
        Call { Method.Name: "op_Implicit", Arguments: [var inner] } => IsMissingHpExpression(inner),
        Operation { Operator: "sub", Left: Call { Method.Name: "get_MaxHp" }, Right: Call { Method.Name: "get_CurrentHp" } } => true,
        _ => false,
    };

    public static string? ReadEvent(
        EventModel activeEvent,
        IReadOnlyList<(string Title, EventOption Option)> visibleOptions,
        bool isFakeMerchant)
        => ReadEventWithCompleteness(activeEvent, visibleOptions, isFakeMerchant).Text;

    public static (string? Text, MechanismCompleteness Completeness) ReadEventWithCompleteness(
        EventModel activeEvent,
        IReadOnlyList<(string Title, EventOption Option)> visibleOptions,
        bool isFakeMerchant)
    {
        var eventId = activeEvent.Id.ToString();
        try
        {
            // Method IL bodies are cached by MethodInfo; dynamic option values are always rendered
            // against the live activeEvent instance so stateful events (e.g. AbyssalBaths) stay fresh.
            var lines = new List<string>();
            var gapCount = 0;
            if (isFakeMerchant && DescribeFakeMerchant(activeEvent.GetType()) is { } merchantLine)
                lines.Add(merchantLine);

            if (!activeEvent.IsFinished)
            {
                foreach (var (title, option) in visibleOptions)
                {
                    if (option.IsProceed || OptionChosen?.GetValue(option) is not Delegate onChosen)
                        continue;
                    if (activeEvent is AncientEventModel && option.Relic is not null && IsSimpleAncientRelicPick(onChosen.Method))
                        continue;

                    var (summary, gap) = DescribeOption(activeEvent, option, onChosen.Method);
                    if (gap is not null)
                    {
                        gapCount++;
                        var gapKey = $"{eventId}:{onChosen.Method.Name}:{gap}";
                        if (LoggedGaps.TryAdd(gapKey, 0))
                            PushWarning($"[sts2agent] Unresolved native event option rule for {gapKey}");
                        continue;
                    }

                    if (summary is not null)
                        lines.Add($"Option [{title}]: {summary}");
                }
            }

            var text = lines.Count > 0 ? string.Join("\n", lines) : null;
            return (text, EnemyMechanismReader.ClassifyCompleteness(text, gapCount));
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd(eventId, 0))
                PushWarning($"[sts2agent] Event mechanism unavailable for {eventId}: {error.Message}");
            return (null, MechanismCompleteness.Unavailable);
        }
    }

    internal static (string? Text, IReadOnlySet<string> Gaps) DescribeEventTypeForProbe(Type eventType)
    {
        var gaps = new HashSet<string>(StringComparer.Ordinal);
        var lines = new List<string>();
        if (eventType.Name == "FakeMerchant" && DescribeFakeMerchant(eventType) is { } fm)
            lines.Add(fm);

        var canonical = ModelDb.GetByIdOrNull<EventModel>(ModelDb.GetId(eventType))
            ?? Activator.CreateInstance(eventType, nonPublic: true) as EventModel;
        if (canonical is null)
            return (null, gaps);

        foreach (var method in eventType.GetMethods(DeclaredInstance))
        {
            if (method.ReturnType != typeof(Task)
                || method.IsSpecialName
                || method.Name is "BeforeEventStarted" or "AfterEventStarted" or "Resume")
            {
                continue;
            }

            var parameters = method.GetParameters();
            if (parameters.Length > 1 || (parameters.Length == 1 && !typeof(AbstractModel).IsAssignableFrom(parameters[0].ParameterType)))
                continue;

            var (summary, gap) = DescribeOptionMethod(canonical, null, method);
            if (gap is not null)
                gaps.Add($"{method.Name}/{gap}");
            else if (summary is not null)
                lines.Add($"{method.Name}: {summary}");
        }

        return (lines.Count > 0 ? string.Join("\n", lines) : null, gaps);
    }

    private static string? DescribeFakeMerchant(Type eventType)
    {
        var method = eventType.GetMethod("FoulPotionThrown", DeclaredInstance);
        if (method is null)
            return null;

        var body = NativeRuleBody.Read(method);
        if (body.Incomplete)
            return null;

        var checksRelicStocked = body.Calls.Any(step =>
            step.Call.Method.Name == "EnterCombatWithoutExitingEvent"
            && step.Conditions.Any(cond => cond is { Expected: true, Value: Call { Method.Name: "get_IsStocked",
                Receiver: Call { Method.Name: "get_Current",
                    Receiver: Call { Method.Name: "GetEnumerator",
                        Receiver: Call { Method.Name: "get_RelicEntries" } } } } }));
        var checksOpenInventory = body.Calls.Any(step =>
            step.Call.Method.Name == "get_IsOpen"
            && step.Call.Method.DeclaringType?.Name == "NMerchantInventory");
        if (!checksRelicStocked)
            return null;

        var foulPotionType = method.GetParameters().FirstOrDefault()?.ParameterType;
        var potionLabel = foulPotionType is not null && typeof(PotionModel).IsAssignableFrom(foulPotionType)
            ? NativeRuleText.ModelLabel(foulPotionType)
            : "Foul Potion";
        var inventoryClause = checksOpenInventory
            ? " (either from the event screen or while its shop inventory is open)"
            : "";
        return $"Using {potionLabel} when at least one shop relic is still stocked{inventoryClause} immediately starts event combat.";
    }

    private static bool IsSimpleAncientRelicPick(MethodInfo method)
    {
        var body = NativeRuleBody.Read(method);
        if (body.Incomplete)
            return false;

        foreach (var step in body.Calls)
        {
            var declaring = step.Call.Method.DeclaringType;
            var name = step.Call.Method.Name;
            if (NativeRuleText.IsPresentationCall(step.Call.Method)
                || declaring?.Namespace is "System.Runtime.CompilerServices" or "System.Threading.Tasks"
                || name is "get_Owner" or "Obtain" or "Done" or "SetEventFinished" or "L10NLookup")
            {
                continue;
            }
            return false;
        }
        return true;
    }

    private static (string? Summary, string? Gap) DescribeOption(EventModel activeEvent, EventOption option, MethodInfo method)
    {
        if (method.DeclaringType != activeEvent.GetType() && method.DeclaringType?.DeclaringType == activeEvent.GetType())
        {
            var wrapper = NativeRuleBody.Read(method);
            var forwarded = wrapper.Calls
                .Select(step => step.Call.Method as MethodInfo)
                .FirstOrDefault(m => m is not null && m.DeclaringType == activeEvent.GetType() && m.ReturnType == typeof(Task));
            if (forwarded is not null)
                return DescribeOptionMethod(activeEvent, option, forwarded);
        }

        return DescribeOptionMethod(activeEvent, option, method);
    }

    internal static (string? Summary, string? Gap) DescribeOptionMethod(
        EventModel activeEvent,
        EventOption? option,
        MethodInfo method,
        int depth = 0,
        IReadOnlyDictionary<string, Value>? bindings = null)
    {
        var body = ReadBody(method);
        if (body.Incomplete)
            return (null, "incomplete");

        var steps = new List<string>();
        var hasSelection = false;
        var usesRandom = false;

        foreach (var group in body.Calls.GroupBy(s => (s.Epoch, s.Index))
            .OrderBy(g => (g.Key.Epoch, g.Max(s => s.Order), g.Key.Index)))
        {
            var distinctCalls = group.Select(s => s.Call).DistinctBy(NativeRuleText.CallKey).ToArray();
            if (distinctCalls.Length != 1)
                return (null, "ambiguous-call-group");

            var call = distinctCalls[0];
            var declaring = call.Method.DeclaringType;
            var name = call.Method.Name;
            var generic = call.Method.IsGenericMethod ? call.Method.GetGenericArguments()[0] : null;

            if (IsBenignEventHelperCall(call, activeEvent.GetType()))
                continue;

            if (typeof(EventModel).IsAssignableFrom(declaring)
                && name is "SetEventFinished" or "Done")
            {
                continue;
            }
            if (typeof(EventModel).IsAssignableFrom(declaring) && name == "SetEventState")
            {
                // Multi-stage state transitions that introduce new follow-up options (e.g. ColossalFlower.ReachDeeper)
                // cannot be summarized accurately from the immediate step alone. Only allow SetEventState when it
                // re-offers the current option and/or pure exit/proceed options.
                if (!IsSelfLoopOrExitStateTransition(body, method, activeEvent.GetType()))
                    return (null, "multi-stage-event-state");
                continue;
            }

            if (typeof(Delegate).IsAssignableFrom(declaring) || name == "Invoke")
                return (null, $"unresolved-delegate:{declaring?.Name}.{name}");

            if (declaring == activeEvent.GetType() && call.Method is MethodInfo pureHelper && pureHelper != method
                && pureHelper.ReturnType != typeof(Task) && pureHelper.ReturnType != typeof(void)
                && !pureHelper.ContainsGenericParameters && !pureHelper.IsGenericMethod)
            {
                var pureBody = ReadBody(pureHelper);
                if (!pureBody.Incomplete && pureBody.Writes.Count == 0 && pureBody.FieldSteps.Count == 0
                    && !pureBody.Calls.Any(s => IsGameplaySideEffectCall(s.Call)))
                {
                    continue;
                }
            }

            if (CallUsesRng(call) || group.Any(s => s.Conditions.Any(c => ValueUsesRng(c.Value))))
                usesRandom = true;

            var conditionalOnSelection = hasSelection && group.All(s => s.Conditions.Any(t => MentionsCardSelection(t.Value)));

            // Reject any gameplay call gated by a non-trivial condition (such as dish checks, RNG branches,
            // or dynamic state checks) unless every terminal execution path reaches this call unconditionally.
            var isUnconditional = body.Terminals.Count > 0 && body.Terminals.All(t => t.Visited.Contains(group.Key));
            if (!isUnconditional && !AllConditionsAreBenignGuards(group))
                return (null, $"conditional-effect:{declaring?.Name}.{name}");

            if (declaring == activeEvent.GetType() && call.Method is MethodInfo helper && helper != method)
            {
                if (helper.ContainsGenericParameters || helper.IsGenericMethod || helper.ReturnType != typeof(Task) || depth >= 1)
                    return (null, $"unresolved-helper:{helper.Name}");
                var helperParams = helper.GetParameters();
                Dictionary<string, Value>? childBindings = null;
                if (helperParams.Length > 0)
                {
                    childBindings = new Dictionary<string, Value>(StringComparer.Ordinal);
                    for (var i = 0; i < helperParams.Length && i < call.Arguments.Length; i++)
                    {
                        if (helperParams[i].Name is { } pName)
                        {
                            var boundVal = SubstituteBinding(call.Arguments[i], bindings);
                            if (boundVal is not Input { Name: var bn } || bn != pName)
                                childBindings[pName] = boundVal;
                        }
                    }
                }
                var (nested, nestedGap) = DescribeOptionMethod(activeEvent, option, helper, depth + 1, childBindings);
                if (nestedGap is not null)
                    return (null, nestedGap);
                if (nested is not null)
                    steps.Add(nested);
                continue;
            }

            if (declaring == typeof(CreatureCmd))
            {
                if (name == nameof(CreatureCmd.Damage))
                {
                    var amountParam = call.Method.GetParameters().FirstOrDefault(p => p.Name == "amount");
                    var propsParam = call.Method.GetParameters().FirstOrDefault(p => p.Name == "props");
                    var dmgVarParam = call.Method.GetParameters().FirstOrDefault(p => p.Name == "damageVar");
                    string? dmgText = null;
                    bool isUnblockable = true;
                    if (dmgVarParam is not null && dmgVarParam.Position < call.Arguments.Length)
                    {
                        var arg = call.Arguments[dmgVarParam.Position];
                        dmgText = ResolveEventAmount(activeEvent, arg);
                        if (ExtractEventDynamicKey(arg) is { } key
                            && activeEvent.DynamicVars.TryGetValue(key, out var dv)
                            && dv is MegaCrit.Sts2.Core.Localization.DynamicVars.DamageVar dmgVar)
                        {
                            isUnblockable = dmgVar.Props.HasFlag(ValueProp.Unblockable);
                        }
                    }
                    else
                    {
                        dmgText = amountParam is not null && amountParam.Position < call.Arguments.Length
                            ? ResolveEventAmount(activeEvent, call.Arguments[amountParam.Position], bindings)
                            : null;
                        if (propsParam is not null && propsParam.Position < call.Arguments.Length
                            && call.Arguments[propsParam.Position] is Constant { Data: int rawProps })
                        {
                            isUnblockable = ((ValueProp)rawProps).HasFlag(ValueProp.Unblockable);
                        }
                    }
                    steps.Add(dmgText is not null
                        ? $"deal {dmgText} {(isUnblockable ? "unblockable" : "blockable")} damage to owner"
                        : $"deal {(isUnblockable ? "unblockable" : "blockable")} damage to owner");
                    continue;
                }
                if (name == nameof(CreatureCmd.Heal))
                {
                    var healText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(1), bindings);
                    steps.Add(healText is not null ? $"heal {healText} HP" : "heal HP");
                    continue;
                }
                if (name == nameof(CreatureCmd.GainMaxHp))
                {
                    var maxHpText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(1), bindings);
                    steps.Add(maxHpText is not null ? $"gain {maxHpText} Max HP" : "gain Max HP");
                    continue;
                }
                if (name == nameof(CreatureCmd.LoseMaxHp))
                {
                    var maxHpText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(2), bindings);
                    steps.Add(maxHpText is not null ? $"lose {maxHpText} Max HP" : "lose Max HP");
                    continue;
                }
                return (null, $"unresolved-cmd:CreatureCmd.{name}");
            }

            if (declaring == typeof(PlayerCmd))
            {
                if (name == nameof(PlayerCmd.GainGold))
                {
                    var goldText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(0), bindings);
                    steps.Add(goldText is not null ? $"gain {goldText} Gold" : "gain Gold");
                    continue;
                }
                if (name == nameof(PlayerCmd.LoseGold))
                {
                    var goldText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(0), bindings);
                    steps.Add(goldText is not null ? $"lose {goldText} Gold" : "lose Gold");
                    continue;
                }
                return (null, $"unresolved-cmd:PlayerCmd.{name}");
            }

            if (declaring == typeof(PotionCmd))
            {
                if (name == nameof(PotionCmd.Discard))
                {
                    steps.Add("discard the chosen potion without triggering its on-use effect");
                    continue;
                }
                if (name == nameof(PotionCmd.TryToProcure))
                {
                    steps.Add("obtain a potion");
                    continue;
                }
                return (null, $"unresolved-cmd:PotionCmd.{name}");
            }

            if (declaring == typeof(CardSelectCmd))
            {
                hasSelection = true;
                var countText = ExtractSelectorCount(activeEvent, body, group.First(), call, bindings) ?? "eligible";
                var cardNoun = countText == "1" ? "card" : "card(s)";
                var hasFilter = HasSelectionFilterPredicate(call);
                var hasNonNullFilter = false;
                var redundantEnchantFilter = false;
                var filterDesc = hasFilter ? DescribeGenericDeckFilter(call, name == nameof(CardSelectCmd.FromDeckForEnchantment), out hasNonNullFilter, out redundantEnchantFilter) : null;
                if (filterDesc is null && hasNonNullFilter && !redundantEnchantFilter)
                    return (null, "unresolved-selection-filter");

                if (name == nameof(CardSelectCmd.FromDeckGeneric))
                {
                    var qualifiedNoun = filterDesc is not null ? $"{filterDesc} {cardNoun}" : cardNoun;
                    steps.Add($"select {countText} {qualifiedNoun} from deck");
                    continue;
                }
                if (name == nameof(CardSelectCmd.FromDeckForRemoval))
                {
                    var qualifiedNoun = filterDesc is not null
                        ? (filterDesc.Contains("removable", StringComparison.Ordinal) ? $"{filterDesc} {cardNoun}" : $"removable {filterDesc} {cardNoun}")
                        : $"removable {cardNoun}";
                    steps.Add($"select {countText} (or all remaining if fewer exist) {qualifiedNoun} from deck to remove");
                    continue;
                }
                if (name == nameof(CardSelectCmd.FromDeckForUpgrade))
                {
                    var qualifiedNoun = filterDesc is not null
                        ? (filterDesc.Contains("upgradable", StringComparison.Ordinal) ? $"{filterDesc} {cardNoun}" : $"upgradable {filterDesc} {cardNoun}")
                        : $"upgradable {cardNoun}";
                    steps.Add($"select {countText} {qualifiedNoun} from deck to upgrade");
                    continue;
                }
                if (name == nameof(CardSelectCmd.FromDeckForTransformation))
                {
                    var qualifiedNoun = filterDesc is not null
                        ? (filterDesc.Contains("transformable", StringComparison.Ordinal)
                            ? $"{filterDesc} {cardNoun}"
                            : $"transformable (removable non-Quest) {filterDesc} {cardNoun}")
                        : $"transformable (removable non-Quest) {cardNoun}";
                    steps.Add($"select {countText} {qualifiedNoun} from deck to transform");
                    continue;
                }
                if (name == nameof(CardSelectCmd.FromDeckForEnchantment))
                {
                    steps.Add(DescribeEnchantSelection(activeEvent, call, countText, filterDesc));
                    continue;
                }
                steps.Add($"select {countText} {cardNoun}");
                continue;
            }

            if (declaring == typeof(CardPileCmd))
            {
                if (name == nameof(CardPileCmd.RemoveFromDeck))
                {
                    if (!hasSelection)
                        steps.Add("remove card(s) from deck");
                    continue;
                }
                if (name == nameof(CardPileCmd.AddCurseToDeck) && generic is not null)
                {
                    var unconditional = hasSelection && !conditionalOnSelection;
                    var suffix = unconditional ? " unconditionally (even if 0 cards were eligible for selection)" : "";
                    steps.Add($"add 1 {NativeRuleText.ModelLabel(generic)} to deck{suffix}");
                    continue;
                }
                if (name == nameof(CardPileCmd.AddCursesToDeck))
                {
                    var curseType = ExtractSingleCardType(call.Arguments.FirstOrDefault()) ?? FindCardModelInBody(body);
                    var curseLabel = curseType is not null ? NativeRuleText.ModelLabel(curseType) : "curse card(s)";
                    var unconditional = hasSelection && !conditionalOnSelection;
                    var suffix = unconditional ? " unconditionally (even if 0 cards were eligible for selection)" : "";
                    steps.Add($"add {curseLabel} to deck{suffix}");
                    continue;
                }
                if (name == nameof(CardPileCmd.Add))
                {
                    var pileParam = call.Method.GetParameters().FirstOrDefault(p => p.Name is "pileType" or "targetPile");
                    var isDeck = pileParam is null
                        || (pileParam.Position < call.Arguments.Length
                            && call.Arguments[pileParam.Position] is Constant { Data: (int)PileType.Deck });
                    if (isDeck)
                    {
                        var addedArg = call.Arguments.FirstOrDefault();
                        if (MentionsCloneCard(addedArg))
                            steps.Add("duplicate deck card(s) directly into deck");
                        else if (ExtractSingleCardType(addedArg) is { } cardType)
                            steps.Add($"add {NativeRuleText.ModelLabel(cardType)} directly to deck");
                        else
                            steps.Add("add card(s) directly to deck");
                    }
                    continue;
                }
                return (null, $"unresolved-cmd:CardPileCmd.{name}");
            }

            if (declaring == typeof(CardCmd))
            {
                if (name == nameof(CardCmd.Upgrade))
                {
                    if (!hasSelection)
                        steps.Add(usesRandom ? "upgrade random upgradable card(s) in deck" : "upgrade card(s) in deck");
                    continue;
                }
                if (name == nameof(CardCmd.Downgrade))
                {
                    steps.Add(usesRandom ? "downgrade random upgraded card(s) in deck" : "downgrade card(s) in deck");
                    continue;
                }
                if (name == nameof(CardCmd.TransformToRandom))
                {
                    if (!hasSelection)
                        steps.Add("transform card(s) in deck into random card(s)");
                    continue;
                }
                if (name == nameof(CardCmd.TransformTo))
                {
                    var targetType = generic is not null && typeof(CardModel).IsAssignableFrom(generic)
                        ? generic
                        : ExtractSingleCardType(call.Arguments.ElementAtOrDefault(1));
                    if (targetType is not null)
                    {
                        steps.Add($"transform {(hasSelection ? "chosen card" : "card(s)")} into {NativeRuleText.ModelLabel(targetType)}");
                        continue;
                    }
                    return (null, "unresolved-transform-target");
                }
                if (name == "Enchant")
                {
                    if (!hasSelection)
                    {
                        var enchLabel = generic is not null && typeof(EnchantmentModel).IsAssignableFrom(generic)
                            ? $" with {NativeRuleText.ModelLabel(generic)}"
                            : "";
                        steps.Add($"enchant card(s) in deck{enchLabel}");
                    }
                    continue;
                }
                return (null, $"unresolved-cmd:CardCmd.{name}");
            }

            if (declaring == typeof(RelicCmd))
            {
                if (name == nameof(RelicCmd.Obtain))
                {
                    if (generic is not null && typeof(RelicModel).IsAssignableFrom(generic))
                        steps.Add($"obtain {NativeRuleText.ModelLabel(generic)} directly");
                    else
                        steps.Add("obtain a relic directly");
                    continue;
                }
                if (name == nameof(RelicCmd.Remove) || name == nameof(RelicCmd.Melt))
                {
                    steps.Add("lose/remove a relic");
                    continue;
                }
                return (null, $"unresolved-cmd:RelicCmd.{name}");
            }

            if (declaring == typeof(RewardsCmd) && name == nameof(RewardsCmd.OfferCustom))
            {
                var rewardDesc = DescribeCustomRewards(body);
                steps.Add(rewardDesc is not null
                    ? $"open an optional reward screen ({rewardDesc})"
                    : "open an optional reward screen");
                continue;
            }

            if (typeof(EventModel).IsAssignableFrom(declaring))
            {
                if (name is "SetEventFinished" or "Done" or "SetEventState")
                    continue;
                if (name == "SelectCardsToAddToDeckFromGrid")
                {
                    steps.Add("select card(s) from offered grid to add directly to deck");
                    continue;
                }
                if (name == "EnterCombatWithoutExitingEvent")
                {
                    var encType = generic is not null && typeof(EncounterModel).IsAssignableFrom(generic)
                        ? generic
                        : FindEncounterInCall(call);
                    steps.Add(encType is not null
                        ? $"enter event combat ({NativeRuleText.ModelLabel(encType)})"
                        : "enter event combat");
                    continue;
                }
                return (null, $"unresolved-event-call:{name}");
            }

            // Fail closed by default on any unrecognized non-benign call (including custom minigames or helpers).
            return (null, $"unresolved-call:{declaring?.Name}.{name}");
        }

        if (steps.Count == 0)
            return (null, null);

        var distinctSteps = new List<string>(steps.Count);
        foreach (var step in steps)
        {
            if (distinctSteps.Count == 0 || distinctSteps[^1] != step)
                distinctSteps.Add(step);
        }

        var prefix = usesRandom && !distinctSteps.Any(s => s.Contains("random", StringComparison.OrdinalIgnoreCase))
            ? "random outcome: "
            : "";
        return (prefix + string.Join(", then ", distinctSteps), null);
    }

    private static bool IsBenignEventHelperCall(Call call, Type eventType)
    {
        var declaring = call.Method.DeclaringType;
        var name = call.Method.Name;
        if (NativeRuleText.IsPresentationCall(call.Method)
            || declaring?.Namespace is "System.Runtime.CompilerServices" or "System.Threading.Tasks"
            || declaring == typeof(Enumerable)
            || declaring == typeof(Array)
            || declaring == typeof(LocString)
            || (declaring == typeof(string) && name is "Concat" or "Format" or "Join")
            || call.Method is ConstructorInfo
            || name.StartsWith("get_", StringComparison.Ordinal)
            || name.StartsWith("op_", StringComparison.Ordinal))
        {
            return true;
        }

        if (declaring == eventType && name.StartsWith("set_", StringComparison.Ordinal))
            return true;

        if (declaring?.Name is "DynamicVar" or "DamageVar" or "HealVar" or "MaxHpVar" or "GoldVar" or "StringVar" or "IntVar"
            && name is "set_BaseValue" or "set_StringValue" or "set_IntValue")
        {
            return true;
        }

        if (declaring?.Namespace == "System.Collections.Generic" && name is "Add" or "Remove" or "Contains" or "GetEnumerator" or "Clear")
            return true;

        if (name is "L10NLookup" or "ToList" or "ToArray" or "FirstOrDefault" or "First" or "MoveNext" or "GetEnumerator" or "Dispose"
            or "ToMutable" or "CreateCard" or "PreviewCardPileAdd" or "WithFlags" or "ForNonCombatWithUniformOdds"
            or "ForNonCombatWithDefaultOdds" or "Enchantment" or "Card" or "Relic" or "Potion" or "Encounter" or "ToStringAndClear"
            or "Preview" or "CustomScaledWait" or "CloneCard" or "CardPool" or "PotionPool" or "GetUnlockedPotions"
            or "GetUnlockedCards" or "CreateForReward" or "GetPile" or "GetCards" or "GetFormattedText" or "NextFloat"
            or "NextDouble" or "NextInt" or "NextBool" or "NextItem" or "ThatDoesDamage" or "AddChildSafely"
            or "Create" or "FromCardWithCardHoverTips" or "FromEnchantment" or "FromRelic" or "Static")
        {
            return true;
        }

        return false;
    }

    private static bool IsGameplaySideEffectCall(Call call)
    {
        var declaring = call.Method.DeclaringType;
        if (declaring is null)
            return true;
        if (declaring.Namespace == typeof(CreatureCmd).Namespace)
            return declaring.Name is not ("SfxCmd" or "VfxCmd" or "TalkCmd" or "ThinkCmd" or "Cmd");
        if (typeof(AbstractModel).IsAssignableFrom(declaring)
            || declaring.Name is "Player" or "Creature" or "CardPile" or "RunState" or "IRunState")
        {
            return call.Method is MethodInfo mi && (mi.ReturnType == typeof(void) || mi.ReturnType == typeof(Task));
        }
        return false;
    }

    private static bool AllConditionsAreBenignGuards(IGrouping<(int Epoch, int Index), Step> group)
    {
        foreach (var step in group)
        {
            foreach (var cond in step.Conditions)
            {
                if (!IsBenignEventGuard(cond))
                    return false;
            }
        }
        return true;
    }

    private static bool IsBenignEventGuard(Test cond)
    {
        if (MentionsCardSelection(cond.Value))
            return true;
        // Null/non-empty checks on rolled/selected candidates (e.g. `if (cardModel != null)`, `if (list.Count != 0)`, `if (cards.Any())`).
        return cond.Value switch
        {
            Call { Method.Name: nameof(Enumerable.Any) or nameof(Enumerable.Count) or "get_Count" } => true,
            Operation { Operator: "ceq" or "cgt" or "cgt.un" or "clt" or "clt.un" or "beq" or "bne.un",
                Left: Call { Method.Name: "get_Count" or nameof(Enumerable.Count) or nameof(Enumerable.Any) },
                Right: Constant { Data: 0 } } => true,
            Call { Method.Name: "NextItem" or "FirstOrDefault" or "CreateCard" or "ToMutable" } => true,
            Operation { Operator: "ceq" or "beq" or "bne.un",
                Left: Call { Method.Name: "NextItem" or "FirstOrDefault" or "CreateCard" or "ToMutable" },
                Right: Constant { Data: null } } => true,
            _ => false,
        };
    }

    private static bool MentionsCloneCard(Value? value) => value switch
    {
        null => false,
        Call call => call.Method.Name == "CloneCard"
            || MentionsCloneCard(call.Receiver)
            || call.Arguments.Any(MentionsCloneCard),
        Operation op => MentionsCloneCard(op.Left) || MentionsCloneCard(op.Right),
        _ => false,
    };

    private static Type? FindCardModelInBody(NativeRuleBody body)
    {
        foreach (var step in body.Calls)
        {
            if (step.Call.Method is { Name: "Card", IsGenericMethod: true } m && m.DeclaringType == typeof(ModelDb))
                return m.GetGenericArguments()[0];
        }
        return null;
    }

    private static Type? FindEncounterInCall(Call call)
    {
        foreach (var arg in call.Arguments)
        {
            if (arg is Call { Method: { Name: "Encounter", IsGenericMethod: true } m } && m.DeclaringType == typeof(ModelDb))
                return m.GetGenericArguments()[0];
        }
        return null;
    }

    private static string? DescribeCustomRewards(NativeRuleBody body)
    {
        var kinds = new List<string>();
        foreach (var step in body.Calls)
        {
            var dt = step.Call.Method.DeclaringType?.Name;
            if (dt == "CardReward" && !kinds.Contains("card reward"))
                kinds.Add("card reward");
            else if (dt == "RelicReward" && !kinds.Contains("relic reward"))
                kinds.Add("relic reward");
            else if (dt == "GoldReward" && !kinds.Contains("Gold reward"))
                kinds.Add("Gold reward");
            else if (dt == "PotionReward")
            {
                var potionType = body.Calls
                    .Where(s => s.Call.Method is { Name: "Potion", IsGenericMethod: true } pm && pm.DeclaringType == typeof(ModelDb))
                    .Select(s => s.Call.Method.GetGenericArguments()[0])
                    .FirstOrDefault();
                var label = potionType is not null ? $"{NativeRuleText.ModelLabel(potionType)} reward" : "potion reward";
                if (!kinds.Contains(label))
                    kinds.Add(label);
            }
        }
        return kinds.Count > 0 ? string.Join(", ", kinds) : null;
    }

    private static bool IsSelfLoopOrExitStateTransition(NativeRuleBody body, MethodInfo currentMethod, Type eventType)
    {
        var optionCtors = body.Calls
            .Where(s => s.Call.Method is ConstructorInfo { DeclaringType: var dt } && dt == typeof(EventOption))
            .ToArray();
        if (optionCtors.Length == 0)
            return false;

        foreach (var ctor in optionCtors)
        {
            var delegateArg = ctor.Call.Arguments.ElementAtOrDefault(1);
            if (delegateArg is Constant { Data: null })
                continue;
            if (delegateArg is not Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo targetMethod }] })
                return false;
            if (targetMethod == currentMethod)
                continue;
            // Allow follow-up options that are pure exit/finish (e.g. ExitBaths) or whose handler is the current method.
            if (targetMethod.DeclaringType == eventType && IsPureFinishEventMethod(targetMethod))
                continue;
            return false;
        }
        return true;
    }

    private static bool IsPureFinishEventMethod(MethodInfo method)
    {
        var b = ReadBody(method);
        if (b.Incomplete)
            return false;
        return b.Calls.All(s =>
            IsBenignEventHelperCall(s.Call, method.DeclaringType ?? typeof(EventModel))
            || (typeof(EventModel).IsAssignableFrom(s.Call.Method.DeclaringType)
                && s.Call.Method.Name is "SetEventFinished" or "Done"));
    }

    private static string DescribeEnchantSelection(EventModel activeEvent, Call call, string countText, string? filterDesc)
    {
        var enchantArg = call.Arguments.ElementAtOrDefault(1);
        var enchantType = enchantArg switch
        {
            Call { Method: { Name: "Enchantment", IsGenericMethod: true } m } => m.GetGenericArguments()[0],
            _ => null,
        };
        var label = enchantType is not null ? NativeRuleText.ModelLabel(enchantType) : "the offered enchantment";
        var amountParam = call.Method.GetParameters().FirstOrDefault(p => p.Name == "amount");
        var amountText = amountParam is not null && amountParam.Position < call.Arguments.Length
            ? ResolveEventAmount(activeEvent, call.Arguments[amountParam.Position])
            : null;
        var showAmount = enchantType is not null
            && ModelDb.GetByIdOrNull<EnchantmentModel>(ModelDb.GetId(enchantType)) is { ShowAmount: true };
        var amountClause = amountText is not null && (showAmount || amountText != "1")
            ? $" ({amountText})"
            : "";
        var qualifier = filterDesc is not null ? $"{filterDesc} " : "eligible ";
        var prefix = countText == "eligible" ? $"{qualifier}deck card(s)" : $"{countText} {qualifier}deck card(s)";
        return $"select {prefix} to enchant with {label}{amountClause}";
    }

    private static bool CallUsesRng(Call call)
        => ValueUsesRng(call.Receiver) || call.Arguments.Any(ValueUsesRng);

    private static bool ValueUsesRng(Value? value) => value switch
    {
        null => false,
        Call { Method.DeclaringType.Name: "Rng", Method.Name: "NextFloat" or "NextDouble" or "NextInt" or "NextBool" or "NextItem" } => true,
        Call call => ValueUsesRng(call.Receiver) || call.Arguments.Any(ValueUsesRng),
        Operation op => ValueUsesRng(op.Left) || ValueUsesRng(op.Right),
        Field field => ValueUsesRng(field.Receiver),
        _ => false,
    };

    private static string? ExtractSelectorCount(
        EventModel activeEvent,
        NativeRuleBody body,
        Step selectStep,
        Call call,
        IReadOnlyDictionary<string, Value>? bindings = null)
    {
        var parameters = call.Method.GetParameters();
        var prefsParam = parameters.FirstOrDefault(p => p.ParameterType.Name == "CardSelectorPrefs");
        if (prefsParam is not null && prefsParam.Position < call.Arguments.Length)
        {
            var prefsArg = call.Arguments[prefsParam.Position];
            if (prefsArg is Call { Method: ConstructorInfo } directCtor
                && ReadPrefsCount(activeEvent, directCtor, bindings) is { } directCount)
            {
                return directCount;
            }
            // In async state machines, struct CardSelectorPrefs may be initialized via `call .ctor` on the same receiver.
            var prefsKey = NativeRuleText.ValueKey(prefsArg);
            var precedingCtor = body.Calls
                .Where(s => s.Call.Method is ConstructorInfo { DeclaringType.Name: "CardSelectorPrefs" }
                    && (s.Epoch < selectStep.Epoch || (s.Epoch == selectStep.Epoch && s.Order < selectStep.Order))
                    && s.Call.Receiver is not null
                    && NativeRuleText.ValueKey(s.Call.Receiver) == prefsKey)
                .OrderByDescending(s => (s.Epoch, s.Order))
                .FirstOrDefault();
            if (precedingCtor is not null && ReadPrefsCount(activeEvent, precedingCtor.Call, bindings) is { } ctorCount)
                return ctorCount;

            return null;
        }

        // Only inspect explicit count parameters when no CardSelectorPrefs is present (never enchantment `amount`).
        var countParam = parameters.FirstOrDefault(p => p.ParameterType == typeof(int) && p.Name is "count" or "numCards");
        if (countParam is not null && countParam.Position < call.Arguments.Length)
            return ResolveEventAmount(activeEvent, call.Arguments[countParam.Position], bindings);

        return null;
    }

    private static string? ReadPrefsCount(EventModel activeEvent, Call prefsCtor, IReadOnlyDictionary<string, Value>? bindings)
    {
        if (prefsCtor.Arguments is [_, var singleCount]
            && ResolveEventAmount(activeEvent, singleCount, bindings) is { } singleText)
        {
            return singleText;
        }
        if (prefsCtor.Arguments is [_, var minArg, var maxArg])
        {
            var minText = ResolveEventAmount(activeEvent, minArg, bindings);
            var maxText = ResolveEventAmount(activeEvent, maxArg, bindings);
            if (minText is not null && maxText is not null)
            {
                if (minText == maxText)
                    return minText;
                if (int.TryParse(maxText, CultureInfo.InvariantCulture, out var maxNum) && maxNum > 99)
                    return $"{minText}+";
                return $"{minText}–{maxText}";
            }
        }
        return null;
    }

    private static bool HasSelectionFilterPredicate(Call call)
        => call.Method.GetParameters().Any(p => p.ParameterType == typeof(Func<CardModel, bool>));

    private static bool HasNonNullFilterArgument(Call call)
    {
        var filterParam = call.Method.GetParameters().FirstOrDefault(p => p.ParameterType == typeof(Func<CardModel, bool>));
        if (filterParam is null || filterParam.Position >= call.Arguments.Length)
            return false;
        return call.Arguments[filterParam.Position] is not Constant { Data: null };
    }

    private static string? DescribeGenericDeckFilter(
        Call call,
        bool isEnchantmentSelection,
        out bool hasNonNullFilter,
        out bool redundantEnchantFilter)
    {
        hasNonNullFilter = false;
        redundantEnchantFilter = false;
        var filterParam = call.Method.GetParameters().FirstOrDefault(p => p.ParameterType == typeof(Func<CardModel, bool>));
        if (filterParam is null || filterParam.Position >= call.Arguments.Length)
            return null;
        if (call.Arguments[filterParam.Position] is Constant { Data: null })
            return null;
        hasNonNullFilter = true;
        if (call.Arguments[filterParam.Position] is not Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo predMethod }] })
            return null;

        if (isEnchantmentSelection && IsRedundantEnchantmentCanEnchantFilter(call, predMethod))
        {
            redundantEnchantFilter = true;
            return null;
        }

        return DescribeCardFilterPredicate(predMethod, boundTag: null, depth: 0);
    }

    private static bool IsRedundantEnchantmentCanEnchantFilter(Call selectCall, MethodInfo predMethod)
    {
        var enchParam = selectCall.Method.GetParameters().FirstOrDefault(p => typeof(EnchantmentModel).IsAssignableFrom(p.ParameterType));
        if (enchParam is null || enchParam.Position >= selectCall.Arguments.Length)
            return false;
        var targetEnchantment = ExtractEnchantmentType(selectCall.Arguments[enchParam.Position]);
        if (targetEnchantment is null)
            return false;

        var candidateParam = predMethod.GetParameters().FirstOrDefault(p => typeof(CardModel).IsAssignableFrom(p.ParameterType));
        if (candidateParam?.Name is not { } candidateName)
            return false;

        var body = ReadBody(predMethod);
        if (body.Incomplete || body.Returns is not [{ Conditions.Length: 0, Value: Call { Method.Name: "CanEnchant", Receiver: var rec, Arguments: [Input { Name: var argName }] } }]
            || argName != candidateName)
            return false;
        return ExtractEnchantmentType(rec) == targetEnchantment;
    }

    private static Type? ExtractEnchantmentType(Value? value) => value switch
    {
        Call { Method.Name: "ToMutable", Receiver: var inner } => ExtractEnchantmentType(inner),
        Call { Method: { Name: "Enchantment", IsGenericMethod: true } lookup } when lookup.DeclaringType == typeof(ModelDb)
            => lookup.GetGenericArguments()[0],
        _ => null,
    };

    private static string? DescribeCardFilterPredicate(MethodInfo predMethod, CardTag? boundTag, int depth)
    {
        if (depth > 1)
            return null;
        var body = ReadBody(predMethod);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        // If the predicate simply delegates to a helper (e.g. `(c) => IsValid(CardTag.Strike, c)`), inline it once.
        if (body.Returns is [{ Conditions.Length: 0, Value: Call { Method: MethodInfo helper } helperCall }]
            && helper.ReturnType == typeof(bool) && helper != predMethod
            && helper.DeclaringType is { } dt && typeof(EventModel).IsAssignableFrom(dt))
        {
            CardTag? passedTag = boundTag;
            foreach (var arg in helperCall.Arguments)
            {
                if (arg is Constant { Data: int rawTag } && Enum.IsDefined(typeof(CardTag), rawTag))
                    passedTag = (CardTag)rawTag;
            }
            return DescribeCardFilterPredicate(helper, passedTag, depth + 1);
        }

        var positiveReturns = body.Returns.Where(r => r.Value is not Constant { Data: 0 or false }).ToArray();
        if (positiveReturns.Length == 0)
            return null;

        var qualifiers = new List<string>();
        var rarities = new List<CardRarity>();
        var tags = new List<CardTag>();
        var requiresTransformable = false;
        var requiresRemovable = false;
        var requiresUpgradable = false;

        foreach (var ret in positiveReturns)
        {
            var pathTests = new List<Test>(ret.Conditions);
            if (ret.Value is not Constant { Data: 1 or true })
                pathTests.Add(new Test(ret.Value, true));

            foreach (var rawTest in pathTests)
            {
                var expr = rawTest.Value;
                var expected = rawTest.Expected;
                while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
                {
                    expr = inner;
                    expected = !expected;
                }

                if (expr is Call { Method.Name: "get_IsTransformable" })
                {
                    if (!expected)
                        return null;
                    requiresTransformable = true;
                }
                else if (expr is Call { Method.Name: "get_IsRemovable" })
                {
                    if (!expected)
                        return null;
                    requiresRemovable = true;
                }
                else if (expr is Call { Method.Name: "get_IsUpgradable" })
                {
                    if (!expected)
                        return null;
                    requiresUpgradable = true;
                }
                else if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Call { Method.Name: "get_Rarity" }, Right: Constant { Data: int rawRarity } } rarityOp
                    && Enum.IsDefined(typeof(CardRarity), rawRarity))
                {
                    var isEquality = rarityOp.Operator == "bne.un" ? !expected : expected;
                    if (!isEquality)
                        return null;
                    if (!rarities.Contains((CardRarity)rawRarity))
                        rarities.Add((CardRarity)rawRarity);
                }
                else if (expr is Call { Method.Name: "Contains", Arguments: [var tagArg] } containsCall
                    && (containsCall.Receiver is Call { Method.Name: "get_Tags" }
                        || containsCall.Arguments.Any(a => a is Call { Method.Name: "get_Tags" })))
                {
                    if (!expected)
                        return null;
                    var tag = tagArg is Constant { Data: int rawTag } && Enum.IsDefined(typeof(CardTag), rawTag)
                        ? (CardTag?)rawTag
                        : boundTag;
                    if (tag is null)
                        return null;
                    if (!tags.Contains(tag.Value))
                        tags.Add(tag.Value);
                }
                else if (expr is Call { Method.Name: "Contains", Arguments: [Call { Method.Name: "get_Tags" }, var extTagArg] })
                {
                    if (!expected)
                        return null;
                    var tag = extTagArg is Constant { Data: int rawTag } && Enum.IsDefined(typeof(CardTag), rawTag)
                        ? (CardTag?)rawTag
                        : boundTag;
                    if (tag is null)
                        return null;
                    if (!tags.Contains(tag.Value))
                        tags.Add(tag.Value);
                }
                else if (expr is Input { Type: var inputType } && typeof(CardModel).IsAssignableFrom(inputType) && expected)
                {
                    // Null check `card != null`.
                    continue;
                }
                else
                {
                    return null;
                }
            }
        }

        if (requiresTransformable)
            qualifiers.Add("transformable");
        else if (requiresRemovable)
            qualifiers.Add("removable");
        if (requiresUpgradable)
            qualifiers.Add("upgradable");
        if (rarities.Count > 0)
            qualifiers.Add(string.Join("/", rarities));
        if (tags.Count > 0)
            qualifiers.Add(string.Join("/", tags));

        return qualifiers.Count > 0 ? string.Join(" ", qualifiers) : null;
    }

    private static Type? ExtractSingleCardType(Value? value) => value switch
    {
        Call { Method.Name: ".ctor", Arguments: [var single] } => ExtractSingleCardType(single),
        Call { Method: { Name: "Card", IsGenericMethod: true } lookup } when lookup.DeclaringType == typeof(ModelDb)
            => lookup.GetGenericArguments()[0],
        Call { Method: { Name: "CreateCard", IsGenericMethod: true } create }
            when typeof(CardModel).IsAssignableFrom(create.GetGenericArguments()[0])
            => create.GetGenericArguments()[0],
        _ => null,
    };

    private static bool MentionsCardSelection(Value value) => value switch
    {
        Call call => call.Method.DeclaringType == typeof(CardSelectCmd)
            || (call.Receiver is not null && MentionsCardSelection(call.Receiver))
            || call.Arguments.Any(MentionsCardSelection),
        Operation op => MentionsCardSelection(op.Left) || (op.Right is not null && MentionsCardSelection(op.Right)),
        _ => false,
    };

    private static Value SubstituteBinding(Value value, IReadOnlyDictionary<string, Value>? bindings)
    {
        if (bindings is null)
            return value;
        if (value is Input { Name: var name } && bindings.TryGetValue(name, out var bound))
            return bound;
        return value;
    }

    private static string? ResolveEventAmount(EventModel activeEvent, Value? expr, IReadOnlyDictionary<string, Value>? bindings = null)
    {
        var appliedBinding = false;
        for (var guard = 0; guard < 16; guard++)
        {
            if (!appliedBinding && expr is Input { Name: var inputName } && bindings is not null
                && bindings.TryGetValue(inputName, out var boundExpr))
            {
                expr = boundExpr;
                appliedBinding = true;
            }
            else if (expr is Call { Method.Name: "op_Implicit", Arguments: [var implicitInner] })
                expr = implicitInner;
            else if (expr is Operation { Operator: "castclass" or "isinst", Left: var castInner })
                expr = castInner;
            else
                break;
        }
        if (expr is null)
            return null;

        if (expr is Constant { Data: int literal })
            return literal.ToString(CultureInfo.InvariantCulture);
        if (ReadDecimalLiteral(expr) is { } dec)
            return dec.ToString("0.########", CultureInfo.InvariantCulture);

        try
        {
            if (ExtractEventDynamicKey(expr) is { } key
                && activeEvent.DynamicVars.TryGetValue(key, out var dynVar)
                && dynVar.BaseValue > 0m)
            {
                return dynVar.BaseValue.ToString("0.########", CultureInfo.InvariantCulture);
            }

            if (expr is Call { Method.Name: "get_BaseValue" or "get_IntValue", Receiver: var rec }
                && ExtractEventDynamicKey(rec) is { } recKey
                && activeEvent.DynamicVars.TryGetValue(recKey, out var recDyn)
                && recDyn.BaseValue > 0m)
            {
                return recDyn.BaseValue.ToString("0.########", CultureInfo.InvariantCulture);
            }
        }
        catch
        {
            // Canonical probe instances fall back to null when DynamicVars require live locale state.
        }

        return null;
    }

    private static string? ExtractEventDynamicKey(Value? expr) => expr switch
    {
        Operation { Operator: "castclass" or "isinst", Left: var inner } => ExtractEventDynamicKey(inner),
        Call { Method.Name: "get_Item", Arguments: [Constant { Data: string key }] } => key,
        Call { Method.Name: "get_Heal" } => "Heal",
        Call { Method.Name: "get_Damage" } => "Damage",
        Call { Method.Name: "get_HpLoss" } => "HpLoss",
        Call { Method.Name: "get_MaxHp" } => "MaxHp",
        Call { Method.Name: "get_Gold" } => "Gold",
        Call { Method.Name: "get_Cards" } => "Cards",
        _ => null,
    };

    private static decimal? ReadDecimalLiteral(Value value) => value switch
    {
        Constant { Data: int i } => i,
        Constant { Data: decimal d } => d,
        Call { Method: ConstructorInfo { DeclaringType: var t }, Arguments: [Constant { Data: int lo }, Constant { Data: int mid },
            Constant { Data: int hi }, Constant { Data: int neg }, Constant { Data: int scale }] } when t == typeof(decimal)
            => new decimal(lo, mid, hi, neg != 0, (byte)scale),
        Call { Method: ConstructorInfo { DeclaringType: var t }, Arguments: [Constant { Data: int val }] } when t == typeof(decimal)
            => val,
        _ => null,
    };
}
