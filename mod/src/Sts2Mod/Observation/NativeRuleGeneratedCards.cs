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
        var tags = card.Tags.OrderBy(t => t.ToString(), StringComparer.Ordinal).ToList();
        if (tags.Count > 0)
            parts.Add($"tags: {string.Join(", ", tags)}");

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
        var chainedAttackCalls = new HashSet<Call>(ReferenceEqualityComparer.Instance);
        var seenAttackRoots = new Dictionary<Call, (int Epoch, int Index)>(ReferenceEqualityComparer.Instance);
        foreach (var step in body.Calls)
        {
            if (step.Call.Method.DeclaringType?.Name == "AttackCommand" && step.Call.Method.Name == "Execute")
            {
                Call? root = null;
                for (var cur = step.Call.Receiver as Call; cur is not null; cur = cur.Receiver as Call)
                {
                    chainedAttackCalls.Add(cur);
                    root = cur;
                }
                // AttackCommand is mutable; fail closed if two distinct Execute instructions reuse the same DamageCmd.Attack instance.
                if (root is not null)
                {
                    var loc = (step.Epoch, step.Index);
                    if (seenAttackRoots.TryGetValue(root, out var prevLoc) && prevLoc != loc)
                        return null;
                    seenAttackRoots[root] = loc;
                }
            }
        }
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
            if (active.All(s => s.Call.Method == call.Method)
                && (IsPresentationCall(call.Method)
                    || declaring?.Namespace is "System.Runtime.CompilerServices" or "System.Threading.Tasks"
                    || (declaring == typeof(ArgumentNullException) && name == nameof(ArgumentNullException.ThrowIfNull))
                    || (declaring == typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty) && name == "CheckMissingWithAnim")
                    || ((declaring == typeof(DamageCmd) || (declaring?.Name == "AttackCommand" && name is not "Execute"))
                        && (active.All(s => chainedAttackCalls.Contains(s.Call))
                            || name is "WithHitFx" or "WithAttackerFx" or "WithAttackerAnim" or "WithNoAttackerAnim"
                                or "WithWaitBeforeHit" or "WithHitVfxNode" or "WithHitVfxSpawnedAtBase"
                                or "SpawningHitVfxOnEachCreature" or "OnlyPlayAnimOnce"))
                    || (declaring == typeof(decimal) && name == "op_Implicit")
                    || name is "get_Owner" or "get_Creature" or "get_Osty" or "get_DynamicVars" or "get_Damage"
                        or "get_OstyDamage" or "get_HpLoss" or "get_Cards" or "get_Energy" or "get_BaseValue"
                        or "get_IntValue" or "get_EnergyCost" or "get_Character" or "get_Target"
                        or "get_CombatState" or "get_Enemies" or "get_HittableEnemies" or "get_Powers"
                        or "OfType" or "FirstOrDefault" or "LastOrDefault"
                    || call.Method is ConstructorInfo))
                continue;
            if (active.All(s => s.Call.Method == call.Method)
                && declaring == card.GetType() && call.Method is MethodInfo cardHelper
                && (IsPureCardLookupHelper(cardHelper) || CardOwnerPowerCheck(card.GetType(), call) is not null))
                continue;

            if (active.Length > 1 && active.Select(s => Key(s.Call)).Distinct(StringComparer.Ordinal).Count() > 1)
            {
                if (DescribeConditionalAoeCardAttack(card, active) is { } aoeDesc)
                {
                    effects.Add(aoeDesc);
                    continue;
                }
                return null;
            }

            var reaching = active.Select(s => s.Conditions).ToArray();
            var missing = body.Terminals.Where(t => !t.Visited.Contains(group.Key)).Select(t => t.Conditions);
            var minimized = (NativeRuleGuards.Minimize(reaching, missing, TestKey) ?? MergeTests(reaching)).ToArray();
            var isTargetedEnemyPowerGuard = declaring == typeof(PowerCmd) && name == nameof(PowerCmd.ModifyAmount)
                && call.Arguments.Length >= 2 && EnemyPowerTargetingCardOwner(card.GetType(), call.Arguments[1]) is not null
                && minimized.Length > 0
                && minimized.All(path => path.Any(c => IsEnemyPowerPresenceCheck(card.GetType(), c))
                    && path.All(c => IsDreadTest(c) || IsEnemyPowerPresenceCheck(card.GetType(), c)));
            var isOstyPresentGuard = declaring?.Name == "AttackCommand" && name == "Execute"
                && minimized.Length > 0
                && minimized.All(path => path.Any(IsOstyMissingCheck)
                    && path.All(c => IsDreadTest(c) || IsOstyMissingCheck(c)));
            if (!isTargetedEnemyPowerGuard && !isOstyPresentGuard
                && minimized.Any(path => path.Any(c => !IsDreadTest(c) && Condition(c) is not "")))
                return null;

            static bool IsOstyMissingCheck(Test c)
                => c is { Expected: false, Value: Call { Method.DeclaringType: var ostyDecl, Method.Name: "CheckMissingWithAnim" } }
                    && ostyDecl == typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty);

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
            if (declaring == typeof(PlayerCmd) && name == nameof(PlayerCmd.GainEnergy))
            {
                if (call.Arguments.Length < 1 || CardNumeric(card, call.Arguments[0]) is not { } energy)
                    return null;
                effects.Add($"gain {Number(energy)} Energy");
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
        Value? hitCountArg = null;
        var fromOsty = false;
        var unpowered = false;
        var unblockable = false;
        string? targetText = null;
        for (var c = receiver; ; c = (Call)c.Receiver)
        {
            var name = c.Method.Name;
            if (c.Method.DeclaringType == typeof(DamageCmd) && name == "Attack")
                amount = c.Arguments.FirstOrDefault();
            else if (name == "WithHitCount" && c.Arguments.Length != 0)
                hitCountArg ??= c.Arguments[0];
            else if (name == "FromOsty")
                fromOsty = true;
            else if (name is "UnpoweredAttack" or "Unpowered")
                unpowered = true;
            else if (name == "Unblockable")
                unblockable = true;
            else if (name == "TargetingRandomOpponents")
                targetText ??= "a random enemy";
            else if (name == "TargetingAllOpponents")
                targetText ??= "all enemies";
            else if (name == "Targeting" && c.Arguments is [Call { Method.Name: "get_Target" }])
                targetText ??= "target enemy";
            else if (name is not ("FromCard" or "FromCardUnpowered" or "FromPlayer" or "FromMonster"
                or "WithHitFx" or "WithAttackerFx" or "WithAttackerAnim" or "WithNoAttackerAnim"
                or "WithWaitBeforeHit" or "WithHitVfxNode" or "WithHitVfxSpawnedAtBase"
                or "SpawningHitVfxOnEachCreature" or "OnlyPlayAnimOnce" or "BeforeDamage" or "AfterDamage"))
                return null;
            else if (name is "BeforeDamage" or "AfterDamage")
                return null;
            else if (name == "FromCardUnpowered")
                unpowered = true;
            if (c.Receiver is not Call)
                break;
        }
        if (amount is null || targetText is null || CardNumeric(card, amount) is not { } dmg)
            return null;
        decimal hits = 1m;
        if (hitCountArg is not null)
        {
            if (CardNumeric(card, hitCountArg) is not { } parsedHits)
                return null;
            hits = parsedHits;
        }
        var dmgText = hits == 1m ? Number(dmg) : $"{Number(dmg)}×{Number(hits)}";
        var props = (unpowered ? ValueProp.Unpowered : (ValueProp)0) | (unblockable ? ValueProp.Unblockable : (ValueProp)0);
        var attackerPrefix = fromOsty
            ? $"if {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} is alive: {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} deals "
            : requiresOsty
                ? $"if {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} is alive: deal "
                : "deal ";
        return $"{attackerPrefix}{dmgText}{DamageFlags(props)} damage to {targetText}";
    }

    private string? DescribeConditionalAoeCardAttack(CardModel card, Step[] active)
    {
        if (!active.All(s => s.Call.Method.DeclaringType?.Name == "AttackCommand"
                && s.Call.Method.Name == "Execute" && s.Call.Receiver is Call))
            return null;

        var byPower = new List<(string Desc, Type Power, bool Expected)>();
        foreach (var step in active)
        {
            if (DescribeCardAttackCommand(card, (Call)step.Call.Receiver!, requiresOsty: false) is not { } desc)
                return null;
            Type? gatePower = null;
            bool? gateExpected = null;
            foreach (var cond in step.Conditions)
            {
                if (IsDreadTest(cond) || Condition(cond) == "")
                    continue;
                // Ignore pure presentation character checks such as `Owner.Character is Silent`.
                if (cond.Value is Operation { Operator: "isinst", Left: Call { Method.Name: "get_Character" } }
                    || cond.Value is Operation { Operator: "cgt.un", Left: Operation { Operator: "isinst", Left: Call { Method.Name: "get_Character" } } })
                    continue;
                if (CardOwnerPowerCheck(card.GetType(), cond.Value) is { } pt)
                {
                    if (gatePower is not null && (gatePower != pt || gateExpected != cond.Expected))
                        return null;
                    gatePower = pt;
                    gateExpected = cond.Expected;
                    continue;
                }
                return null;
            }
            if (gatePower is null || gateExpected is null)
                return null;
            if (!byPower.Any(x => x.Desc == desc && x.Power == gatePower && x.Expected == gateExpected.Value))
                byPower.Add((desc, gatePower, gateExpected.Value));
        }

        if (byPower.Count != 2 || byPower[0].Power != byPower[1].Power || byPower[0].Expected == byPower[1].Expected)
            return null;
        var withPower = byPower.First(x => x.Expected);
        var withoutPower = byPower.First(x => !x.Expected);
        return $"{withoutPower.Desc} (or if owner has {PowerLabel(withPower.Power)}: {withPower.Desc})";
    }

    private Type? CardOwnerPowerCheck(Type cardType, Value value)
    {
        if (value is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && getter.DeclaringType == cardType && getter.ReturnType == typeof(bool) && !getter.ContainsGenericParameters)
        {
            var body = Body(getter);
            if (!body.Incomplete)
            {
                var nonFalse = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
                if (nonFalse is [{ Value: var ret }])
                    return CardOwnerPowerCheck(cardType, ret);
            }
        }
        if (value is Call { Method: { Name: "HasPower", IsGenericMethod: true } hp, Receiver: { } rec }
            && hp.DeclaringType == typeof(Creature) && IsCardOwnerCreature(rec))
            return hp.GetGenericArguments()[0];
        return null;
    }

    private decimal? CardNumeric(CardModel card, Value value) => value switch
    {
        Call { Method.Name: "op_Implicit", Arguments: [var inner] } => CardNumeric(card, inner),
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_HpLoss" } }
            when card.DynamicVars.TryGetValue("HpLoss", out var hp) => hp.BaseValue,
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_OstyDamage" } }
            when card.DynamicVars.TryGetValue("OstyDamage", out var ostyDmg) => ostyDmg.BaseValue,
        Call { Method.Name: "get_BaseValue", Receiver: Call { Method.Name: "get_Cards" } }
            when card.DynamicVars.TryGetValue("Cards", out var cards) => cards.BaseValue,
        Call { Method.Name: "get_BaseValue" or "get_IntValue", Receiver: Call { Method.Name: "get_Energy" } }
            when card.DynamicVars.TryGetValue("Energy", out var energy) => energy.BaseValue,
        Call { Method.Name: "get_BaseValue" or "get_IntValue", Receiver: var dyn }
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
