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

    public static string? ReadEnchantment(EnchantmentModel enchantment, string language)
    {
        var key = new SimpleCacheKey(enchantment.Id.ToString(), language);
        if (EnchantmentCache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            text = DescribeEnchantment(enchantment.GetType());
        }
        catch
        {
            text = null;
        }

        EnchantmentCache[key] = text;
        return text;
    }

    public static string? ReadAffliction(AfflictionModel affliction, string language)
    {
        var key = new SimpleCacheKey(affliction.Id.ToString(), language);
        if (AfflictionCache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            text = DescribeAffliction(affliction.GetType());
        }
        catch
        {
            text = null;
        }

        AfflictionCache[key] = text;
        return text;
    }

    public static string? ReadOrb(OrbModel orb, string language)
    {
        var key = new SimpleCacheKey(orb.Id.ToString(), language);
        if (OrbCache.TryGetValue(key, out var cached))
            return cached;

        string? text = null;
        try
        {
            text = DescribeOrb(orb.GetType());
        }
        catch
        {
            text = null;
        }

        OrbCache[key] = text;
        return text;
    }

    internal static string? DescribeEnchantment(Type type)
    {
        if (!typeof(EnchantmentModel).IsAssignableFrom(type) || type.IsAbstract)
            return null;

        var parts = new List<string>();

        if (DescribeEnchantmentEligibility(type) is { } elig)
            parts.Add(elig);

        var disablesAfterPlay = type.GetMethod("AfterCardPlayed", DeclaredInstance) is { } afterPlay
            && NativeRuleBody.Read(afterPlay) is { Incomplete: false } afterBody
            && afterBody.Calls.Any(s => s.Call.Method.Name == "set_Status"
                && s.Call.Arguments is [Constant { Data: int statusVal }] && statusVal != 0);

        if (type.GetMethod("EnchantDamageAdditive", DeclaredInstance) is { } dmgAdd
            && NativeRuleBody.Read(dmgAdd) is { Incomplete: false } dmgAddBody)
        {
            var poweredOnly = dmgAddBody.Returns.Any(r => r.Conditions.Any(c => c.Value is Call { Method.Name: "IsPoweredAttack" }));
            var scope = poweredOnly ? "powered attack damage per hit" : "attack damage per hit";
            var suffix = disablesAfterPlay ? " (first play each combat only)" : "";
            parts.Add($"Adds +amount {scope}{suffix}.");
        }

        if (type.GetMethod("EnchantDamageMultiplicative", DeclaredInstance) is { } dmgMult
            && NativeRuleBody.Read(dmgMult) is { Incomplete: false } dmgMultBody)
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
        }

        if (type.GetMethod("EnchantBlockAdditive", DeclaredInstance) is { } blkAdd
            && NativeRuleBody.Read(blkAdd) is { Incomplete: false })
        {
            parts.Add("Adds +amount Block (before multipliers).");
        }

        if (type.GetMethod("EnchantPlayCount", DeclaredInstance) is { } playCnt
            && NativeRuleBody.Read(playCnt) is { Incomplete: false } playCntBody)
        {
            var addsOne = playCntBody.Returns.Any(r => r.Value is Operation { Operator: "add", Right: Constant { Data: 1 } });
            if (addsOne)
            {
                var suffix = disablesAfterPlay ? " on its first play each combat" : " every time it is played";
                parts.Add($"Replays this card 1 additional time{suffix}.");
            }
        }

        if (type.GetMethod("OnPlay", DeclaredInstance) is { } onPlay
            && NativeRuleBody.Read(onPlay) is { Incomplete: false } onPlayBody)
        {
            var oncePrefix = disablesAfterPlay ? "First play each combat: " : "On play: ";
            if (onPlayBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardPileCmd) && s.Call.Method.Name == nameof(CardPileCmd.Draw)))
                parts.Add($"{oncePrefix}draws amount card(s).");
            if (onPlayBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.GainEnergy)))
                parts.Add($"{oncePrefix}gains amount Energy.");
            if (onPlayBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock)))
                parts.Add($"{oncePrefix}gains amount unpowered Block.");
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
            if (onPlayBody.Calls.Any(s => s.Call.Method.Name == "set_Amount" || s.Call.Method.Name == "UpgradeValueBy"))
                parts.Add("Increases this enchantment's bonus each time this card is played this combat.");
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
            if (enchBody.Calls.Any(s => s.Call.Method.Name == "WithBaseCost" || s.Call.Method.Name == "SetWithModifiers" || s.Call.Method.Name == "AddModifier" || s.Call.Method.Name == "ChangeBaseCost"))
                parts.Add("Permanently modifies the enchanted card's Energy cost.");
        }

        if (type.GetMethod("AfterAutoPrePlayPhaseEntered", DeclaredInstance) is { } autoPre
            && NativeRuleBody.Read(autoPre) is { Incomplete: false } autoPreBody
            && autoPreBody.Calls.Any(s => s.Call.Method.Name == "AutoPlay"))
        {
            parts.Add("Auto-plays this card automatically at the start of turn 1 before normal card play.");
        }

        if (type.GetMethod("BeforeFlush", DeclaredInstance) is { } beforeFlush
            && NativeRuleBody.Read(beforeFlush) is { Incomplete: false } flushBody
            && flushBody.Calls.Any(s => s.Call.Method.Name == "AddThisCombat" || s.Call.Method.Name == "AddThisTurn"))
        {
            parts.Add("If still in hand before end-of-turn discard, reduces this card's Energy cost for this combat.");
        }

        if (type.GetMethod("AfterCardDrawn", DeclaredInstance) is { } afterDraw
            && NativeRuleBody.Read(afterDraw) is { Incomplete: false } drawBody
            && drawBody.Calls.Any(s => s.Call.Method.Name == "SetThisTurn" || s.Call.Method.Name == "SetThisCombat"))
        {
            parts.Add("Randomizes this card's Energy cost when drawn.");
        }

        if (type.GetMethod("ModifyShuffleOrder", DeclaredInstance) is not null)
        {
            parts.Add("Places this card at the top of the draw pile whenever the draw pile is shuffled.");
        }

        return parts.Count > 0 ? string.Join(" ", parts) : null;
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
    {
        if (!typeof(AfflictionModel).IsAssignableFrom(type) || type.IsAbstract)
            return null;

        var parts = new List<string>();
        if (type.GetMethod("AfterCardEnteredCombat", DeclaredInstance) is { } entered
            && NativeRuleBody.Read(entered) is { Incomplete: false } enteredBody
            && enteredBody.Calls.Any(s => s.Call.Method.Name == "AddKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw)))
        {
            var kws = enteredBody.Calls
                .Where(s => s.Call.Method.Name == "AddKeyword" && s.Call.Arguments is [Constant { Data: int kw }] && Enum.IsDefined(typeof(CardKeyword), kw))
                .Select(s => ((CardKeyword)(int)((Constant)s.Call.Arguments[0]).Data!).ToString())
                .Distinct(StringComparer.Ordinal);
            parts.Add($"Grants {string.Join(", ", kws)} to the afflicted card in combat.");
        }

        if (type.GetMethod("CanAfflictCardType", DeclaredInstance) is { } canType
            && NativeRuleBody.Read(canType) is { Incomplete: false } canBody)
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

        return parts.Count > 0 ? string.Join(" ", parts) : null;
    }

    internal static string? DescribeOrb(Type type)
    {
        if (!typeof(OrbModel).IsAssignableFrom(type) || type.IsAbstract)
            return null;

        var passiveProp = type.GetProperty("PassiveVal", DeclaredInstance)?.GetMethod;
        var evokeProp = type.GetProperty("EvokeVal", DeclaredInstance)?.GetMethod;
        var passiveScales = passiveProp is not null
            && NativeRuleBody.Read(passiveProp) is { Incomplete: false } pBody
            && pBody.Calls.Any(s => s.Call.Method.Name == "ModifyOrbValue");
        var evokeScales = evokeProp is not null
            && NativeRuleBody.Read(evokeProp) is { Incomplete: false } eBody
            && (eBody.Calls.Any(s => s.Call.Method.Name == "ModifyOrbValue")
                || (passiveScales && eBody.Calls.Any(s => s.Call.Method.Name == "get_PassiveVal")));

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
        if (type.GetMethod("Evoke", DeclaredInstance) is { } evokeMethod
            && NativeRuleBody.Read(evokeMethod) is { Incomplete: false } evBody)
        {
            if (evBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PlayerCmd) && s.Call.Method.Name == nameof(PlayerCmd.GainEnergy)))
                effectClause = "Grants Energy on passive/evoke.";
            else if (evBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.GainBlock)))
                effectClause = "Grants unpowered Block on passive/evoke.";
            else if (evBody.Calls.Any(s => s.Call.Method.Name == "ApplyLightningDamage"))
                effectClause = "Deals unpowered orb damage to a random hittable enemy (respecting target override/Lock-On when present).";
            else if (evBody.Calls.Any(s => s.Call.Method.Name == "MinBy" || s.Call.Method.Name == "OrderBy"))
                effectClause = "Passive increases this orb's stored Evoke damage; Evoke deals unpowered damage to the lowest-HP hittable enemy.";
            else if (evBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CreatureCmd) && s.Call.Method.Name == nameof(CreatureCmd.Damage)))
                effectClause = "Deals unpowered orb damage to all hittable enemies; passive value decreases by 1 after each trigger.";
        }

        var parts = new[] { timingClause, focusClause, effectClause }.Where(s => s.Length > 0);
        return string.Join(" ", parts);
    }

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
