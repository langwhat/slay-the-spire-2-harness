using System.Collections.Concurrent;
using System.Globalization;
using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.ValueProps;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal static class ModifierMechanismReader
{
    private const BindingFlags DeclaredInstance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

    private sealed record SimpleCacheKey(string ModelId, string Language);

    private static readonly ConcurrentDictionary<SimpleCacheKey, string?> EnchantmentCache = new();
    private static readonly ConcurrentDictionary<SimpleCacheKey, string?> AfflictionCache = new();
    private static readonly ConcurrentDictionary<SimpleCacheKey, string?> OrbCache = new();
    private static readonly ConcurrentDictionary<string, byte> LoggedGaps = new(StringComparer.Ordinal);

    private static void PushWarning(string message)
    {
        if (MegaCrit.Sts2.Core.Nodes.NGame.Instance is not null)
            Godot.GD.PushWarning(message);
    }

    public static string? ReadEnchantment(EnchantmentModel enchantment, string language)
    {
        var id = enchantment.Id.ToString();
        var key = new SimpleCacheKey(id, language);
        if (EnchantmentCache.TryGetValue(key, out var cached))
            return cached;

        try
        {
            var (text, gaps) = DescribeEnchantmentWithGaps(enchantment.GetType());
            if (gaps.Count > 0 && LoggedGaps.TryAdd($"enchant:{id}", 0))
                PushWarning($"[sts2agent] Unresolved native enchantment rules for {id}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");
            EnchantmentCache[key] = text;
            return text;
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd($"enchant-err:{id}", 0))
                PushWarning($"[sts2agent] Enchantment mechanism unavailable for {id}: {error.Message}");
            return null;
        }
    }

    public static string? ReadAffliction(AfflictionModel affliction, string language)
    {
        var id = affliction.Id.ToString();
        var key = new SimpleCacheKey(id, language);
        if (AfflictionCache.TryGetValue(key, out var cached))
            return cached;

        try
        {
            var (text, gaps) = DescribeAfflictionWithGaps(affliction.GetType());
            if (gaps.Count > 0 && LoggedGaps.TryAdd($"afflict:{id}", 0))
                PushWarning($"[sts2agent] Unresolved native affliction rules for {id}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");
            AfflictionCache[key] = text;
            return text;
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd($"afflict-err:{id}", 0))
                PushWarning($"[sts2agent] Affliction mechanism unavailable for {id}: {error.Message}");
            return null;
        }
    }

    public static string? ReadOrb(OrbModel orb, string language)
    {
        var id = orb.Id.ToString();
        var key = new SimpleCacheKey(id, language);
        if (OrbCache.TryGetValue(key, out var cached))
            return cached;

        try
        {
            var (text, gaps) = DescribeOrbWithGaps(orb.GetType());
            if (gaps.Count > 0 && LoggedGaps.TryAdd($"orb:{id}", 0))
                PushWarning($"[sts2agent] Unresolved native orb rules for {id}: {string.Join(", ", gaps.Order(StringComparer.Ordinal))}");
            OrbCache[key] = text;
            return text;
        }
        catch (Exception error)
        {
            if (LoggedGaps.TryAdd($"orb-err:{id}", 0))
                PushWarning($"[sts2agent] Orb mechanism unavailable for {id}: {error.Message}");
            return null;
        }
    }

    internal static string? DescribeEnchantment(Type type)
        => DescribeEnchantmentWithGaps(type).Text;

    internal static (string? Text, IReadOnlySet<string> Gaps) DescribeEnchantmentWithGaps(Type type)
    {
        var gaps = new HashSet<string>(StringComparer.Ordinal);
        if (!typeof(EnchantmentModel).IsAssignableFrom(type) || type.IsAbstract)
            return (null, gaps);

        var parts = new List<string>();

        if (DescribeEnchantmentEligibility(type, gaps) is { } elig)
            parts.Add(elig);

        var afterPlayMethod = type.GetMethod("AfterCardPlayed", DeclaredInstance);
        var afterPlayBody = afterPlayMethod is not null ? NativeRuleBody.Read(afterPlayMethod) : null;
        var disablesAfterPlay = afterPlayBody is { Incomplete: false }
            && afterPlayBody.Calls.Any(s => s.Call.Method.Name == "set_Status"
                && s.Call.Arguments is [Constant { Data: int statusVal }] && statusVal != 0);
        var incrementsAmountAfterPlay = afterPlayBody is { Incomplete: false }
            && afterPlayBody.Calls.Any(s => s.Call.Method.Name == "set_Amount"
                && s.Call.Arguments is [Operation { Operator: "add", Left: Call { Method.Name: "get_Amount" }, Right: Constant { Data: 1 } }]);
        var updatesDeckVersionAfterPlay = afterPlayBody is { Incomplete: false }
            && afterPlayBody.Calls.Any(s => s.Call.Method.Name == "get_DeckVersion");

        var onPlayMethod = type.GetMethod("OnPlay", DeclaredInstance);
        var onPlayBody = onPlayMethod is not null ? NativeRuleBody.Read(onPlayMethod) : null;
        var accumulatedPropName = (string?)null;
        if (onPlayBody is { Incomplete: false })
        {
            var accumCall = onPlayBody.Calls.FirstOrDefault(s =>
                s.Call.Method.DeclaringType == type
                && s.Call.Method.Name.StartsWith("set_", StringComparison.Ordinal)
                && s.Call.Arguments is [Operation { Operator: "add", Left: Call { Method.Name: var getterName }, Right: Call { Method.Name: "get_Amount" } }]
                && getterName == "get_" + s.Call.Method.Name[4..]);
            if (accumCall is not null)
                accumulatedPropName = accumCall.Call.Method.Name[4..];
        }

        if (type.GetMethod("EnchantDamageAdditive", DeclaredInstance) is { } dmgAdd)
        {
            var dmgAddBody = NativeRuleBody.Read(dmgAdd);
            if (dmgAddBody.Incomplete)
            {
                gaps.Add("EnchantDamageAdditive");
            }
            else if (DescribeEnchantDamageAdditive(type, dmgAddBody, disablesAfterPlay, accumulatedPropName) is { } dmgClause)
            {
                parts.Add(dmgClause);
            }
            else
            {
                gaps.Add("EnchantDamageAdditive");
            }
        }

        if (type.GetMethod("EnchantDamageMultiplicative", DeclaredInstance) is { } dmgMult)
        {
            var dmgMultBody = NativeRuleBody.Read(dmgMult);
            if (dmgMultBody.Incomplete)
            {
                gaps.Add("EnchantDamageMultiplicative");
            }
            else
            {
                var factor = dmgMultBody.Returns
                    .Select(r => ReadDecimalLiteral(r.Value))
                    .FirstOrDefault(v => v is not null && v != 1m);
                if (factor is { } f)
                {
                    var poweredOnly = dmgMultBody.Returns.Any(r => r.Conditions.Any(c => c.Value is Call { Method.Name: "IsPoweredAttack" }));
                    var scope = poweredOnly ? "powered attack damage" : "attack damage";
                    parts.Add($"Multiplies {scope} by ×{f.ToString("0.########", CultureInfo.InvariantCulture)}.");
                }
                else
                {
                    gaps.Add("EnchantDamageMultiplicative");
                }
            }
        }

        if (type.GetMethod("EnchantBlockAdditive", DeclaredInstance) is { } blkAdd)
        {
            var blkAddBody = NativeRuleBody.Read(blkAdd);
            if (blkAddBody.Incomplete)
            {
                gaps.Add("EnchantBlockAdditive");
            }
            else if (DescribeEnchantBlockAdditive(blkAddBody) is { } blkClause)
            {
                parts.Add(blkClause);
            }
            else
            {
                gaps.Add("EnchantBlockAdditive");
            }
        }

        if (incrementsAmountAfterPlay)
        {
            parts.Add(updatesDeckVersionAfterPlay
                ? "Permanently increases this enchantment's amount by +1 each time this card is played."
                : "Increases this enchantment's amount by +1 each time this card is played this combat.");
        }

        if (type.GetMethod("EnchantPlayCount", DeclaredInstance) is { } playCnt)
        {
            var playCntBody = NativeRuleBody.Read(playCnt);
            if (playCntBody.Incomplete)
            {
                gaps.Add("EnchantPlayCount");
            }
            else
            {
                var extraPlays = ResolveEnchantPlayCountDelta(type, playCntBody);
                if (extraPlays is > 0)
                {
                    var suffix = disablesAfterPlay ? " on its first play each combat" : " every time it is played";
                    var noun = extraPlays.Value == 1 ? "time" : "times";
                    parts.Add($"Replays this card {extraPlays.Value.ToString(CultureInfo.InvariantCulture)} additional {noun}{suffix}.");
                }
                else
                {
                    gaps.Add("EnchantPlayCount");
                }
            }
        }

        if (onPlayBody is { Incomplete: false })
        {
            var oncePrefix = disablesAfterPlay ? "First play each combat: " : "On play: ";
            if (onPlayBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.Draw)))
                parts.Add($"{oncePrefix}draws amount card(s).");
            if (onPlayBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.GainEnergy)))
                parts.Add($"{oncePrefix}gains amount Energy.");
            if (onPlayBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock)))
            {
                var isUnpoweredBlock = IsEnchantmentBlockUnpowered(type, onPlayBody);
                parts.Add(isUnpoweredBlock
                    ? $"{oncePrefix}gains amount unpowered Block."
                    : $"{oncePrefix}gains amount Block (scales with Block modifiers such as Dexterity/Frail).");
            }
            if (onPlayBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Damage)) is { } dmgCall)
            {
                var amt = dmgCall.Call.Arguments.ElementAtOrDefault(2) is { } a ? ReadDecimalLiteral(a) : null;
                var amtText = amt is { } v ? v.ToString("0.########", CultureInfo.InvariantCulture) : "amount";
                parts.Add($"{oncePrefix}deals {amtText} unblockable unpowered self-damage.");
            }
            if (onPlayBody.Calls.FirstOrDefault(s => s.Call.Method.DeclaringType == typeof(PowerCmd) && s.Call.Method.Name == nameof(PowerCmd.Apply) && s.Call.Method.IsGenericMethod) is { } pwrCall)
            {
                var pt = pwrCall.Call.Method.GetGenericArguments()[0];
                parts.Add($"{oncePrefix}applies {NativeRuleText.ModelLabel(pt)}.");
            }
            if (accumulatedPropName is null
                && onPlayBody.Calls.Any(s => s.Call.Method.Name == "set_Amount" || s.Call.Method.Name == "UpgradeValueBy"))
            {
                parts.Add("Increases this enchantment's bonus each time this card is played this combat.");
            }
        }
        else if (onPlayMethod is not null)
        {
            gaps.Add("OnPlay");
        }

        if (type.GetMethod("OnEnchant", DeclaredInstance) is { } onEnchant)
        {
            var enchBody = NativeRuleBody.Read(onEnchant);
            if (enchBody.Incomplete)
            {
                gaps.Add("OnEnchant");
            }
            else
            {
                var beforeCount = parts.Count;
                var addedKws = ExtractKeywordArguments(enchBody, "AddKeyword", "ApplyKeyword");
                if (addedKws.Length > 0)
                    parts.Add($"Grants keyword(s): {string.Join(", ", addedKws)}.");

                var removedKws = ExtractKeywordArguments(enchBody, "RemoveKeyword");
                if (removedKws.Length > 0)
                    parts.Add($"Removes keyword(s): {string.Join(", ", removedKws)}.");

                if (enchBody.Calls.Any(s => s.Call.Method.Name is "WithBaseCost" or "SetWithModifiers" or "AddModifier" or "ChangeBaseCost" or "UpgradeBy"))
                    parts.Add("Permanently modifies the enchanted card's Energy cost.");

                if (parts.Count == beforeCount)
                    gaps.Add("OnEnchant");
            }
        }

        if (type.GetMethod("AfterAutoPrePlayPhaseEntered", DeclaredInstance) is { } autoPre
            && NativeRuleBody.Read(autoPre) is { Incomplete: false } autoPreBody
            && autoPreBody.Calls.Any(s => s.Call.Method.Name == "AutoPlay"))
        {
            parts.Add("Auto-plays this card automatically at the start of turn 1 before normal card play.");
        }

        if (type.GetMethod("BeforeFlush", DeclaredInstance) is { } beforeFlush)
        {
            var flushBody = NativeRuleBody.Read(beforeFlush);
            if (flushBody.Incomplete)
            {
                gaps.Add("BeforeFlush");
            }
            else if (flushBody.Calls.Any(s => s.Call.Method.Name == "AddUntilPlayed"))
            {
                parts.Add("If still in hand before end-of-turn discard, reduces this card's Energy cost until played.");
            }
            else if (flushBody.Calls.Any(s => s.Call.Method.Name == "AddThisCombat" || s.Call.Method.Name == "AddThisTurn"))
            {
                parts.Add("If still in hand before end-of-turn discard, reduces this card's Energy cost for this combat.");
            }
            else
            {
                gaps.Add("BeforeFlush");
            }
        }

        if (type.GetMethod("AfterCardDrawn", DeclaredInstance) is { } afterDraw)
        {
            var drawBody = NativeRuleBody.Read(afterDraw);
            if (drawBody.Incomplete)
            {
                gaps.Add("AfterCardDrawn");
            }
            else if (drawBody.Calls.FirstOrDefault(s =>
                s.Call.Method.DeclaringType == typeof(MegaCrit.Sts2.Core.Entities.Cards.CardEnergyCost)
                && s.Call.Method.Name is "SetThisTurn" or "SetThisCombat") is { } setCostStep
                && IsRandomizedEnergyCostArg(type, setCostStep.Call.Arguments.ElementAtOrDefault(0))
                && TryReadAfterCardDrawnConditions(setCostStep.Conditions, out var checksHand))
            {
                var duration = setCostStep.Call.Method.Name == "SetThisCombat" ? " for this combat" : " this turn";
                parts.Add(checksHand
                    ? $"If still in hand when drawn, randomizes this card's Energy cost{duration}."
                    : $"Randomizes this card's Energy cost{duration} when drawn.");
            }
            else
            {
                gaps.Add("AfterCardDrawn");
            }
        }

        if (type.GetProperty("ShouldStartAtBottomOfDrawPile", DeclaredInstance)?.GetMethod is { } startBottom
            && NativeRuleBody.Read(startBottom) is { Incomplete: false, Returns: [{ Conditions.Length: 0, Value: var sbVal }] }
            && TryTruth(sbVal, out var startsAtBottom) && startsAtBottom)
        {
            parts.Add("Starts at the bottom of the initial draw pile (taking precedence over Innate; only drawn in the opening hand if the opening draw reaches this card's position).");
        }

        if (type.GetMethod("ModifyShuffleOrder", DeclaredInstance) is { } shuffleOrder)
        {
            var shuffleBody = NativeRuleBody.Read(shuffleOrder);
            if (shuffleBody.Incomplete)
            {
                gaps.Add("ModifyShuffleOrder");
            }
            else if (DescribeModifyShuffleOrder(shuffleBody) is { } shuffleClause)
            {
                parts.Add(shuffleClause);
            }
            else
            {
                gaps.Add("ModifyShuffleOrder");
            }
        }

        if (type.GetMethod("TryModifyRestSiteOptions", DeclaredInstance) is { } restSiteOptions)
        {
            var restBody = NativeRuleBody.Read(restSiteOptions);
            if (restBody.Incomplete)
            {
                gaps.Add("TryModifyRestSiteOptions");
            }
            else if (DescribeTryModifyRestSiteOptions(restBody) is { } restClause)
            {
                parts.Add(restClause);
            }
            else
            {
                gaps.Add("TryModifyRestSiteOptions");
            }
        }

        RecordUnhandledModifierHooks(type, typeof(EnchantmentModel), HandledEnchantmentHooks, gaps);

        return (parts.Count > 0 ? string.Join(" ", parts) : null, gaps);
    }

    private static string? DescribeTryModifyRestSiteOptions(NativeRuleBody body)
    {
        var addedOptions = body.Calls
            .Where(s => s.Call.Method.Name == "Add"
                && s.Call.Receiver is Input { Name: "options" }
                && s.Call.Arguments is [Call { Method: ConstructorInfo ctor }]
                && ctor.DeclaringType is { } optType
                && typeof(MegaCrit.Sts2.Core.Entities.RestSite.RestSiteOption).IsAssignableFrom(optType))
            .Select(s => ((Call)s.Call.Arguments[0]).Method.DeclaringType!)
            .Distinct()
            .ToArray();
        if (addedOptions is [var optionType]
            && optionType == typeof(MegaCrit.Sts2.Core.Entities.RestSite.CloneRestSiteOption))
        {
            return "At Rest Sites, adds one Clone option while at least one card with this enchantment is in your deck (choosing Clone duplicates every card with this enchantment into your deck).";
        }
        return null;
    }

    private static readonly HashSet<string> HandledEnchantmentHooks = new(StringComparer.Ordinal)
    {
        "CanEnchantCardType", "CanEnchant", "AfterCardPlayed", "EnchantDamageAdditive", "EnchantDamageMultiplicative",
        "EnchantBlockAdditive", "EnchantPlayCount", "OnPlay", "OnEnchant", "AfterAutoPrePlayPhaseEntered",
        "BeforeFlush", "AfterCardDrawn", "ModifyShuffleOrder", "TryModifyRestSiteOptions", "RecalculateValues",
    };

    private static readonly HashSet<string> HandledAfflictionHooks = new(StringComparer.Ordinal)
    {
        "AfterCardEnteredCombat", "CanAfflictCardType",
    };

    private static void RecordUnhandledModifierHooks(Type type, Type baseModelType, HashSet<string> handledNames, HashSet<string> gaps)
    {
        foreach (var method in type.GetMethods(DeclaredInstance))
        {
            if (method.IsSpecialName || handledNames.Contains(method.Name))
                continue;
            var baseDef = method.GetBaseDefinition();
            if (baseDef.DeclaringType == typeof(object) || baseDef.DeclaringType == type)
                continue;
            if (baseModelType.IsAssignableFrom(baseDef.DeclaringType) || baseDef.DeclaringType == typeof(AbstractModel))
                gaps.Add(method.Name);
        }
    }

    private static bool IsEnchantmentBlockUnpowered(Type type, NativeRuleBody onPlayBody)
    {
        if (ModelDb.GetByIdOrNull<EnchantmentModel>(ModelDb.GetId(type)) is { } canonical
            && canonical.DynamicVars.TryGetValue("Block", out var dv)
            && dv is MegaCrit.Sts2.Core.Localization.DynamicVars.BlockVar blockVar)
        {
            return blockVar.Props.HasFlag(ValueProp.Unpowered);
        }

        var gainCall = onPlayBody.Calls.FirstOrDefault(s =>
            s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock));
        if (gainCall is not null)
        {
            foreach (var arg in gainCall.Call.Arguments)
            {
                if (arg is Constant { Data: int rawProp })
                    return ((ValueProp)rawProp).HasFlag(ValueProp.Unpowered);
            }
        }
        return false;
    }

    private static int? ResolveEnchantPlayCountDelta(Type type, NativeRuleBody playCntBody)
    {
        var nonPassthrough = playCntBody.Returns
            .Where(r => r.Value is not Input { Name: "originalPlayCount" or "playCount" })
            .ToArray();
        if (nonPassthrough.Length == 0)
            return null;

        int? agreedDelta = null;
        foreach (var ret in nonPassthrough)
        {
            // Every boosted path must be unguarded or guarded solely by `base.Status != EnchantmentStatus.Disabled`.
            foreach (var cond in ret.Conditions)
            {
                var expr = cond.Value;
                var expected = cond.Expected;
                while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
                {
                    expr = inner;
                    expected = !expected;
                }
                var isNotDisabled = expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Call { Method.Name: "get_Status", Receiver: Input { Name: "this" } }, Right: Constant { Data: (int)MegaCrit.Sts2.Core.Entities.Enchantments.EnchantmentStatus.Disabled } } statusOp
                    && (statusOp.Operator == "bne.un" ? expected : !expected);
                if (!isNotDisabled)
                    return null;
            }

            if (ret.Value is not Operation { Operator: "add", Left: Input { Name: "originalPlayCount" or "playCount" }, Right: var right })
                return null;
            int? delta = null;
            if (right is Constant { Data: int literal } && literal > 0)
            {
                delta = literal;
            }
            else if (right is Call { Method.Name: "get_IntValue" or "get_BaseValue", Receiver: Call { Method.Name: "get_Item", Arguments: [Constant { Data: string varKey }] } }
                && ModelDb.GetByIdOrNull<EnchantmentModel>(ModelDb.GetId(type)) is { } canonical
                && canonical.DynamicVars.TryGetValue(varKey, out var dv)
                && dv.BaseValue > 0m && dv.BaseValue == decimal.Truncate(dv.BaseValue))
            {
                delta = (int)dv.BaseValue;
            }
            if (delta is null)
                return null;
            if (agreedDelta is not null && agreedDelta != delta)
                return null;
            agreedDelta = delta;
        }
        return agreedDelta;
    }

    private static string? DescribeEnchantDamageAdditive(
        Type type,
        NativeRuleBody body,
        bool disablesAfterPlay,
        string? accumulatedPropName)
    {
        var nonZeroReturns = body.Returns
            .Where(r => ReadDecimalLiteral(UnwrapImplicit(r.Value)) is not 0m)
            .ToArray();
        if (nonZeroReturns.Length != 1)
            return null;

        var ret = nonZeroReturns[0];
        var expr = UnwrapImplicit(ret.Value);
        var poweredOnly = body.Returns.Any(r => r.Conditions.Any(c => c.Value is Call { Method.Name: "IsPoweredAttack" }));
        var scope = poweredOnly ? "powered attack damage per hit" : "attack damage per hit";
        var suffix = disablesAfterPlay ? " (first play each combat only)" : "";

        if (expr is Call { Method.Name: "get_Amount", Receiver: Input { Name: "this" } })
            return $"Adds +amount {scope}{suffix}.";

        if (accumulatedPropName is not null
            && expr is Call { Method.Name: var propGetter, Receiver: Input { Name: "this" } }
            && propGetter == "get_" + accumulatedPropName)
        {
            return $"Adds accumulated bonus {scope} (starts at +0; increases by +amount each time this card is played this combat).";
        }

        if (expr is Call { Method.Name: "get_BaseValue" or "get_IntValue", Receiver: Call { Method.Name: "get_Item", Arguments: [Constant { Data: string varKey }] } }
            && ModelDb.GetByIdOrNull<EnchantmentModel>(ModelDb.GetId(type)) is { } canonical
            && canonical.DynamicVars.TryGetValue(varKey, out var dv))
        {
            return $"Adds +{dv.BaseValue.ToString("0.########", CultureInfo.InvariantCulture)} {scope}{suffix}.";
        }

        if (ReadDecimalLiteral(expr) is { } literal && literal > 0m)
            return $"Adds +{literal.ToString("0.########", CultureInfo.InvariantCulture)} {scope}{suffix}.";

        return null;
    }

    private static string? DescribeEnchantBlockAdditive(NativeRuleBody body)
    {
        var nonZeroReturns = body.Returns
            .Where(r => ReadDecimalLiteral(UnwrapImplicit(r.Value)) is not 0m)
            .ToArray();
        if (nonZeroReturns.Length != 1)
            return null;

        var expr = UnwrapImplicit(nonZeroReturns[0].Value);
        if (expr is Call { Method.Name: "get_Amount", Receiver: Input { Name: "this" } })
            return "Adds +amount Block (before multipliers).";

        if (expr is Operation { Operator: "sub", Left: Call { Method.Name: "get_Amount", Receiver: Input { Name: "this" } }, Right: Constant { Data: int delta } } && delta > 0)
            return $"Adds +(amount - {delta.ToString(CultureInfo.InvariantCulture)}) Block (before multipliers).";

        if (expr is Call { Method.Name: "op_Subtraction", Arguments: [var lhs, var rhs] }
            && UnwrapImplicit(lhs) is Call { Method.Name: "get_Amount", Receiver: Input { Name: "this" } }
            && ReadDecimalLiteral(UnwrapImplicit(rhs)) is { } decDelta && decDelta > 0m)
        {
            return $"Adds +(amount - {decDelta.ToString("0.########", CultureInfo.InvariantCulture)}) Block (before multipliers).";
        }

        if (ReadDecimalLiteral(expr) is { } literal && literal > 0m)
            return $"Adds +{literal.ToString("0.########", CultureInfo.InvariantCulture)} Block (before multipliers).";

        return null;
    }

    private static string? DescribeModifyShuffleOrder(NativeRuleBody body)
    {
        var insertStep = body.Calls.FirstOrDefault(s =>
            s.Call.Method.Name == "Insert"
            && s.Call.Arguments is [Constant { Data: 0 }, Call { Method.Name: "get_Card", Receiver: Input { Name: "this" } }]);
        var hasRemove = body.Calls.Any(s =>
            s.Call.Method.Name == "Remove"
            && s.Call.Arguments is [Call { Method.Name: "get_Card", Receiver: Input { Name: "this" } }]);
        if (insertStep is null || !hasRemove)
            return null;

        var initialChecks = insertStep.Conditions
            .Where(c => c.Value is Input { Name: "isInitialShuffle" })
            .ToArray();
        if (initialChecks.Any(c => !c.Expected))
            return "Places this card at the top of the draw pile on non-initial shuffles (not the opening combat shuffle).";
        if (initialChecks.Length == 0)
            return "Places this card at the top of the draw pile whenever the draw pile is shuffled.";
        return null;
    }

    private static Value UnwrapImplicit(Value value)
    {
        while (value is Call { Method.Name: "op_Implicit", Arguments: [var inner] })
            value = inner;
        return value;
    }

    private static string? DescribeEnchantmentEligibility(Type type, HashSet<string> gaps)
    {
        var clauses = new List<string>();
        if (type.GetMethod("CanEnchantCardType", DeclaredInstance) is { } byType)
        {
            var body = NativeRuleBody.Read(byType);
            if (body.Incomplete)
            {
                gaps.Add("CanEnchantCardType");
            }
            else if (TryReadCanEnchantCardTypes(body) is { Length: > 0 } types)
            {
                clauses.Add($"Eligible cards: {string.Join("/", types)} only.");
            }
            else
            {
                gaps.Add("CanEnchantCardType");
            }
        }
        if (type.GetMethod("CanEnchant", DeclaredInstance) is { } custom)
        {
            var customBody = NativeRuleBody.Read(custom);
            if (customBody.Incomplete)
            {
                gaps.Add("CanEnchant");
            }
            else if (IsPositiveGainsBlockCanEnchant(customBody))
            {
                clauses.Add("Eligible cards: cards that gain Block.");
            }
            else if (TryReadLocalKeywordCanEnchant(customBody) is { } localKw)
            {
                clauses.Add($"Eligible cards: cards with local {NativeRuleText.KeywordLabel(localKw)} keyword.");
            }
            else if (DescribeCustomCanEnchant(customBody) is { } customDesc)
            {
                clauses.Add($"Eligible cards: {customDesc}.");
            }
            else
            {
                gaps.Add("CanEnchant");
            }
        }
        return clauses.Count > 0 ? string.Join(" ", clauses) : null;
    }

    private static bool IsRandomizedEnergyCostArg(Type enchantmentType, Value? arg)
    {
        if (arg is Call { Method.Name: "NextInt" })
            return true;
        if (arg is Call { Method: MethodInfo helper } && helper.DeclaringType == enchantmentType
            && NativeRuleBody.Read(helper) is { Incomplete: false } helperBody)
            return helperBody.Calls.Any(c => c.Call.Method.Name == "NextInt");
        return false;
    }

    private static bool TryReadAfterCardDrawnConditions(Test[] conditions, out bool checksHand)
    {
        checksHand = false;
        foreach (var raw in conditions)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: var l, Right: var r } eqOp)
            {
                var isEq = eqOp.Operator == "bne.un" ? !expected : expected;
                if (isEq && ((l is Input { Name: "card" } && r is Call { Method.Name: "get_Card" })
                    || (r is Input { Name: "card" } && l is Call { Method.Name: "get_Card" })))
                    continue;
                if (isEq && l is Call { Method.Name: "get_Type", Method.DeclaringType: var pileDecl }
                    && pileDecl == typeof(CardPile)
                    && r is Constant { Data: int rawPile } && (PileType)rawPile == PileType.Hand)
                {
                    checksHand = true;
                    continue;
                }
            }
            return false;
        }
        return true;
    }

    private static CardType[]? TryReadCanEnchantCardTypes(NativeRuleBody body)
    {
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length == 0)
            return null;
        var matched = new List<CardType>();
        foreach (var ret in positive)
        {
            if (ret.Value is Operation { Operator: "ceq", Left: Input, Right: Constant { Data: int ct } }
                && Enum.IsDefined(typeof(CardType), ct))
            {
                if (!matched.Contains((CardType)ct))
                    matched.Add((CardType)ct);
                continue;
            }
            if (TryTruth(ret.Value, out var rb) && rb)
            {
                var branchTypes = new List<CardType>();
                foreach (var raw in ret.Conditions)
                {
                    var expr = raw.Value;
                    var expected = raw.Expected;
                    while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
                    {
                        expr = inner;
                        expected = !expected;
                    }
                    if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Input, Right: Constant { Data: int cct } } op
                        && Enum.IsDefined(typeof(CardType), cct))
                    {
                        var isEq = op.Operator == "bne.un" ? !expected : expected;
                        if (isEq)
                            branchTypes.Add((CardType)cct);
                        continue;
                    }
                    return null;
                }
                if (branchTypes.Count == 0)
                    return null;
                foreach (var bt in branchTypes)
                    if (!matched.Contains(bt))
                        matched.Add(bt);
                continue;
            }
            return null;
        }
        return matched.ToArray();
    }

    private static bool IsPositiveGainsBlockCanEnchant(NativeRuleBody body)
    {
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var rb) && rb))
            tests.Add(new Test(positive[0].Value, true));
        var sawGainsBlock = false;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expected && expr is Call { Method.Name: "CanEnchant", Method.DeclaringType: var decl } && decl == typeof(EnchantmentModel))
                continue;
            if (expected && expr is Call { Method.Name: "get_GainsBlock", Receiver: Input })
            {
                sawGainsBlock = true;
                continue;
            }
            return false;
        }
        return sawGainsBlock;
    }

    private static CardKeyword? TryReadLocalKeywordCanEnchant(NativeRuleBody body)
    {
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return null;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var rb) && rb))
            tests.Add(new Test(positive[0].Value, true));
        CardKeyword? matched = null;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expected && expr is Call { Method.Name: "CanEnchant", Method.DeclaringType: var decl } && decl == typeof(EnchantmentModel))
                continue;
            if (expected && expr is Call { Method.Name: "Contains", Receiver: var kwRec, Arguments: var args }
                && (kwRec ?? args.ElementAtOrDefault(0)) is Call { Method.Name: "GetKeywordsWithSources", Receiver: Input { Name: "card" }, Arguments: [Constant { Data: int rawSrc }] }
                && (KeywordSources)rawSrc == KeywordSources.Local
                && (kwRec is not null ? args.ElementAtOrDefault(0) : args.ElementAtOrDefault(1)) is Constant { Data: int rawKw }
                && Enum.IsDefined(typeof(CardKeyword), rawKw))
            {
                var nextKw = (CardKeyword)rawKw;
                if (matched is not null && matched != nextKw)
                    return null;
                matched = nextKw;
                continue;
            }
            return null;
        }
        return matched;
    }

    private sealed record CanEnchantBranch(
        bool ExcludesUnplayable,
        bool ExcludesXCost,
        CardRarity? Rarity,
        List<CardRarity> NegativeRarities,
        List<CardTag> PositiveTags,
        List<CardTag> NegativeTags);

    private static string? DescribeCustomCanEnchant(NativeRuleBody body)
    {
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length == 0)
            return null;

        var branches = new List<CanEnchantBranch>(positive.Length);
        foreach (var ret in positive)
        {
            CardRarity? branchRarity = null;
            var negRarities = new List<CardRarity>();
            var posTags = new List<CardTag>();
            var negTags = new List<CardTag>();
            var excludesUnplayable = false;
            var excludesXCost = false;
            var tests = new List<Test>(ret.Conditions);
            if (!(TryTruth(ret.Value, out var rb) && rb))
                tests.Add(new Test(ret.Value, true));

            foreach (var raw in tests)
            {
                var expr = raw.Value;
                var expected = raw.Expected;
                while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
                {
                    expr = inner;
                    expected = !expected;
                }
                if (expected && expr is Call { Method.Name: "CanEnchant", Method.DeclaringType: var decl } && decl == typeof(EnchantmentModel))
                    continue;
                if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Call { Method.Name: "get_Rarity" }, Right: Constant { Data: int rawRar } } rarOp
                    && Enum.IsDefined(typeof(CardRarity), rawRar))
                {
                    var isEq = rarOp.Operator == "bne.un" ? !expected : expected;
                    var rar = (CardRarity)rawRar;
                    if (isEq)
                    {
                        if (branchRarity is not null && branchRarity != rar)
                            return null;
                        branchRarity = rar;
                    }
                    else if (!negRarities.Contains(rar))
                    {
                        negRarities.Add(rar);
                    }
                    continue;
                }
                if (expr is Call { Method.Name: "Contains", Arguments: [Call { Method.Name: "get_Tags" }, Constant { Data: int rawTag }] }
                    && Enum.IsDefined(typeof(CardTag), rawTag))
                {
                    var tag = (CardTag)rawTag;
                    if (expected)
                    {
                        if (!posTags.Contains(tag))
                            posTags.Add(tag);
                    }
                    else if (!negTags.Contains(tag))
                    {
                        negTags.Add(tag);
                    }
                    continue;
                }
                if (!expected && expr is Call { Method.Name: "Contains", Receiver: Call { Method.Name: "get_Keywords" }, Arguments: [Constant { Data: (int)CardKeyword.Unplayable }] })
                {
                    excludesUnplayable = true;
                    continue;
                }
                if (!expected && expr is Call { Method.Name: "get_CostsX" })
                {
                    excludesXCost = true;
                    continue;
                }
                return null;
            }
            branches.Add(new CanEnchantBranch(excludesUnplayable, excludesXCost, branchRarity, negRarities, posTags, negTags));
        }

        // Only allow a negated tag or rarity on a branch when another branch with compatible qualifiers
        // positively covers it (short-circuit `A || B`).
        foreach (var branch in branches)
        {
            foreach (var negTag in branch.NegativeTags)
            {
                var covered = branches.Any(other =>
                    other != branch
                    && other.ExcludesUnplayable == branch.ExcludesUnplayable
                    && other.ExcludesXCost == branch.ExcludesXCost
                    && other.Rarity == branch.Rarity
                    && other.PositiveTags.Contains(negTag)
                    && other.PositiveTags.Count == 1);
                if (!covered)
                    return null;
            }
            foreach (var negRar in branch.NegativeRarities)
            {
                var covered = branches.Any(other =>
                    other != branch
                    && other.ExcludesUnplayable == branch.ExcludesUnplayable
                    && other.ExcludesXCost == branch.ExcludesXCost
                    && other.Rarity == negRar
                    && other.PositiveTags.Count == 0);
                if (!covered)
                    return null;
            }
        }

        // Merge branches that share identical non-tag qualifiers (e.g., Basic && (Defend || Strike)).
        var sameNonTag = branches.All(b =>
            b.ExcludesUnplayable == branches[0].ExcludesUnplayable
            && b.ExcludesXCost == branches[0].ExcludesXCost
            && b.Rarity == branches[0].Rarity);
        if (sameNonTag)
        {
            var nonTagPrefix = FormatNonTagPrefix(branches[0]);
            var hasTaglessBranch = branches.Any(b => b.PositiveTags.Count == 0);
            if (hasTaglessBranch)
                return nonTagPrefix.Length > 0 ? $"{nonTagPrefix} cards" : null;
            var tagGroups = branches
                .Select(b => string.Join(" and ", b.PositiveTags))
                .Distinct(StringComparer.Ordinal)
                .ToArray();
            var tagText = string.Join(" or ", tagGroups);
            return nonTagPrefix.Length > 0
                ? $"{nonTagPrefix} cards tagged {tagText}"
                : $"{tagText}-tagged cards";
        }

        // Otherwise render each branch explicitly as a disjunction.
        var renderedBranches = new List<string>(branches.Count);
        foreach (var branch in branches)
        {
            var nonTag = FormatNonTagPrefix(branch);
            string desc;
            if (branch.PositiveTags.Count == 0)
            {
                if (nonTag.Length == 0)
                    return null;
                desc = $"{nonTag} cards";
            }
            else
            {
                var tagText = string.Join(" and ", branch.PositiveTags);
                desc = nonTag.Length > 0 ? $"{nonTag} cards tagged {tagText}" : $"{tagText}-tagged cards";
            }
            if (!renderedBranches.Contains(desc, StringComparer.Ordinal))
                renderedBranches.Add(desc);
        }
        return renderedBranches.Count > 0 ? string.Join(" or ", renderedBranches) : null;

        static string FormatNonTagPrefix(CanEnchantBranch b)
        {
            var p = new List<string>();
            if (b.ExcludesUnplayable)
                p.Add("playable");
            if (b.ExcludesXCost)
                p.Add("non-Energy-X-cost");
            if (b.Rarity is { } r)
                p.Add(r.ToString());
            return string.Join(" ", p);
        }
    }

    private static string[] ExtractKeywordArguments(NativeRuleBody body, params string[] methodNames)
    {
        var keywords = new List<string>();
        foreach (var step in body.Calls)
        {
            if (!methodNames.Contains(step.Call.Method.Name, StringComparer.Ordinal))
                continue;
            foreach (var arg in step.Call.Arguments)
            {
                if (arg is Constant { Data: int rawKw } && Enum.IsDefined(typeof(CardKeyword), rawKw))
                {
                    var label = NativeRuleText.KeywordLabel((CardKeyword)rawKw);
                    if (!keywords.Contains(label, StringComparer.Ordinal))
                        keywords.Add(label);
                }
                else if (arg is ArrayValue array)
                {
                    foreach (var elem in array.Elements)
                    {
                        if (elem is Constant { Data: int arrKw } && Enum.IsDefined(typeof(CardKeyword), arrKw))
                        {
                            var label = NativeRuleText.KeywordLabel((CardKeyword)arrKw);
                            if (!keywords.Contains(label, StringComparer.Ordinal))
                                keywords.Add(label);
                        }
                    }
                }
            }
        }
        return keywords.ToArray();
    }

    internal static string? DescribeAffliction(Type type)
        => DescribeAfflictionWithGaps(type).Text;

    internal static (string? Text, IReadOnlySet<string> Gaps) DescribeAfflictionWithGaps(Type type)
    {
        var gaps = new HashSet<string>(StringComparer.Ordinal);
        if (!typeof(AfflictionModel).IsAssignableFrom(type) || type.IsAbstract)
            return (null, gaps);

        var parts = new List<string>();
        if (type.GetMethod("AfterCardEnteredCombat", DeclaredInstance) is { } entered)
        {
            var enteredBody = NativeRuleBody.Read(entered);
            if (enteredBody.Incomplete)
            {
                gaps.Add("AfterCardEnteredCombat");
            }
            else if (enteredBody.Calls.Any(s => s.Call.Method.Name == "AddKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw)))
            {
                var kws = enteredBody.Calls
                    .Where(s => s.Call.Method.Name == "AddKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw))
                    .Select(s => ((CardKeyword)(int)((Constant)s.Call.Arguments[0]).Data!).ToString())
                    .Distinct(StringComparer.Ordinal);
                parts.Add($"Grants {string.Join(", ", kws)} to the afflicted card in combat.");
            }
            else
            {
                gaps.Add("AfterCardEnteredCombat");
            }
        }

        if (type.GetMethod("CanAfflictCardType", DeclaredInstance) is { } canType)
        {
            var canBody = NativeRuleBody.Read(canType);
            if (canBody.Incomplete)
            {
                gaps.Add("CanAfflictCardType");
            }
            else
            {
                var types = canBody.Returns
                    .SelectMany(r => r.Value is Operation { Operator: "ceq", Right: Constant { Data: int ct } } && Enum.IsDefined(typeof(CardType), ct)
                        ? [(CardType)ct]
                        : Array.Empty<CardType>())
                    .Distinct()
                    .ToArray();
                if (types.Length > 0)
                    parts.Add($"Applies only to {string.Join("/", types)} cards.");
            }
        }

        RecordUnhandledModifierHooks(type, typeof(AfflictionModel), HandledAfflictionHooks, gaps);
        return (parts.Count > 0 ? string.Join(" ", parts) : null, gaps);
    }

    internal static string? DescribeOrb(Type type)
        => DescribeOrbWithGaps(type).Text;

    internal static (string? Text, IReadOnlySet<string> Gaps) DescribeOrbWithGaps(Type type)
    {
        var gaps = new HashSet<string>(StringComparer.Ordinal);
        if (!typeof(OrbModel).IsAssignableFrom(type) || type.IsAbstract)
            return (null, gaps);

        var passiveProp = type.GetProperty("PassiveVal", DeclaredInstance)?.GetMethod;
        var evokeProp = type.GetProperty("EvokeVal", DeclaredInstance)?.GetMethod;
        var passiveBody = passiveProp is not null ? NativeRuleBody.Read(passiveProp) : null;
        var evokeBody = evokeProp is not null ? NativeRuleBody.Read(evokeProp) : null;
        if (passiveBody?.Incomplete == true)
            gaps.Add("PassiveVal");
        if (evokeBody?.Incomplete == true)
            gaps.Add("EvokeVal");

        var passiveScales = passiveBody is { Incomplete: false }
            && passiveBody.Calls.Any(s => s.Call.Method.Name == "ModifyOrbValue");
        var evokeScales = evokeBody is { Incomplete: false }
            && (evokeBody.Calls.Any(s => s.Call.Method.Name == "ModifyOrbValue")
                || (passiveScales && evokeBody.Calls.Any(s => s.Call.Method.Name == "get_PassiveVal")));

        var focusClause = (passiveScales, evokeScales) switch
        {
            (true, true) => "Both Passive and Evoke scale with Focus.",
            (true, false) => "Passive scales with Focus; Evoke value is accumulated/fixed (not directly re-scaled by Focus).",
            (false, true) => "Evoke scales with Focus; Passive is unaffected by Focus.",
            (false, false) => "Unaffected by Focus.",
        };

        var timingClause = type.GetMethod("AfterTurnStartOrbTrigger", DeclaredInstance) is not null
            ? "Passive triggers at start of owner's turn."
            : type.GetMethod("BeforeTurnEndOrbTrigger", DeclaredInstance) is not null
                ? "Passive triggers before owner's turn ends."
                : "";

        var effectClause = "";
        if (type.GetMethod("Evoke", DeclaredInstance) is { } evokeMethod)
        {
            var evBody = NativeRuleBody.Read(evokeMethod);
            if (evBody.Incomplete)
            {
                gaps.Add("Evoke");
            }
            else if (evBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.GainEnergy)))
                effectClause = "Grants Energy on passive/evoke.";
            else if (evBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock)))
                effectClause = "Grants unpowered Block on passive/evoke.";
            else if (evBody.Calls.Any(s => s.Call.Method.Name == "ApplyLightningDamage"))
                effectClause = "Deals unpowered orb damage to a random hittable enemy (respecting target override/Lock-On when present).";
            else if (evBody.Calls.Any(s => s.Call.Method.Name == "MinBy" || s.Call.Method.Name == "OrderBy"))
                effectClause = "Passive increases this orb's stored Evoke damage; Evoke deals unpowered damage to the lowest-HP hittable enemy.";
            else if (evBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Damage)))
            {
                var passiveFloorZero = type.GetMethod("Passive", DeclaredInstance) is { } passiveMethod
                    && NativeRuleBody.Read(passiveMethod) is { Incomplete: false } passiveMethodBody
                    && passiveMethodBody.Calls.Any(s => s.Call.Method.Name == nameof(Math.Max));
                effectClause = passiveFloorZero
                    ? "Deals unpowered orb damage to all hittable enemies; when effective Passive value is > 0, triggers and decreases passive base value by 1 (minimum base 0; Focus still modifies Passive and Evoke; if effective Passive value is ≤ 0, neither damages nor decreases base value)."
                    : "Deals unpowered orb damage to all hittable enemies.";
            }
            else
                gaps.Add("Evoke");
        }

        var parts = new[] { timingClause, focusClause, effectClause }.Where(s => s.Length > 0);
        return (gaps.Count == 0 ? string.Join(" ", parts) : null, gaps);
    }

    private static decimal? ReadDecimalLiteral(Value value) => value switch
    {
        Constant { Data: int i } => i,
        Constant { Data: decimal d } => d,
        Field { Member: { Name: "Zero", DeclaringType: var zt } } when zt == typeof(decimal) => 0m,
        Field { Member: { Name: "One", DeclaringType: var ot } } when ot == typeof(decimal) => 1m,
        Field { Member: { Name: "MinusOne", DeclaringType: var mt } } when mt == typeof(decimal) => -1m,
        Call { Method: ConstructorInfo { DeclaringType: var t }, Arguments: [Constant { Data: int lo }, Constant { Data: int mid },
            Constant { Data: int hi }, Constant { Data: int neg }, Constant { Data: int scale }] } when t == typeof(decimal)
            => new decimal(lo, mid, hi, neg != 0, (byte)scale),
        Call { Method: ConstructorInfo { DeclaringType: var t }, Arguments: [Constant { Data: int val }] } when t == typeof(decimal)
            => val,
        _ => null,
    };
}
