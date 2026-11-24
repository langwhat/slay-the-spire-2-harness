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
    {
        var eventId = activeEvent.Id.ToString();
        try
        {
            // Method IL bodies are cached by MethodInfo; dynamic option values are always rendered
            // against the live activeEvent instance so stateful events (e.g. AbyssalBaths) stay fresh.
            var lines = new List<string>();
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
                        var gapKey = $"{eventId}:{onChosen.Method.Name}:{gap}";
                        if (LoggedGaps.TryAdd(gapKey, 0))
                            PushWarning($"[sts2agent] Unresolved native event option rule for {gapKey}");
                        continue;
                    }

                    if (summary is not null)
                        lines.Add($"Option [{title}]: {summary}");
                }
            }

            return lines.Count > 0 ? string.Join("\n", lines) : null;
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd(eventId, 0))
                PushWarning($"[sts2agent] Event mechanism unavailable for {eventId}: {error.Message}");
            return null;
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
        int depth = 0)
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
                && name is "SetEventFinished" or "Done" or "SetEventState")
            {
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
                var (nested, nestedGap) = DescribeOptionMethod(activeEvent, option, helper, depth + 1);
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
                            ? ResolveEventAmount(activeEvent, call.Arguments[amountParam.Position])
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
                    var healText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(1));
                    steps.Add(healText is not null ? $"heal {healText} HP" : "heal HP");
                    continue;
                }
                if (name == nameof(CreatureCmd.GainMaxHp))
                {
                    var maxHpText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(1));
                    steps.Add(maxHpText is not null ? $"gain {maxHpText} Max HP" : "gain Max HP");
                    continue;
                }
                if (name == nameof(CreatureCmd.LoseMaxHp))
                {
                    var maxHpText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(2));
                    steps.Add(maxHpText is not null ? $"lose {maxHpText} Max HP" : "lose Max HP");
                    continue;
                }
                return (null, $"unresolved-cmd:CreatureCmd.{name}");
            }

            if (declaring == typeof(PlayerCmd))
            {
                if (name == nameof(PlayerCmd.GainGold))
                {
                    var goldText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(0));
                    steps.Add(goldText is not null ? $"gain {goldText} Gold" : "gain Gold");
                    continue;
                }
                if (name == nameof(PlayerCmd.LoseGold))
                {
                    var goldText = ResolveEventAmount(activeEvent, call.Arguments.ElementAtOrDefault(0));
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
                var countText = ExtractSelectorCount(activeEvent, body, group.First(), call) ?? "eligible";
                var cardNoun = countText == "1" ? "card" : "card(s)";
                if (name == nameof(CardSelectCmd.FromDeckGeneric))
                {
                    var filterDesc = DescribeGenericDeckFilter(call);
                    if (filterDesc is null && HasSelectionFilterPredicate(call))
                        return (null, "unresolved-selection-filter");
                    var qualifiedNoun = filterDesc is not null ? $"{filterDesc} {cardNoun}" : cardNoun;
                    steps.Add($"select {countText} {qualifiedNoun} from deck");
                    continue;
                }
                steps.Add(name switch
                {
                    nameof(CardSelectCmd.FromDeckForRemoval) => $"select {countText} removable {cardNoun} from deck to remove",
                    nameof(CardSelectCmd.FromDeckForUpgrade) => $"select {countText} upgradable {cardNoun} from deck to upgrade",
                    nameof(CardSelectCmd.FromDeckForTransformation) => $"select {countText} removable {cardNoun} from deck to transform",
                    nameof(CardSelectCmd.FromDeckForEnchantment) => DescribeEnchantSelection(call, countText),
                    _ => $"select {countText} {cardNoun}",
                });
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

    private static string DescribeEnchantSelection(Call call, string countText)
    {
        var enchantArg = call.Arguments.ElementAtOrDefault(1);
        var enchantType = enchantArg switch
        {
            Call { Method: { Name: "Enchantment", IsGenericMethod: true } m } => m.GetGenericArguments()[0],
            _ => null,
        };
        var label = enchantType is not null ? NativeRuleText.ModelLabel(enchantType) : "the offered enchantment";
        var prefix = countText == "eligible" ? "eligible deck card(s)" : $"{countText} eligible deck card(s)";
        return $"select {prefix} to enchant with {label}";
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

    private static string? ExtractSelectorCount(EventModel activeEvent, NativeRuleBody body, Step selectStep, Call call)
    {
        var parameters = call.Method.GetParameters();
        var prefsParam = parameters.FirstOrDefault(p => p.ParameterType.Name == "CardSelectorPrefs");
        if (prefsParam is not null && prefsParam.Position < call.Arguments.Length)
        {
            var prefsArg = call.Arguments[prefsParam.Position];
            if (prefsArg is Call { Method: ConstructorInfo } directCtor
                && ReadPrefsCount(activeEvent, directCtor) is { } directCount)
            {
                return directCount;
            }
            // In async state machines, struct CardSelectorPrefs may be initialized via `call .ctor` on a state field.
            var precedingCtor = body.Calls
                .Where(s => s.Call.Method is ConstructorInfo { DeclaringType.Name: "CardSelectorPrefs" }
                    && (s.Epoch < selectStep.Epoch || (s.Epoch == selectStep.Epoch && s.Order < selectStep.Order)))
                .OrderByDescending(s => (s.Epoch, s.Order))
                .FirstOrDefault();
            if (precedingCtor is not null && ReadPrefsCount(activeEvent, precedingCtor.Call) is { } ctorCount)
                return ctorCount;

            return null;
        }

        // Only inspect explicit count parameters when no CardSelectorPrefs is present (never enchantment `amount`).
        var countParam = parameters.FirstOrDefault(p => p.ParameterType == typeof(int) && p.Name is "count" or "numCards");
        if (countParam is not null && countParam.Position < call.Arguments.Length)
            return ResolveEventAmount(activeEvent, call.Arguments[countParam.Position]);

        return null;
    }

    private static string? ReadPrefsCount(EventModel activeEvent, Call prefsCtor)
    {
        if (prefsCtor.Arguments is [_, var singleCount]
            && ResolveEventAmount(activeEvent, singleCount) is { } singleText)
        {
            return singleText;
        }
        if (prefsCtor.Arguments is [_, var minArg, var maxArg])
        {
            var minText = ResolveEventAmount(activeEvent, minArg);
            var maxText = ResolveEventAmount(activeEvent, maxArg);
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

    private static string? DescribeGenericDeckFilter(Call call)
    {
        var filterParam = call.Method.GetParameters().FirstOrDefault(p => p.ParameterType == typeof(Func<CardModel, bool>));
        if (filterParam is null || filterParam.Position >= call.Arguments.Length)
            return null;
        if (call.Arguments[filterParam.Position] is not Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo predMethod }] })
            return null;

        var body = ReadBody(predMethod);
        if (body.Incomplete)
            return null;

        var qualifiers = new List<string>();
        var checksTransformable = body.Calls.Any(s => s.Call.Method.Name == "get_IsTransformable");
        var checksRemovable = body.Calls.Any(s => s.Call.Method.Name == "get_IsRemovable");
        var checksUpgradable = body.Calls.Any(s => s.Call.Method.Name == "get_IsUpgradable");
        if (checksTransformable)
            qualifiers.Add("transformable");
        else if (checksRemovable)
            qualifiers.Add("removable");
        if (checksUpgradable)
            qualifiers.Add("upgradable");

        var rarities = new List<CardRarity>();
        foreach (var ret in body.Returns)
        {
            if (ret.Value is Constant { Data: 0 or false })
                continue;
            foreach (var expr in ret.Conditions.Select(c => c.Value).Append(ret.Value))
            {
                if (expr is Operation { Operator: "ceq" or "beq", Left: Call { Method.Name: "get_Rarity" }, Right: Constant { Data: int raw } }
                    && Enum.IsDefined(typeof(CardRarity), raw) && !rarities.Contains((CardRarity)raw))
                {
                    rarities.Add((CardRarity)raw);
                }
            }
        }
        if (rarities.Count > 0)
            qualifiers.Add(string.Join("/", rarities));

        return qualifiers.Count > 0 ? string.Join(" ", qualifiers) : null;
    }

    private static Type? ExtractSingleCardType(Value? value) => value switch
    {
        Call { Method.Name: ".ctor", Arguments: [var single] } => ExtractSingleCardType(single),
        Call { Method: { Name: "Card", IsGenericMethod: true } lookup } when lookup.DeclaringType == typeof(ModelDb)
            => lookup.GetGenericArguments()[0],
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

    private static string? ResolveEventAmount(EventModel activeEvent, Value? expr)
    {
        while (true)
        {
            if (expr is Call { Method.Name: "op_Implicit", Arguments: [var implicitInner] })
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
