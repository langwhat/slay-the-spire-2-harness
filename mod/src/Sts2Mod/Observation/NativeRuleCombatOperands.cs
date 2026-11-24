using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands.Builders;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.ValueProps;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    // CreatureCmd.Damage delivers each result after block, even at zero HP loss.
    // It skips AfterDamageReceived when a hit killed its receiver and they are still dead.
    private const string AfterEachReceivedHit = "After each hit resolves (including fully blocked hits; skip a lethal hit if its target stays dead)";

    private Effect? ReadGeneratedCard(Call call)
    {
        var pileNum = Numeric(Argument(call, "newPileType"));
        if (pileNum is null)
            return null;
        var pile = (PileType)(int)pileNum;
        var position = Numeric(Argument(call, "position")) is { } rawPos ? (CardPilePosition)(int)rawPos : CardPilePosition.Bottom;
        var where = pile == PileType.Draw && position != CardPilePosition.Bottom
            ? $" ({position.ToString().ToLowerInvariant()} position)"
            : "";
        var pileLabel = $"{pile.ToString().ToLowerInvariant()} pile{where}";

        var cardArg = Argument(call, "card", "cards");
        if (cardArg is Call { Method: { Name: "CreateCard", IsGenericMethod: true } create, Arguments: [var player] }
            && create.DeclaringType == typeof(ICombatState) && typeof(CardModel).IsAssignableFrom(create.GetGenericArguments()[0])
            && ValueText(player) is { } recipient)
        {
            var cardType = create.GetGenericArguments()[0];
            return new Effect($"add 1 {ModelLabel(cardType)} to {Possessive(recipient)} {pileLabel}", AddedCard: cardType);
        }

        // Non-generic combatState.CreateCard(ModelDb.Card<T>(), player)
        if (cardArg is Call { Method: { Name: "CreateCard", IsGenericMethod: false } nonGenCreate,
                Arguments: [Call { Method: { Name: "Card", IsGenericMethod: true } dbCard }, var ngPlayer] }
            && nonGenCreate.DeclaringType == typeof(ICombatState)
            && dbCard.DeclaringType == typeof(ModelDb)
            && typeof(CardModel).IsAssignableFrom(dbCard.GetGenericArguments()[0])
            && ValueText(ngPlayer) is { } ngRecipient)
        {
            var cardType = dbCard.GetGenericArguments()[0];
            return new Effect($"add 1 {ModelLabel(cardType)} to {Possessive(ngRecipient)} {pileLabel}", AddedCard: cardType);
        }

        // card.CreateClone() on recorded card instance (e.g. NightmarePower)
        if (cardArg is Call { Method.Name: "CreateClone", Receiver: Field cloneSource }
            && CardSlot(cloneSource) is { } cloneSlot
            && Argument(call, "creator", "player") is { } clonePlayer
            && ValueText(clonePlayer) is { } cloneRecipient)
        {
            return new Effect($"add 1 clone of {cloneSlot} to {Possessive(cloneRecipient)} {pileLabel}");
        }

        // Static CardType.Create(player, count, combatState) (e.g. Soul.Create)
        if (cardArg is Call { Method: { Name: "Create", IsStatic: true, DeclaringType: { } createdCardType } } staticCreate
            && typeof(CardModel).IsAssignableFrom(createdCardType)
            && staticCreate.Arguments.Length >= 2
            && ValueText(staticCreate.Arguments[0]) is { } createdRecipient
            && ValueText(staticCreate.Arguments[1]) is { } createdCount)
        {
            return new Effect($"add {createdCount} {ModelLabel(createdCardType)} to {Possessive(createdRecipient)} {pileLabel}", AddedCard: createdCardType);
        }

        // CardFactory.GetDistinctForCombat / GetForCombat(player, pool, count, rng)
        var factoryCall = UnwrapCardFactoryCall(cardArg, out var takesSingle);
        if (factoryCall is { Method.DeclaringType.Name: "CardFactory", Method.Name: "GetDistinctForCombat" or "GetForCombat" }
            && factoryCall.Arguments.Length >= 3
            && ValueText(factoryCall.Arguments[0]) is { } factoryRecipient
            && (takesSingle ? "1" : ValueText(factoryCall.Arguments[2])) is { } factoryCount
            && DescribeCardFactoryPool(factoryCall.Arguments[1]) is { } poolDesc)
        {
            if (factoryCall.Method.Name == "GetDistinctForCombat")
                return new Effect($"generate up to {factoryCount} distinct {poolDesc} card(s) into {Possessive(factoryRecipient)} {pileLabel}");
            return new Effect($"generate {factoryCount} {poolDesc} card(s) (duplicates allowed) into {Possessive(factoryRecipient)} {pileLabel}");
        }

        return null;
    }

    private static Call? UnwrapCardFactoryCall(Value? value, out bool takesSingle)
    {
        takesSingle = false;
        switch (value)
        {
            case Call { Method.DeclaringType.Name: "CardFactory" } cf:
                return cf;
            case Call { Method.Name: "First" or "FirstOrDefault", Arguments: [var inner, ..] }:
                var foundSingle = UnwrapCardFactoryCall(inner, out _);
                takesSingle = foundSingle is not null;
                return foundSingle;
            case Call { Method.Name: "ToList" or "ToArray", Arguments: [var inner, ..] }:
                return UnwrapCardFactoryCall(inner, out takesSingle);
            case Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: var col } }:
                return UnwrapCardFactoryCall(col, out takesSingle);
            default:
                return null;
        }
    }

    private static Call? UnwrapCardFactoryCall(Value? value)
        => UnwrapCardFactoryCall(value, out _);

    private string? DescribeCardFactoryPool(Value poolExpr)
    {
        var filters = new List<string>();
        var current = poolExpr;
        while (current is Call { Method.Name: "ToList" or "ToArray", Arguments: [var inner] })
            current = inner;
        if (current is Call { Method.Name: "Where", Arguments: [var source, Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] })
        {
            var predParamName = pred.GetParameters().FirstOrDefault(p => typeof(CardModel).IsAssignableFrom(p.ParameterType))?.Name;
            if (predParamName is null)
                return null;
            var predBody = Body(pred);
            if (predBody.Incomplete || predBody.Returns.Count == 0)
                return null;
            var positive = predBody.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
            if (positive.Length != 1)
                return null;
            var tests = new List<Test>(positive[0].Conditions);
            if (!(TryTruth(positive[0].Value, out var rb) && rb))
                tests.Add(new Test(positive[0].Value, true));
            foreach (var raw in tests)
            {
                var expr = raw.Value;
                var expected = raw.Expected;
                while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
                {
                    expr = inner;
                    expected = !expected;
                }
                if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Call { Method.Name: "get_Type", Receiver: Input { Name: var typeRec } }, Right: Constant { Data: int rawType } } typeOp
                    && typeRec == predParamName && Enum.IsDefined(typeof(CardType), rawType))
                {
                    var isEq = typeOp.Operator == "bne.un" ? !expected : expected;
                    if (!isEq)
                        return null;
                    filters.Add(((CardType)rawType).ToString());
                    continue;
                }
                if (expr is Operation { Operator: "ceq" or "beq" or "bne.un", Left: Call { Method.Name: "get_Rarity", Receiver: Input { Name: var rarRec } }, Right: Constant { Data: int rawRarity } } rarOp
                    && rarRec == predParamName && Enum.IsDefined(typeof(CardRarity), rawRarity))
                {
                    var isEq = rarOp.Operator == "bne.un" ? !expected : expected;
                    if (!isEq)
                        return null;
                    filters.Add(((CardRarity)rawRarity).ToString());
                    continue;
                }
                if (expected && expr is Call { Method.Name: "Contains", Receiver: Call { Method.Name: "get_Keywords", Receiver: Input { Name: var kwRec } }, Arguments: [Constant { Data: int rawKw }] }
                    && kwRec == predParamName && Enum.IsDefined(typeof(CardKeyword), rawKw))
                {
                    filters.Add(KeywordLabel((CardKeyword)rawKw));
                    continue;
                }
                // Fail closed on any unrecognized pool filter (e.g., `!(c is JackOfAllTrades)` or non-candidate checks).
                return null;
            }
            current = source;
        }
        var poolName = current is Call { Method.Name: "GetUnlockedCards", Receiver: Call { Method: { Name: "CardPool", IsGenericMethod: true } cp } }
            ? HumanizeLower(cp.GetGenericArguments()[0].Name.Replace("CardPool", "", StringComparison.Ordinal))
            : current is Call { Method.Name: "GetUnlockedCards", Receiver: Call { Method.Name: "get_CardPool" } }
                ? "unlocked character"
                : null;
        if (poolName is null)
            return null;
        var parts = new List<string> { "combat-generatable", poolName };
        parts.AddRange(filters);
        return $"random {string.Join(" ", parts)}";
    }

    private static string? CardSlot(Field field)
        => field.Member.FieldType == typeof(CardModel)
            && field.Receiver is Call { Method: { Name: "GetInternalData", DeclaringType: var origin, IsGenericMethod: true } method,
                Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && origin == typeof(PowerModel) && method.GetGenericArguments()[0] == field.Member.DeclaringType
                ? "recorded card instance"
                : null;

    private string? PowerBoolSlot(Field field)
    {
        if (field.Member.FieldType != typeof(bool))
            return null;
        if (InternalDataField(field) is { } internalBool)
            return IsGameplayField(internalBool) ? UpperFirst(internalBool.Name.TrimStart('_')) : null;
        if (field.Receiver is Input { Name: "this" }
            && (typeof(PowerModel).IsAssignableFrom(field.Member.DeclaringType)
                || typeof(MonsterModel).IsAssignableFrom(field.Member.DeclaringType))
            && IsGameplayField(field.Member))
            return UpperFirst(field.Member.Name.TrimStart('_'));
        return null;
    }

    private static string UpperFirst(string name)
        => name.Length == 0 ? name : char.ToUpperInvariant(name[0]) + name[1..];

    private Effect? DescribeSlotAssignment(Assignment assignment, MethodInfo method)
    {
        if (CardSlot(assignment.Target) is not null)
        {
            return assignment.Assigned switch
            {
                Constant { Data: null } => new Effect("clear the recorded card instance",
                    Trigger: method.Name == "AfterCardPlayed" ? "After each card play fully resolves (after all its hits)" : null, ChangesGuard: true),
                Input { Name: "cardSource", Type: var type } when type == typeof(CardModel) && method.Name == "AfterDamageReceived"
                    => new Effect("record the source card instance", Trigger: AfterEachReceivedHit, ChangesGuard: true),
                _ => null,
            };
        }
        if (PowerBoolSlot(assignment.Target) is { } flagName)
        {
            return assignment.Assigned switch
            {
                Constant { Data: int flag } when flag is 0 or 1
                    => new Effect($"{flagName} = {(flag == 1 ? "true" : "false")}", ChangesGuard: true),
                Operation { Operator: "ceq", Left: Field toggled, Right: Constant { Data: 0 } }
                    when PowerBoolSlot(toggled) == flagName
                    => new Effect($"toggle {flagName} (starts false)", ChangesGuard: true),
                _ => null,
            };
        }
        return null;
    }

    private bool IsTrackedSlot(Field field)
        => CardSlot(field) is not null || PowerBoolSlot(field) is not null;

    private static bool IsUnchangedAssignment(EffectStep step)
        => step.Action is Assignment { Assigned: Input { Name: "cardSource" } source } assignment
            && step.Conditions.Any(test => test is { Expected: true, Value: Operation { Operator: "beq", Left: var left, Right: var right } }
                && (left == source && Key(right) == Key(assignment.Target) || right == source && Key(left) == Key(assignment.Target)));

    // Keep the original decision in scope when an effect writes to a value tested by it.
    private List<GuardedEffects> GroupGuardedChanges(List<GuardedEffects> effects, MethodInfo? currentMethod = null)
    {
        var groups = new List<GuardedEffects>();
        for (var index = 0; index < effects.Count;)
        {
            var first = effects[index++];
            if (first.Repetitions is not null || first.Guard.Length == 0 || first.Path.Length == 0
                || !first.Effects.Any(effect => effect.ChangesGuard))
            {
                groups.Add(first);
                continue;
            }
            var nested = new List<GuardedEffects> { first with { Path = [], Guard = "" } };
            while (index < effects.Count)
            {
                var candidate = effects[index];
                if (!first.Path.All(test => candidate.Path.Any(other => TestKey(test) == TestKey(other) && test.Expected == other.Expected)))
                    break;
                var tail = candidate.Path.Where(test => !first.Path.Any(other => TestKey(test) == TestKey(other) && test.Expected == other.Expected)).ToArray();
                if (Guard(tail) is not { } guard)
                    break;
                nested.Add(candidate with { Path = tail, Guard = guard });
                index++;
            }
            if (nested.Count == 1)
            {
                groups.Add(first);
                continue;
            }
            var steps = RenderBranches(nested).ToList();
            // Fold pure self-reentrancy guards (`if ... and Flag=false: (flag = true; body; flag = false)`)
            // only when no other method on the owning model writes that flag.
            if (steps.Count >= 3
                && steps[0].Text.EndsWith(" = true", StringComparison.Ordinal)
                && steps[^1].Text.EndsWith(" = false", StringComparison.Ordinal))
            {
                var flagHuman = steps[0].Text[..^" = true".Length];
                if (steps[^1].Text[..^" = false".Length] == flagHuman
                    && (currentMethod is null || IsSingleMethodReentrancyFlag(currentMethod, flagHuman)))
                {
                    var expectedGuardToken = flagHuman.Replace(" ", "", StringComparison.Ordinal) + "=false";
                    var matchingCond = first.Path.FirstOrDefault(t =>
                        Condition(t) is { } condText
                        && condText.Equals(expectedGuardToken, StringComparison.OrdinalIgnoreCase));
                    if (matchingCond is not null)
                    {
                        var trimmedPath = first.Path.Where(t => t != matchingCond).ToArray();
                        if (Guard(trimmedPath) is { } trimmedGuard)
                        {
                            var middle = steps.Skip(1).Take(steps.Count - 2).ToList();
                            var middleText = middle.Count == 1
                                ? $"{middle[0].Text} (does not re-trigger itself)"
                                : $"({string.Join("; ", middle.Select(s => s.Text))}; does not re-trigger itself)";
                            groups.Add(first with
                            {
                                Path = trimmedPath,
                                Guard = trimmedGuard,
                                Effects = [Combine(middle, middleText)],
                            });
                            continue;
                        }
                    }
                }
            }
            // When an assignment mutates a property (`cards exhausted = ...`) and a later step in the same
            // guarded group reads the pre-mutation snapshot (no getter or helper called after the setter), qualify it.
            if (currentMethod is not null && steps.Count >= 2 && steps[0].Text.Contains(" = ", StringComparison.Ordinal)
                && !steps[0].Text.EndsWith(" = true", StringComparison.Ordinal)
                && !steps[0].Text.EndsWith(" = false", StringComparison.Ordinal))
            {
                var eqIdx = steps[0].Text.IndexOf(" = ", StringComparison.Ordinal);
                var propHuman = steps[0].Text[..eqIdx];
                if (propHuman.Length > 0 && IsPreMutationSnapshotRead(currentMethod, propHuman))
                {
                    for (var si = 1; si < steps.Count; si++)
                    {
                        if (steps[si].Text.Contains(propHuman, StringComparison.Ordinal)
                            && !steps[si].Text.Contains($"previous {propHuman}", StringComparison.Ordinal))
                            steps[si] = steps[si] with { Text = steps[si].Text.Replace(propHuman, $"previous {propHuman}", StringComparison.Ordinal) };
                    }
                }
            }
            groups.Add(first with { Effects = [Combine(steps, $"({string.Join("; ", steps.Select(step => step.Text))})")] });
        }
        return groups;
    }

    private bool IsPreMutationSnapshotRead(MethodInfo currentMethod, string propHuman)
    {
        var methodBody = Body(currentMethod);
        var propPascal = propHuman.Replace(" ", "", StringComparison.Ordinal);
        var setterStep = methodBody.Calls.FirstOrDefault(s =>
            s.Call.Method.Name.StartsWith("set_", StringComparison.Ordinal)
            && s.Call.Method.Name[4..].Equals(propPascal, StringComparison.OrdinalIgnoreCase));
        if (setterStep is null)
            return false;
        var getterName = "get_" + setterStep.Call.Method.Name[4..];
        var hasLaterSameTypeHelperOrGetter = methodBody.Calls.Any(s =>
            (s.Epoch > setterStep.Epoch || (s.Epoch == setterStep.Epoch && s.Order > setterStep.Order))
            && (s.Call.Method.Name.Equals(getterName, StringComparison.Ordinal)
                || (s.Call.Method.DeclaringType == currentMethod.DeclaringType
                    && !IsBenignReadOrPlumbingCall(s.Call.Method))));
        return !hasLaterSameTypeHelperOrGetter;
    }

    private bool IsSingleMethodReentrancyFlag(MethodInfo currentMethod, string flagHuman)
    {
        if (currentMethod.DeclaringType is not { } ownerType)
            return false;
        var expectedName = flagHuman.Replace(" ", "", StringComparison.Ordinal);
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
        var prop = ownerType.GetProperties(flags)
            .FirstOrDefault(p => p.PropertyType == typeof(bool) && p.Name.Equals(expectedName, StringComparison.OrdinalIgnoreCase));
        var backing = prop?.GetMethod is { } getter ? BackingField(ownerType, getter) : null;
        var directField = backing ?? ownerType.GetFields(flags)
            .FirstOrDefault(f => f.FieldType == typeof(bool) && f.Name.TrimStart('_').Equals(expectedName, StringComparison.OrdinalIgnoreCase));
        if (directField is null && prop?.SetMethod is null)
            return false;

        foreach (var ctor in ownerType.GetConstructors(flags))
        {
            var ctorBody = NativeRuleBody.Read(ctor);
            if (ctorBody.Incomplete)
                return false;
            if (directField is not null && (ctorBody.Writes.Contains(directField)
                || ctorBody.FieldSteps.Any(fs => fs.Assignment.Target.Member == directField && Numeric(fs.Assignment.Assigned) != 0m)))
                return false;
            if (prop?.SetMethod is { } ctorSetter
                && ctorBody.Calls.Any(s => s.Call.Method == ctorSetter && Numeric(s.Call.Arguments.FirstOrDefault()) != 0m))
                return false;
        }

        foreach (var other in ownerType.GetMethods(flags))
        {
            if (other == currentMethod || other.IsSpecialName || other.ContainsGenericParameters || IsPurePresentationMember(other))
                continue;
            var otherBody = Body(other);
            if (otherBody.Incomplete)
                return false;
            if (directField is not null && (otherBody.Writes.Contains(directField)
                || otherBody.FieldSteps.Any(fs => fs.Assignment.Target.Member == directField)))
                return false;
            if (prop?.SetMethod is { } setter && otherBody.Calls.Any(s => s.Call.Method == setter))
                return false;
        }
        return true;
    }

    private string? CombatCondition(Test test)
    {
        if (CorridorCombatCondition(test) is { } corridorCond)
            return corridorCond;

        var expected = test.Expected;
        if (test.Value is Operation { Operator: "isinst", Right: Constant { Data: Type card },
            Left: Call { Method.Name: "get_ModelSource", Method.DeclaringType: var attack } source }
            && card == typeof(CardModel) && attack == typeof(AttackCommand) && IsAttackCommand(source.Receiver))
            return expected ? "attack source is a card" : "attack source is not a card";

        if (test.Value is Call { Method.Name: nameof(Enum.HasFlag), Method.DeclaringType: var enumeration,
            Receiver: Call { Method.Name: "get_DamageProps", Method.DeclaringType: var owner } props,
            Arguments: [Constant { Data: (int)ValueProp.Move }] }
            && enumeration == typeof(Enum) && owner == typeof(AttackCommand) && IsAttackCommand(props.Receiver))
            return expected ? "attack has Move damage" : "attack lacks Move damage";

        if (test.Value is Call { Method.Name: "get_UnblockedDamage" or "get_TotalDamage", Method.DeclaringType: var result,
            Receiver: { } hit } damage && result == typeof(DamageResult) && AttackHit(hit) is { } label)
            return $"{label} has {(expected ? "nonzero" : "zero")} {(damage.Method.Name == "get_UnblockedDamage" ? "unblocked" : "total")} damage";

        if (AttackHit(test.Value) is { } first)
            return expected ? $"{first} exists" : $"{first} does not exist";

        if (test.Value is Field field)
        {
            if (CardSlot(field) is { } slot)
                return expected ? $"{slot} exists" : $"no {slot}";
            if (PowerBoolSlot(field) is { } flagSlot)
                return $"{flagSlot}={expected.ToString().ToLowerInvariant()}";
        }

        if (test.Value is Call { Method: MethodInfo boolGetter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && boolGetter.ReturnType == typeof(bool) && typeof(PowerModel).IsAssignableFrom(boolGetter.DeclaringType)
            && Body(boolGetter) is { Incomplete: false, Returns: [{ Value: Field innerField, Conditions.Length: 0 }] }
            && PowerBoolSlot(innerField) is { } getterFlag)
            return $"{getterFlag}={expected.ToString().ToLowerInvariant()}";

        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un" } sideComparison
            && ((sideComparison.Left is Input { Name: "side" }
                    && sideComparison.Right is Call { Method.DeclaringType: var c1, Method.Name: "get_Side", Receiver: var h1 }
                    && c1 == typeof(Creature) && IsOwner(h1))
                || (sideComparison.Right is Input { Name: "side" }
                    && sideComparison.Left is Call { Method.DeclaringType: var c2, Method.Name: "get_Side", Receiver: var h2 }
                    && c2 == typeof(Creature) && IsOwner(h2))))
            return expected == (sideComparison.Operator != "bne.un") ? "turn side is owner's side" : "turn side is opposing side";

        if (test.Value is Operation { Operator: "isinst", Left: Input { Name: "power" }, Right: Constant { Data: Type iface } }
            && iface.Name == "ITemporaryPower")
            return expected ? "changed power is temporary" : "changed power is not temporary";

        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un",
            Left: Call { Method.Name: "GetTypeForAmount", Receiver: Input { Name: "power" } },
            Right: Constant { Data: int rawPType } } ptOp && Enum.IsDefined(typeof(MegaCrit.Sts2.Core.Entities.Powers.PowerType), rawPType))
        {
            var ptName = ((MegaCrit.Sts2.Core.Entities.Powers.PowerType)rawPType).ToString();
            return expected == (ptOp.Operator != "bne.un") ? $"changed power is a {ptName}" : $"changed power is not a {ptName}";
        }

        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un" } sideCheck
            && ((sideCheck.Left is Call { Method.Name: "get_CurrentSide" }
                    && sideCheck.Right is Call { Method.DeclaringType: var sideOwner1, Method.Name: "get_Side", Receiver: var sideReceiver1 }
                    && sideOwner1 == typeof(Creature) && ValueText(sideReceiver1) is { } sideOwnerWho1)
                || (sideCheck.Right is Call { Method.Name: "get_CurrentSide" }
                    && sideCheck.Left is Call { Method.DeclaringType: var sideOwner2, Method.Name: "get_Side", Receiver: var sideReceiver2 }
                    && sideOwner2 == typeof(Creature) && ValueText(sideReceiver2) is { } sideOwnerWho2 && (sideOwnerWho1 = sideOwnerWho2) is not null)))
            return expected == (sideCheck.Operator != "bne.un")
                ? $"acting side={Possessive(sideOwnerWho1)} side"
                : $"acting side≠{Possessive(sideOwnerWho1)} side";

        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un",
            Left: Call { Method.DeclaringType: var sideLType, Method.Name: "get_Side", Receiver: var sideLRec },
            Right: Call { Method.DeclaringType: var sideRType, Method.Name: "get_Side", Receiver: var sideRRec } } sameSideCheck
            && sideLType == typeof(Creature) && sideRType == typeof(Creature)
            && ValueText(sideLRec) is { } slWho && ValueText(sideRRec) is { } srWho)
            return expected == (sameSideCheck.Operator != "bne.un")
                ? $"{Possessive(slWho)} side={Possessive(srWho)} side"
                : $"{Possessive(slWho)} side≠{Possessive(srWho)} side";

        if (test.Value is Call { Method.Name: "CanAfflict", Receiver: Call { Method: { Name: "Affliction", IsGenericMethod: true } aff },
            Arguments: [var targetCard] } && ValueText(targetCard) is { } cardLabel)
            return $"{cardLabel} {(expected ? "can" : "cannot")} receive {ModelLabel(aff.GetGenericArguments()[0])}";

        if (test.Value is Call { Method.Name: "get_IsDupe", Receiver: var dupeCard } && ValueText(dupeCard) is { } dupeLabel)
            return $"{dupeLabel} {(expected ? "is" : "is not")} a dupe";

        if (test.Value is Call { Method.Name: nameof(Enumerable.Any), Arguments: [Call { Method.Name: "get_CardPlaysStarted" }, var predArg] }
            && CardPlayStartedFilterLabel(predArg, expected) is { } startedLabel)
            return startedLabel;

        // Private bool property checking DamageReceivedEntry history (e.g. EmotionChip.LostHpInPreviousTurn).
        if (test.Value is Call { Method: MethodInfo histProp, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && histProp.ReturnType == typeof(bool) && histProp.DeclaringType == _hookOwner
            && IsLostHpInPreviousTurnGetter(histProp))
            return expected ? "owner received a hit that was not fully blocked last player turn" : "owner did not receive a hit that was not fully blocked last player turn";

        if (test.Value is Call { Method: MethodInfo segmentCheck, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && TeammatePowerAllDeadType(segmentCheck) is { } sharedPower)
            return expected
                ? $"all other teammates with {PowerLabel(sharedPower)} are dead"
                : $"another teammate with {PowerLabel(sharedPower)} is alive";

        if (test.Value is Call { Method.Name: "op_Equality" or "op_Inequality", Method.DeclaringType: var strType, Arguments: [var leftStr, var rightStr] }
            && strType == typeof(string) && ValueText(leftStr) is { } ls && ValueText(rightStr) is { } rs)
            return $"{ls}{(test.Value is Call { Method.Name: "op_Equality" } == expected ? "=" : "≠")}{rs}";

        return null;
    }

    private string? CardPlayStartedFilterLabel(Value predArg, bool expected)
    {
        if (predArg is not Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }
            || pred.ContainsGenericParameters || Body(pred) is not { Incomplete: false } body)
            return null;
        CardType? filteredType = null;
        foreach (var cond in body.Returns.SelectMany(r => r.Conditions))
        {
            if (cond.Value is Operation { Left: Call { Method.Name: "get_Type" }, Right: var rhs } op
                && Numeric(rhs) is { } ct && (op.Operator is "beq" or "ceq" == cond.Expected))
                filteredType = (CardType)(int)ct;
        }
        var noun = filteredType is { } kind ? $"{kind} play" : "card play";
        return expected ? $"owner has started a {noun} this turn" : $"owner has not started a {noun} this turn";
    }

    private Type? TeammatePowerAllDeadType(MethodInfo method)
    {
        if (method.ReturnType != typeof(bool) || Body(method) is not { Incomplete: false,
            Returns: [{ Value: Call { Method.Name: nameof(Enumerable.All), Arguments: [Call { Method: MethodInfo sourceMethod, Receiver: Input { Name: "this" } },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }, Conditions.Length: 0 }] })
            return null;
        if (Body(pred) is not { Incomplete: false, Returns: [{ Value: Call { Method.Name: "get_IsDead" }, Conditions.Length: 0 }] })
            return null;
        var srcBody = Body(sourceMethod);
        var filter = srcBody.Calls.Select(s => s.Call.Arguments.OfType<Call>()
                .SelectMany(c => c.Arguments.OfType<Constant>())
                .Select(c => c.Data as MethodInfo).FirstOrDefault(m => m is not null))
            .FirstOrDefault(m => m is not null);
        if (filter is null)
            filter = srcBody.Returns.SelectMany(r => r.Value is Call { Arguments: var a } ? a : [])
                .OfType<Call>().SelectMany(c => c.Arguments.OfType<Constant>())
                .Select(c => c.Data as MethodInfo).FirstOrDefault();
        if (filter is not null && Body(filter) is { Incomplete: false, Returns: [{ Value: Call { Method: { Name: "HasPower", IsGenericMethod: true } hasPower } }] })
            return hasPower.GetGenericArguments()[0];
        return null;
    }

    private string? CombatValue(Value value)
    {
        if (CorridorCombatValue(value) is { } corridorVal)
            return corridorVal;

        return value switch
        {
            Field { Member: var field, Receiver: Input { Name: "this" } } when _choiceCursors.Contains(field) => "choice rounds completed",
            Field field when CardSlot(field) is { } slot => slot,
            Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                when monster is not null && getter.DeclaringType == monster.GetType() && _choiceCursors.Count != 0
                && Body(getter) is { Incomplete: false, Returns: [{ Value: Field { Receiver: Input { Name: "this" }, Member: var cursor }, Conditions.Length: 0 }] }
                && _choiceCursors.Contains(cursor) => "choice rounds completed",
            Call { Method: MethodInfo scalarGetter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                when scalarGetter.ReturnType == typeof(int)
                    && monster is not null && scalarGetter.DeclaringType?.IsInstanceOfType(monster) == true
                    && MonsterScalarText(scalarGetter) is { } scalar => scalar,
            Call { Method.Name: "get_CurrentHp", Method.DeclaringType: var creatureType, Receiver: { } who }
                when creatureType == typeof(Creature) && ValueText(who) is { } targetLabel => $"{Possessive(targetLabel)} current HP",
            Call { Method.Name: "get_MaxHp", Method.DeclaringType: var creatureMaxType, Receiver: { } maxWho }
                when creatureMaxType == typeof(Creature) && ValueText(maxWho) is { } maxLabel => $"{Possessive(maxLabel)} maximum HP",
            Call { Method.Name: "get_Block", Method.DeclaringType: var creatureBlkType, Receiver: { } blkWho }
                when creatureBlkType == typeof(Creature) && ValueText(blkWho) is { } blkLabel => $"{Possessive(blkLabel)} Block",
            Call { Method.Name: "get_SlotName", Method.DeclaringType: var slotOwner }
                when slotOwner == typeof(Creature) => "encounter slot",
        Call { Method.Name: "get_Target", Method.DeclaringType: var powerType, Receiver: Input { Name: "this" } }
            when powerType == typeof(PowerModel) => "this power's target",
        Call { Method.Name: "get_AllAffectedCreatures", Receiver: Input { Name: "this" } }
            => "this power's target and its pets",
        Call { Method.Name: "GetOpponentsOf", Arguments: [var selfCreature] }
            when selfCreature == Self || IsOwner(selfCreature)
            => PlayerTargetsLabel(),
        Call { Method.Name: "get_Monster", Method.DeclaringType: var type, Receiver: Input { Name: "dealer" } } when type == typeof(Creature)
            => "attacker",
        Call { Method.Name: "get_PetOwner", Method.DeclaringType: var type, Receiver: Input { Name: "dealer" } } when type == typeof(Creature)
            => "attacker's owner",
        Call { Method.Name: "get_PetOwner", Method.DeclaringType: var type, Receiver: { } targetPet }
            when type == typeof(Creature) && ValueText(targetPet) == PlayerTargetsLabel()
            => PlayerTargetsLabel(),
        Call { Method.Name: "get_PetOwner", Method.DeclaringType: var petOwnerType, Receiver: { } anyPet }
            when petOwnerType == typeof(Creature) && ValueText(anyPet) is { } petLabel
            => $"{Possessive(petLabel)} owner",
        Call { Method.Name: "get_Player", Method.DeclaringType: var type, Receiver: Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_PetOwner" } pet } }
            when type == typeof(Creature) => ValueText(pet),
        Call { Method.Name: "get_UnblockedDamage" or "get_TotalDamage" or "get_BlockedDamage", Method.DeclaringType: var type, Receiver: { } hit } damage
            when type == typeof(DamageResult) && (AttackHit(hit) ?? ValueText(hit)) is { } label
            => $"{label} {(damage.Method.Name switch { "get_UnblockedDamage" => "unblocked", "get_BlockedDamage" => "blocked", _ => "total" })} damage",
        Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: "get_Orbs" },
            Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo orbPred }] }] }
            when OrbTypeFromPredicate(orbPred) is { } orbType
            => $"owner's channeled {ModelLabel(orbType)} count",
        Input { Type: var type } when type == typeof(DamageResult) => "this hit's",
        Call { Method: { Name: "GetPowerAmount", IsGenericMethod: true } getAmt, Method.DeclaringType: var cType, Receiver: { } holder }
            when cType == typeof(Creature) && ValueText(holder) is { } who
            => $"{Possessive(who)} {ModelLabel(getAmt.GetGenericArguments()[0])} stacks",
        Call { Method.Name: "get_Amount", Method.DeclaringType: var type, Receiver: { } power }
            when type == typeof(PowerModel) && OwnedPower(power) is { } kind => $"self's {ModelLabel(kind)} stacks",
        Call call when OwnedPower(call) is { } power => $"self's {ModelLabel(power)}",
            Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method: { Name: "OfType", IsGenericMethod: true } ofType },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo countPred }] }] }
                when ofType.GetGenericArguments()[0].Name == "CardAfflictedEntry" && AfflictionFromPredicate(countPred) is { } affType
                => $"owner's cards afflicted with {ModelLabel(affType)} this turn",
            Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method: { Name: "OfType", IsGenericMethod: true } ofType },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo drawnPred }] }] }
                when ofType.GetGenericArguments()[0].Name == "CardDrawnEntry" && DescribeHistoryEntryCount("drawn", drawnPred) is { } drawnLabel
                => drawnLabel,
            Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method: { Name: "OfType", IsGenericMethod: true } ofType },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo blkPred }] }] }
                when ofType.GetGenericArguments()[0].Name == "BlockGainedEntry" && IsBlockGainedThisTurnPredicate(blkPred)
                => "earlier card/move Block gains from other card plays by owner this turn",
            Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: "get_CardPlaysFinished" },
                Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo finPred }] }] }
                when DescribeHistoryEntryCount("finished", finPred) is { } finLabel
                => finLabel,
            Call { Method: MethodInfo countProp, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                when countProp.ReturnType == typeof(int) && countProp.DeclaringType == _hookOwner
                    && DescribePowerHistoryCountGetter(countProp) is { } propCountLabel
                => propCountLabel,
            _ => AttackHit(value),
        };
    }

    private bool IsLostHpInPreviousTurnGetter(MethodInfo getter)
    {
        if (Body(getter) is not { Incomplete: false, Returns: [{ Value: Call { Method.Name: nameof(Enumerable.Any),
                Arguments: [Call { Method: { Name: "OfType", IsGenericMethod: true } ofType,
                        Arguments: [Call { Method.Name: "get_Entries", Receiver: Call { Method.Name: "get_History" } }] },
                    Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] }, Conditions.Length: 0 }] }
            || ofType.GetGenericArguments()[0].Name != "DamageReceivedEntry")
            return false;
        var pBody = Body(pred);
        if (pBody.Incomplete || pBody.Returns.Count == 0)
            return false;
        var positive = pBody.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var checksOwnerReceiver = false;
        var checksNotFullyBlocked = false;
        var checksLastPlayerTurn = false;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (IsEquality(new Test(expr, expected), true, out var l, out var r)
                && ((l is Call { Method.Name: "get_Receiver" } && IsOwner(r))
                    || (r is Call { Method.Name: "get_Receiver" } && IsOwner(l))))
            {
                checksOwnerReceiver = true;
                continue;
            }
            if (!expected && expr is Call { Method.Name: "get_WasFullyBlocked" })
            {
                checksNotFullyBlocked = true;
                continue;
            }
            if (expected && expr is Call { Method.Name: "HappenedLastPlayerTurn" })
            {
                checksLastPlayerTurn = true;
                continue;
            }
            return false;
        }
        return checksOwnerReceiver && checksNotFullyBlocked && checksLastPlayerTurn;
    }

    private string? DescribePowerHistoryCountGetter(MethodInfo getter)
    {
        var body = Body(getter);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        Result? countRet = null;
        if (body.Returns is [{ Conditions.Length: 0 } singleRet])
        {
            countRet = singleRet;
        }
        else if (body.Returns.Count == 2)
        {
            var zeroRet = body.Returns.FirstOrDefault(r => Numeric(r.Value) == 0m
                && r.Conditions is [{ Expected: false, Value: Call { Method.Name: "get_IsInProgress" } }]);
            var activeRet = body.Returns.FirstOrDefault(r => r != zeroRet
                && r.Conditions is [{ Expected: true, Value: Call { Method.Name: "get_IsInProgress" } }]);
            if (zeroRet is not null && activeRet is not null)
                countRet = activeRet;
        }
        if (countRet?.Value is not Call { Method.Name: nameof(Enumerable.Count),
                Arguments: [Call { Method.Name: "get_CardPlaysFinished" or "get_CardPlaysStarted" } srcCall,
                    Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo pred }] }] })
            return null;
        var kind = srcCall.Method.Name == "get_CardPlaysStarted" ? "started" : "finished";
        return DescribeHistoryEntryCount(kind, pred);
    }

    private string? DescribeHistoryEntryCount(string kind, MethodInfo pred)
    {
        var body = Body(pred);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return null;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var hasTurnCheck = false;
        var hasOwnerCheck = false;
        CardType? filteredType = null;
        CardTag? filteredTag = null;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expected && expr is Call { Method.Name: "HappenedThisTurn" })
            {
                hasTurnCheck = true;
                continue;
            }
            if (IsEquality(new Test(expr, expected), true, out var eqL, out var eqR)
                && ((IsEntryOwnerExpr(eqL) && IsOwnerPlayerOrCreature(eqR))
                    || (IsEntryOwnerExpr(eqR) && IsOwnerPlayerOrCreature(eqL))))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (IsEquality(new Test(expr, expected), true, out var l, out var r) && l is Call { Method.Name: "get_Type" }
                && Numeric(r) is { } ct && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                filteredType = (CardType)(int)ct;
                continue;
            }
            if (expected && expr is Call { Method.Name: "Contains", Arguments: [Call { Method.Name: "get_Tags" }, Constant { Data: int rawTag }] }
                && Enum.IsDefined(typeof(CardTag), rawTag))
            {
                filteredTag = (CardTag)rawTag;
                continue;
            }
            return null;
        }
        if (!hasTurnCheck || !hasOwnerCheck)
            return null;

        var qualifiers = new List<string>();
        if (filteredTag is { } tag)
            qualifiers.Add($"{tag}-tagged");
        if (filteredType is { } ctVal)
            qualifiers.Add(ctVal.ToString());
        var qualifier = qualifiers.Count > 0 ? $"{string.Join(" ", qualifiers)} " : "";
        return kind switch
        {
            "drawn" => $"{qualifier}cards drawn by owner this turn",
            "started" => $"{qualifier}card plays started by owner this turn",
            _ => $"{qualifier}card plays finished by owner this turn",
        };
    }

    private bool IsBlockGainedThisTurnPredicate(MethodInfo pred)
    {
        var body = Body(pred);
        if (body.Incomplete || body.Returns.Count == 0)
            return false;
        var positive = body.Returns.Where(r => !(TryTruth(r.Value, out var b) && !b)).ToArray();
        if (positive.Length != 1)
            return false;
        var tests = new List<Test>(positive[0].Conditions);
        if (!(TryTruth(positive[0].Value, out var retBool) && retBool))
            tests.Add(new Test(positive[0].Value, true));

        var hasTurnCheck = false;
        var hasMoveCheck = false;
        var hasOwnerCheck = false;
        var excludesCurrentCardPlay = false;
        foreach (var raw in tests)
        {
            var expr = raw.Value;
            var expected = raw.Expected;
            while (expr is Operation { Operator: "ceq", Left: var inner, Right: Constant { Data: 0 or null } })
            {
                expr = inner;
                expected = !expected;
            }
            if (expected && expr is Call { Method.Name: "HappenedThisTurn" })
            {
                hasTurnCheck = true;
                continue;
            }
            if (expected && expr is Call { Method.Name: "get_CardPlay" })
                continue;
            if (expected && expr is Call { Method.Name: "IsCardOrMonsterMove" })
            {
                hasMoveCheck = true;
                continue;
            }
            if (IsEquality(new Test(expr, expected), true, out var eqL, out var eqR)
                && ((eqL is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Player" } } && IsOwnerOrClosureOwner(eqR))
                    || (eqR is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Player" } } && IsOwnerOrClosureOwner(eqL))))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (IsEquality(new Test(expr, expected), false, out var neqL, out var neqR)
                && ((neqL is Call { Method.Name: "get_CardPlay" } && neqR is Field { Member.Name: "cardPlay" })
                    || (neqR is Call { Method.Name: "get_CardPlay" } && neqL is Field { Member.Name: "cardPlay" })))
            {
                excludesCurrentCardPlay = true;
                continue;
            }
            return false;
        }
        return hasTurnCheck && hasMoveCheck && hasOwnerCheck && excludesCurrentCardPlay;
    }

    private static bool IsOwnerOrClosureOwner(Value? value)
        => IsOwner(value)
            || value is Call { Method.Name: "get_Owner" or "get_Creature", Receiver: Field { Member.Name: "<>4__this" } }
            || value is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner", Receiver: Field { Member.Name: "<>4__this" } } };

    private Type? OrbTypeFromPredicate(MethodInfo predicate)
        => Body(predicate).Returns.SelectMany(r => FindOrbTypes(r.Value)).FirstOrDefault();

    private static IEnumerable<Type> FindOrbTypes(Value value) => value switch
    {
        Operation { Operator: "isinst", Right: Constant { Data: Type t } } when typeof(OrbModel).IsAssignableFrom(t) => [t],
        Operation op => FindOrbTypes(op.Left).Concat(op.Right is { } r ? FindOrbTypes(r) : []),
        Call call => (call.Receiver is { } rec ? FindOrbTypes(rec) : []).Concat(call.Arguments.SelectMany(FindOrbTypes)),
        _ => [],
    };

    private Type? AfflictionFromPredicate(MethodInfo predicate)
        => Body(predicate).Returns.SelectMany(r => FindAfflictionTypes(r.Value)).FirstOrDefault();

    private static IEnumerable<Type> FindAfflictionTypes(Value value) => value switch
    {
        Operation { Operator: "isinst", Right: Constant { Data: Type t } } when typeof(AfflictionModel).IsAssignableFrom(t) => [t],
        Operation op => FindAfflictionTypes(op.Left).Concat(op.Right is { } r ? FindAfflictionTypes(r) : []),
        Call call => (call.Receiver is { } rec ? FindAfflictionTypes(rec) : []).Concat(call.Arguments.SelectMany(FindAfflictionTypes)),
        _ => [],
    };

    private string? MonsterScalarText(MethodInfo getter)
    {
        var isPropertyGetter = getter.Name.StartsWith("get_", StringComparison.Ordinal);
        if (isPropertyGetter && getter.DeclaringType is { } owner && BackingField(owner, getter) is { } backing)
        {
            if (!IsGameplayField(backing))
                return null;
            _trackedMonsterInts.Add((getter.Name[4..], backing));
            return HumanizeLower(getter.Name[4..]);
        }
        var body = Body(getter);
        if (body.Incomplete || (!isPropertyGetter && (body.Writes.Count > 0 || body.FieldSteps.Count > 0)))
            return null;
        if (isPropertyGetter
            && body.Returns is [{ Conditions.Length: 0, Value: Call { Method.Name: "GetValueOrDefault",
                Receiver: Field { Receiver: Input { Name: "this" }, Member: var nullableField },
                Arguments: [Constant { Data: int defaultVal }] } }]
            && monster is not null && nullableField.DeclaringType?.IsInstanceOfType(monster) == true)
        {
            var canonical = ModelDb.GetByIdOrNull<MonsterModel>(monster.Id);
            var initial = canonical is not null && nullableField.DeclaringType.IsInstanceOfType(canonical)
                && nullableField.GetValue(canonical) is int setVal
                ? setVal
                : defaultVal;
            _trackedMonsterNullableDefaults.Add((getter.Name[4..], initial));
            return HumanizeLower(getter.Name[4..]);
        }
        var active = body.Returns.Where(r => r.Conditions.All(IsCurrentRun)).ToArray();
        if (active is not [var ret] || !ret.Conditions.All(IsDreadTest))
            return null;
        if (_expandingGetters.Count >= MaxGetterDepth || !_expandingGetters.Add(getter))
        {
            _getterCycleDetected = true;
            Gap($"recursive-getter:{getter.DeclaringType?.Name}.{getter.Name}");
            return null;
        }
        try
        {
            return ValueText(ret.Value);
        }
        finally
        {
            _expandingGetters.Remove(getter);
        }
    }

    private readonly HashSet<(string Name, FieldInfo Field)> _trackedMonsterInts = [];
    private readonly HashSet<(string Name, int DefaultValue)> _trackedMonsterNullableDefaults = [];

    private static Type? OwnedPower(Value source)
        => source is Call { Method.DeclaringType: var first, Method.Name: nameof(Enumerable.FirstOrDefault),
            Arguments: [Call { Method: { DeclaringType: var filter, Name: nameof(Enumerable.OfType), IsGenericMethod: true } lookup, Arguments:
                [Call { Method.DeclaringType: var creature, Method.Name: "get_Powers", Receiver: var holder }] }] }
            && first == typeof(Enumerable) && filter == typeof(Enumerable) && creature == typeof(Creature) && IsOwner(holder)
            && typeof(PowerModel).IsAssignableFrom(lookup.GetGenericArguments()[0])
                ? lookup.GetGenericArguments()[0]
                : null;

    private static bool ReadsOwnedPowerStacks(Value test)
        => test is Operation { Left: Call { Method.DeclaringType: var type, Method.Name: "get_Amount", Receiver: { } power } }
            && type == typeof(PowerModel) && OwnedPower(power) is not null;

    private static bool IsAttackCommand(Value? value)
        => value is Input { Type: var type } && type == typeof(AttackCommand);

    private string? AttackHit(Value value)
    {
        // Read a flattened attack result only when the first-match predicate selects the holder.
        if (value is not Call { Method.Name: nameof(Enumerable.FirstOrDefault), Method.DeclaringType: var first,
                Arguments: [Call { Method.Name: nameof(Enumerable.SelectMany), Method.DeclaringType: var flatten, Arguments:
                    [Call { Method.Name: "get_Results", Method.DeclaringType: var attack } results,
                    Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo group }] }] },
                    Call { Method: ConstructorInfo, Arguments: [Input { Name: "this" }, Constant { Data: MethodInfo predicate }] }] }
            || first != typeof(Enumerable) || flatten != typeof(Enumerable) || attack != typeof(AttackCommand) || !IsAttackCommand(results.Receiver)
            || Body(group) is not { Incomplete: false, Returns: [{ Value: Input, Conditions.Length: 0 }] }
            || Body(predicate) is not { Incomplete: false, Returns: [{ Value: Operation { Operator: "ceq", Left:
                Call { Method.Name: "get_Receiver", Method.DeclaringType: var result, Receiver: Input }, Right: var holder }, Conditions.Length: 0 }] }
            || result != typeof(DamageResult) || !IsOwner(holder))
            return null;
        return "the first hit on owner in this attack";
    }
}
