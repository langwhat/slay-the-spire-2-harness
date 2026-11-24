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

        if (DescribeEnchantmentEligibility(type) is { } elig)
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
                var addsOne = playCntBody.Returns.Any(r => r.Value is Operation { Operator: "add", Right: Constant { Data: 1 } });
                if (addsOne)
                {
                    var suffix = disablesAfterPlay ? " on its first play each combat" : " every time it is played";
                    parts.Add($"Replays this card 1 additional time{suffix}.");
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

        if (type.GetMethod("OnEnchant", DeclaredInstance) is { } onEnchant
            && NativeRuleBody.Read(onEnchant) is { Incomplete: false } enchBody)
        {
            if (enchBody.Calls.Any(s => s.Call.Method.Name == "AddKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw)))
            {
                var kws = enchBody.Calls
                    .Where(s => s.Call.Method.Name == "AddKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw))
                    .Select(s => ((CardKeyword)(int)((Constant)s.Call.Arguments[0]).Data!).ToString())
                    .Distinct(StringComparer.Ordinal);
                parts.Add($"Grants keyword(s): {string.Join(", ", kws)}.");
            }
            if (enchBody.Calls.Any(s => s.Call.Method.Name == "RemoveKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw)))
            {
                var kws = enchBody.Calls
                    .Where(s => s.Call.Method.Name == "RemoveKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw))
                    .Select(s => ((CardKeyword)(int)((Constant)s.Call.Arguments[0]).Data!).ToString())
                    .Distinct(StringComparer.Ordinal);
                parts.Add($"Removes keyword(s): {string.Join(", ", kws)}.");
            }
            if (enchBody.Calls.Any(s => s.Call.Method.Name == "WithBaseCost" || s.Call.Method.Name == "SetWithModifiers" || s.Call.Method.Name == "AddModifier" || s.Call.Method.Name == "ChangeBaseCost" || s.Call.Method.Name == "UpgradeBy"))
                parts.Add("Permanently modifies the enchanted card's Energy cost.");
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
            else if (drawBody.Calls.Any(s => s.Call.Method.Name == "SetThisTurn" || s.Call.Method.Name == "SetThisCombat"))
            {
                parts.Add("Randomizes this card's Energy cost when drawn.");
            }
            else
            {
                gaps.Add("AfterCardDrawn");
            }
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

        RecordUnhandledModifierHooks(type, typeof(EnchantmentModel), HandledEnchantmentHooks, gaps);

        return (parts.Count > 0 ? string.Join(" ", parts) : null, gaps);
    }

    private static readonly HashSet<string> HandledEnchantmentHooks = new(StringComparer.Ordinal)
    {
        "CanEnchantCardType", "CanEnchant", "AfterCardPlayed", "EnchantDamageAdditive", "EnchantDamageMultiplicative",
        "EnchantBlockAdditive", "EnchantPlayCount", "OnPlay", "OnEnchant", "AfterAutoPrePlayPhaseEntered",
        "BeforeFlush", "AfterCardDrawn", "ModifyShuffleOrder", "RecalculateValues",
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

    private static string? DescribeEnchantmentEligibility(Type type)
    {
        if (type.GetMethod("CanEnchantCardType", DeclaredInstance) is { } byType
            && NativeRuleBody.Read(byType) is { Incomplete: false } body)
        {
            var types = body.Returns
                .SelectMany(r => r.Value is Operation { Operator: "ceq", Right: Constant { Data: int ct } } && Enum.IsDefined(typeof(CardType), ct)
                    ? [(CardType)ct]
                    : Array.Empty<CardType>())
                .Distinct()
                .ToArray();
            if (types.Length > 0)
                return $"Eligible cards: {string.Join("/", types)} only.";
        }
        if (type.GetMethod("CanEnchant", DeclaredInstance) is { } custom
            && NativeRuleBody.Read(custom) is { Incomplete: false } customBody)
        {
            if (customBody.Calls.Any(s => s.Call.Method.Name == "get_GainsBlock"))
                return "Eligible cards: cards that gain Block.";
        }
        return null;
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
                    ? "Deals unpowered orb damage to all hittable enemies; passive base value decreases by 1 after each trigger (minimum base 0; Focus still modifies Passive and Evoke)."
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
