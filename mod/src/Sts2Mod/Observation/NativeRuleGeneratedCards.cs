using System.Reflection;
using System.Runtime.CompilerServices;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.ValueProps;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    internal void ReadGeneratedCards(List<string> lines)
    {
        foreach (var cardType in _generatedCards.OrderBy(type => ModelDb.GetId(type).ToString(), StringComparer.Ordinal))
        {
            if (DescribeGeneratedCard(cardType) is { } summary)
                lines.Add($"{ModelLabel(cardType)} · {summary}");
            else
                Gap($"{cardType.Name}/generated card rule");
        }
    }

    private string? DescribeGeneratedCard(Type cardType)
    {
        if (ModelDb.GetByIdOrNull<CardModel>(ModelDb.GetId(cardType)) is not { } card)
            return null;
        if (HasUnhandledCardOverrides(cardType))
            return null;

        var parts = new List<string> { card.Type.ToString() };
        var keywords = card.CanonicalKeywords.OrderBy(k => k.ToString(), StringComparer.Ordinal).ToList();
        var unplayable = keywords.Contains(CardKeyword.Unplayable) || card.EnergyCost.Canonical < 0;
        parts.Add(unplayable ? "Unplayable" : $"{card.EnergyCost.Canonical} Energy");
        foreach (var keyword in keywords.Where(k => k != CardKeyword.Unplayable))
            parts.Add(keyword.ToString());

        var clauses = new List<string> { string.Join(", ", parts) };
        if (card.HasTurnEndInHandEffect)
        {
            if (FindCardOverride(cardType, "OnTurnEndInHand") is not { } turnEnd
                || ReadCardMethod(card, turnEnd) is not { } turnEndEffects || turnEndEffects.Count == 0)
                return null;
            clauses.Add($"at end of turn in hand: {string.Join(", ", turnEndEffects)}");
        }
        else if (FindCardOverride(cardType, "OnTurnEndInHand") is not null)
        {
            return null;
        }

        if (FindCardOverride(cardType, "OnPlay") is { } onPlay)
        {
            if (unplayable || ReadCardMethod(card, onPlay) is not { } playEffects)
                return null;
            if (playEffects.Count > 0)
                clauses.Add($"on play: {string.Join(", ", playEffects)}");
        }
        return string.Join("; ", clauses);
    }

    private static bool HasUnhandledCardOverrides(Type cardType)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
        foreach (var method in cardType.GetMethods(flags))
        {
            if (!method.IsVirtual || method.GetBaseDefinition().DeclaringType == cardType)
                continue;
            if (method.Name is "OnTurnEndInHand" or "OnPlay" or "OnUpgrade" or "FakeUpgrade"
                or "SetupSkins" or "BeforeAnimPlayedInCompendium"
                or "get_CanonicalKeywords" or "get_CanonicalTags" or "get_CanonicalVars" or "get_HasTurnEndInHandEffect"
                or "get_CanonicalEnergyCost" or "get_CanonicalStarCost" or "get_MaxUpgradeLevel"
                or "get_MultiplayerConstraint" or "get_TargetType" or "get_Rarity" or "get_Type"
                or "get_ExtraHoverTips" or "get_GainsBlock" or "get_Title"
                or "get_HasBuiltInOverlay" or "get_CanBeGeneratedInCombat" or "get_CanBeUpgraded"
                or "get_ShouldGlowRedInternal" or "get_ShouldGlowGoldInternal"
                || method.Name.Contains("Portrait", StringComparison.Ordinal)
                || method.Name.Contains("AssetPath", StringComparison.Ordinal))
                continue;
            return true;
        }
        return false;
    }

    private static MethodInfo? FindCardOverride(Type cardType, string name)
        => cardType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly)
            .FirstOrDefault(method => method.Name == name && method.IsVirtual);

    private List<string>? ReadCardMethod(CardModel card, MethodInfo method)
    {
        var body = Body(method);
        if (body.Incomplete || body.Writes.Count > 0 || body.Loops.Count > 0
            || body.FieldSteps.Any(step => step.Assignment.Target.Member.DeclaringType?.IsDefined(typeof(CompilerGeneratedAttribute), false) != true))
            return null;
        var effects = new List<string>();
        foreach (var group in body.Calls.GroupBy(step => (step.Epoch, step.Index))
            .OrderBy(group => (group.Key.Epoch, group.Max(s => s.Order), group.Key.Index)))
        {
            var active = group.Where(s => s.Conditions.All(IsCurrentRun)).ToArray();
            if (active.Length == 0)
                continue;

            var call = active[0].Call;
            var declaring = call.Method.DeclaringType;
            var name = call.Method.Name;
            if (IsPresentationCall(call.Method)
                || declaring?.Namespace is "System.Runtime.CompilerServices" or "System.Threading.Tasks"
                || (declaring == typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty) && name == "CheckMissingWithAnim")
                || declaring == typeof(DamageCmd)
                || declaring?.Name == "AttackCommand" && name is not "Execute"
                || name is "get_Owner" or "get_Creature" or "get_Osty" or "get_DynamicVars" or "get_Damage"
                    or "get_OstyDamage" or "get_HpLoss" or "get_Cards" or "get_BaseValue" or "get_EnergyCost"
                    or "get_CombatState" or "get_Enemies" or "get_Powers" or "OfType" or "FirstOrDefault"
                || call.Method is ConstructorInfo)
                continue;
            if (declaring == card.GetType() && call.Method is MethodInfo cardHelper && IsPureCardLookupHelper(cardHelper))
                continue;

            var reaching = active.Select(s => s.Conditions).ToArray();
            var missing = body.Terminals.Where(t => !t.Visited.Contains(group.Key)).Select(t => t.Conditions);
            var minimized = (NativeRuleGuards.Minimize(reaching, missing, TestKey) ?? MergeTests(reaching)).ToArray();
            var isTargetedEnemyPowerGuard = declaring == typeof(PowerCmd) && name == nameof(PowerCmd.ModifyAmount)
                && call.Arguments.Length >= 2 && EnemyPowerTargetingCardOwner(card.GetType(), call.Arguments[1]) is not null
                && minimized.All(path => path.All(c => IsDreadTest(c) || IsEnemyPowerPresenceCheck(card.GetType(), c)));
            var isOstyPresentGuard = declaring?.Name == "AttackCommand" && name == "Execute"
                && minimized.All(path => path.All(c => IsDreadTest(c)
                    || (c is { Expected: false, Value: Call { Method.DeclaringType: var ostyDecl, Method.Name: "CheckMissingWithAnim" } }
                        && ostyDecl == typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))));
            if (!isTargetedEnemyPowerGuard && !isOstyPresentGuard
                && minimized.Any(path => path.Any(c => !IsDreadTest(c) && Condition(c) is not "")))
                return null;

            if (declaring?.Name == "AttackCommand" && name == "Execute" && call.Receiver is Call attackBuilder)
            {
                if (DescribeCardAttackCommand(card, attackBuilder, isOstyPresentGuard) is not { } attackDesc)
                    return null;
                effects.Add(attackDesc);
                continue;
            }

            if (declaring == typeof(CreatureCmd) && name == nameof(CreatureCmd.Damage))
            {
                if (DescribeCardDamage(card, call) is not { } dmg)
                    return null;
                effects.Add(dmg);
                continue;
            }
            if (declaring == typeof(CardPileCmd) && name == nameof(CardPileCmd.Draw))
            {
                if (Argument(call, "count") is not { } countArg || CardNumeric(card, countArg) is not { } count)
                    return null;
                var n = Number(count);
                effects.Add($"draw {n} {(n == "1" ? "card" : "cards")}");
                continue;
            }
            if (declaring == typeof(PowerCmd) && name == nameof(PowerCmd.ModifyAmount))
            {
                if (call.Arguments.Length < 3 || CardNumeric(card, call.Arguments[2]) is not { } delta
                    || EnemyPowerTargetingCardOwner(card.GetType(), call.Arguments[1]) is not { } powerType)
                    return null;
                effects.Add($"if an enemy has {PowerLabel(powerType)} targeting card owner: change that {PowerLabel(powerType)} by {Number(delta)}");
                continue;
            }
            if (declaring == typeof(CardEnergyCost) && name == "AddThisCombat" && call.Arguments.Length >= 1
                && call.Receiver is Call { Method.Name: "get_EnergyCost", Receiver: Input { Name: "this" } }
                && CardNumeric(card, call.Arguments[0]) is { } costDelta)
            {
                effects.Add($"increase this card's Energy cost by {Number(costDelta)} this combat");
                continue;
            }
            return null;
        }
        return effects;
    }

    private bool IsPureCardLookupHelper(MethodInfo helper)
    {
        if (helper.ReturnType == typeof(void) || helper.ReturnType == typeof(Task) || helper.ContainsGenericParameters)
            return false;
        var body = Body(helper);
        if (body.Incomplete || body.Writes.Count > 0 || body.FieldSteps.Count > 0)
            return false;
        return body.Calls.All(s => IsBenignReadOrPlumbingCall(s.Call.Method) || s.Call.Method is ConstructorInfo);
    }

    private string? DescribeCardDamage(CardModel card, Call call)
    {
        if (Argument(call, "target", "targets") is not { } targetArg || !IsCardOwnerCreature(targetArg))
            return null;
        if (call.Arguments.FirstOrDefault(arg => arg is Call { Method.Name: "get_Damage" }) is not null
            && card.DynamicVars.TryGetValue("Damage", out var variable) && variable is DamageVar damageVar)
        {
            return $"deal {Number(damageVar.BaseValue)}{DamageFlags(damageVar.Props)} damage to card owner";
        }
        if (Argument(call, "amount") is { } amountArg)
        {
            if (CardNumeric(card, amountArg) is not { } amount)
                return null;
            var props = Argument(call, "props") is Constant { Data: int raw } ? (ValueProp)raw : (ValueProp)0;
            return $"deal {Number(amount)}{DamageFlags(props)} damage to card owner";
        }
        return null;
    }

    private static bool IsCardOwnerCreature(Value value) => value switch
    {
        Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Input { Name: "this" } } } => true,
        Call { Method.Name: "NewReadOnlySingleElementList" or ".ctor", Arguments: [var single] } => IsCardOwnerCreature(single),
        _ => false,
    };

    private string? DescribeCardAttackCommand(CardModel card, Call receiver, bool requiresOsty)
    {
        Value? amount = null;
        var fromOsty = false;
        string? targetText = null;
        for (var c = receiver; ; c = (Call)c.Receiver)
        {
            if (c.Method.DeclaringType == typeof(DamageCmd) && c.Method.Name == "Attack")
                amount = c.Arguments.FirstOrDefault();
            else if (c.Method.Name == "FromOsty")
                fromOsty = true;
            else if (c.Method.Name == "TargetingRandomOpponents")
                targetText = "a random enemy";
            else if (c.Method.Name == "TargetingAllOpponents")
                targetText = "all enemies";
            if (c.Receiver is not Call)
                break;
        }
        if (amount is null || targetText is null || CardNumeric(card, amount) is not { } dmg)
            return null;
        var attackerPrefix = fromOsty
            ? $"if {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} is alive: {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} deals "
            : requiresOsty
                ? $"if {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} is alive: deal "
                : "deal ";
        return $"{attackerPrefix}{Number(dmg)} damage to {targetText}";
    }

    private decimal? CardNumeric(CardModel card, Value value) => value switch
    {
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_HpLoss" } }
            when card.DynamicVars.TryGetValue("HpLoss", out var hp) => hp.BaseValue,
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_OstyDamage" } }
            when card.DynamicVars.TryGetValue("OstyDamage", out var ostyDmg) => ostyDmg.BaseValue,
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_Cards" } }
            when card.DynamicVars.TryGetValue("Cards", out var cards) => cards.BaseValue,
        Call { Method.Name: "get_BaseValue", Receiver: var dyn }
            when DynamicKey(dyn) is { } key && card.DynamicVars.TryGetValue(key, out var found) => found.BaseValue,
        _ => Numeric(value),
    };

    private bool IsEnemyPowerPresenceCheck(Type cardType, Test test)
        => test.Expected && (test.Value is Call { Method: MethodInfo helper } && helper.DeclaringType == cardType && IsPureCardLookupHelper(helper)
            || EnemyPowerTargetingCardOwner(cardType, test.Value) is not null);

    private Type? EnemyPowerTargetingCardOwner(Type cardType, Value value)
    {
        if (value is Call { Method: MethodInfo helper } && helper.DeclaringType == cardType && !helper.ContainsGenericParameters)
        {
            var body = Body(helper);
            if (body.Incomplete)
                return null;
            var retVal = body.Returns.FirstOrDefault()?.Value;
            if (retVal is not null)
                return EnemyPowerTargetingCardOwner(cardType, retVal);
        }
        if (value is Call { Method: { Name: "FirstOrDefault", IsGenericMethod: true } first, Arguments: var args }
            && typeof(PowerModel).IsAssignableFrom(first.GetGenericArguments()[0]))
        {
            var powerType = first.GetGenericArguments()[0];
            var searchesEnemies = ContainsMethodCall(args[0], "get_Enemies", cardType);
            var checksTargetOwner = args.Length >= 2 && args[1] is Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }
                && !pred.ContainsGenericParameters && Body(pred) is { Incomplete: false } predBody
                && predBody.Calls.Any(c => c.Call.Method.Name == "get_Target" && c.Call.Method.DeclaringType == typeof(PowerModel))
                && predBody.Calls.Any(c => c.Call.Method.Name == "get_Creature");
            return searchesEnemies && checksTargetOwner ? powerType : null;
        }
        return null;
    }

    private bool ContainsMethodCall(Value value, string methodName, Type cardType, int depth = 0)
    {
        if (depth > 3)
            return false;
        return value switch
        {
            Call call => call.Method.Name == methodName
                || (call.Method is MethodInfo helper && helper.DeclaringType == cardType && !helper.ContainsGenericParameters
                    && Body(helper) is { Incomplete: false } helperBody
                    && (helperBody.Calls.Any(s => s.Call.Method.Name == methodName)
                        || helperBody.Returns.Any(r => ContainsMethodCall(r.Value, methodName, cardType, depth + 1))))
                || (call.Receiver is { } rec && ContainsMethodCall(rec, methodName, cardType, depth + 1))
                || call.Arguments.Any(a => ContainsMethodCall(a, methodName, cardType, depth + 1)),
            Operation op => ContainsMethodCall(op.Left, methodName, cardType, depth + 1)
                || (op.Right is { } r && ContainsMethodCall(r, methodName, cardType, depth + 1)),
            _ => false,
        };
    }

    private static string DamageFlags(ValueProp props)
    {
        var unblockable = props.HasFlag(ValueProp.Unblockable);
        var unpowered = props.HasFlag(ValueProp.Unpowered);
        return (unblockable, unpowered) switch
        {
            (true, true) => " unblockable unpowered",
            (true, false) => " unblockable",
            (false, true) => " unpowered",
            _ => "",
        };
    }
}
