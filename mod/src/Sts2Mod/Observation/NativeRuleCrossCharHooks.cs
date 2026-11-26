using System.Globalization;
using System.Reflection;
using HarmonyLib;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Commands.Builders;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Powers;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Cards;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    private const BindingFlags DeclaredInstanceOnly =
        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

    private void ReadCrossCharHooks(Type type, string prefix, HashSet<string> handled, List<string> lines)
    {
        if (typeof(PowerModel).IsAssignableFrom(type) && ReadMarkerPowerRule(type, prefix, out var markerLine))
            lines.Add(markerLine);

        if (type.GetMethod("ModifyVulnerableMultiplier", DeclaredInstanceOnly) is { } vulnHook)
        {
            if (ReadDamageStatusMultiplierHook(vulnHook, isVulnerable: true, out var vulnLine))
                lines.Add($"{prefix}{vulnLine}");
            else
                Gap($"{type.Name}.{vulnHook.Name}");
        }

        if (type.GetMethod("ModifyWeakMultiplier", DeclaredInstanceOnly) is { } weakHook)
        {
            if (ReadDamageStatusMultiplierHook(weakHook, isVulnerable: false, out var weakLine))
                lines.Add($"{prefix}{weakLine}");
            else
                Gap($"{type.Name}.{weakHook.Name}");
        }

        if (FindOverride(type, "ModifyXValue") is { } xHook)
        {
            handled.Add(xHook.Name);
            if (ReadModifyXValueHook(xHook) is { } xLine)
                lines.Add($"{prefix}{xLine}");
            else
                Gap($"{type.Name}.{xHook.Name}");
        }

        if (FindOverride(type, "ModifyAttackHitCount") is { } hitHook)
        {
            handled.Add(hitHook.Name);
            if (ReadModifyAttackHitCountHook(hitHook) is { } hitLine)
                lines.Add($"{prefix}{hitLine}");
            else
                Gap($"{type.Name}.{hitHook.Name}");
        }

        if (FindOverride(type, "ModifyEnergyGain") is { } energyGainHook)
        {
            handled.Add(energyGainHook.Name);
            if (ReadModifyEnergyGainHook(type, energyGainHook, handled) is { } egLine)
                lines.Add($"{prefix}{egLine}");
            else
                Gap($"{type.Name}.{energyGainHook.Name}");
        }

        if (typeof(RelicModel).IsAssignableFrom(type) && ReadUnsettlingLampHooks(type, prefix, handled, out var lampLine))
            lines.Add(lampLine);
        else if (!handled.Contains("ModifyPowerAmountGivenMultiplicative")
            && FindOverride(type, "ModifyPowerAmountGivenMultiplicative") is { } givenMult)
        {
            handled.Add(givenMult.Name);
            var rendered = ReadValueHook(givenMult, 1m, "×{0}", out _);
            if (rendered is { Count: > 0 })
            {
                if (FindOverride(type, "AfterModifyingPowerAmountGiven") is { } afterGiven && IsPurePresentationHook(afterGiven))
                    handled.Add(afterGiven.Name);
                lines.Add($"{prefix}Power stacks given (multiplicative): {string.Join("; ", rendered)}");
            }
            else
            {
                Gap($"{type.Name}.{givenMult.Name}");
            }
        }

        if (typeof(RelicModel).IsAssignableFrom(type) && ReadOncePerCombatReplayHook(type, prefix, handled, out var replayLine))
            lines.Add(replayLine);

        if (typeof(RelicModel).IsAssignableFrom(type) && ReadPrioritizedFreeHandCardHook(type, prefix, handled, out var freeHandLine))
            lines.Add(freeHandLine);
    }

    private bool ReadDamageStatusMultiplierHook(MethodInfo hook, bool isVulnerable, out string line)
    {
        line = "";
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0 || hook.DeclaringType is not { } declaring)
            return false;

        if (!TryReadStatusMultiplierScope(declaring, isVulnerable, out var callerScope))
            return false;

        var statusLabel = ModelLabel(isVulnerable ? typeof(VulnerablePower) : typeof(WeakPower));
        var heading = $"{statusLabel} damage multiplier{callerScope}";
        var outcomes = new List<(string Text, Test[] Conditions)>();

        foreach (var result in body.Returns)
        {
            if (result.Value is Input { Name: "amount" })
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }

            if (DescribeMultiplierTransform(result.Value, isVulnerable) is { } transform)
            {
                outcomes.Add((transform, result.Conditions));
                continue;
            }

            return false;
        }

        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        if (rendered is not { Count: > 0 })
            return false;

        line = $"{heading}: {string.Join("; ", rendered)}";
        return true;
    }

    private static bool TryReadStatusMultiplierScope(Type declaringType, bool isVulnerable, out string scope)
    {
        scope = "";
        var ownerPower = isVulnerable ? typeof(VulnerablePower) : typeof(WeakPower);
        var hookName = isVulnerable ? "ModifyVulnerableMultiplier" : "ModifyWeakMultiplier";
        var method = ownerPower.GetMethod("ModifyDamageMultiplicative", DeclaredInstanceOnly);
        if (method is null)
            return false;

        var body = NativeRuleBody.Read(method);
        if (body.Incomplete)
            return false;

        var ownerAttacks = false;
        var petAttacks = false;
        var ownerTargeted = false;
        var matchedAny = false;

        foreach (var step in body.Calls)
        {
            if (step.Call.Method.DeclaringType != declaringType || step.Call.Method.Name != hookName)
                continue;
            matchedAny = true;
            var receiver = step.Call.Receiver;
            if (receiver is Call { Method.Name: "GetRelic", Receiver: Call { Method.Name: "get_Player", Receiver: Input { Name: "dealer" } } }
                || receiver is Call { Method.Name: "GetPower", Receiver: Input { Name: "dealer" } })
            {
                ownerAttacks = true;
            }
            else if (receiver is Call { Method.Name: "GetPower", Receiver: Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_PetOwner", Receiver: Input { Name: "dealer" } } } })
            {
                petAttacks = true;
            }
            else if (receiver is Call { Method.Name: "GetRelic", Receiver: Call { Method.Name: "get_Player", Receiver: Input { Name: "target" } } }
                || receiver is Call { Method.Name: "GetPower", Receiver: Input { Name: "target" } })
            {
                ownerTargeted = true;
            }
            else
            {
                return false;
            }
        }

        if (!matchedAny)
            return false;
        if (ownerTargeted && !ownerAttacks && !petAttacks)
        {
            scope = " (when owner is targeted)";
            return true;
        }
        if (ownerAttacks && petAttacks && !ownerTargeted)
        {
            scope = " (when owner or owner's pet attacks)";
            return true;
        }
        if (ownerAttacks && !petAttacks && !ownerTargeted)
        {
            scope = " (when owner attacks)";
            return true;
        }
        return false;
    }

    private string? DescribeMultiplierTransform(Value value, bool isVulnerable)
    {
        var statusLabel = ModelLabel(isVulnerable ? typeof(VulnerablePower) : typeof(WeakPower));
        // Debilitate Vulnerable: amount + (amount - 1m)
        if (value is Call
            {
                Method.Name: "op_Addition",
                Arguments: [Input { Name: "amount" }, Call
                {
                    Method.Name: "op_Subtraction",
                    Arguments: [Input { Name: "amount" }, var subOne],
                }],
            }
            && Numeric(subOne) == 1m)
        {
            return $"double the {statusLabel} bonus above 1× (amount + (amount - 1))";
        }

        // Debilitate Weak: amount - (1m - amount)
        if (value is Call
            {
                Method.Name: "op_Subtraction",
                Arguments: [Input { Name: "amount" }, Call
                {
                    Method.Name: "op_Subtraction",
                    Arguments: [var oneSub, Input { Name: "amount" }],
                }],
            }
            && Numeric(oneSub) == 1m)
        {
            return $"double the {statusLabel} penalty below 1× (amount - (1 - amount))";
        }

        // CrueltyPower: amount + (decimal)Amount / 100m
        if (value is Call
            {
                Method.Name: "op_Addition",
                Arguments: [Input { Name: "amount" }, Call
                {
                    Method.Name: "op_Division",
                    Arguments: [var num, var den],
                }],
            }
            && IsStackAmount(num) && Numeric(den) is { } denom && denom > 0m)
        {
            return $"add +(stacks/{Number(denom)}) to the {statusLabel} multiplier";
        }

        // PaperPhrog: amount + 0.25m
        if (value is Call
            {
                Method.Name: "op_Addition",
                Arguments: [Input { Name: "amount" }, var addDelta],
            }
            && ValueText(addDelta) is { } addText)
        {
            return $"add +{addText} to the {statusLabel} multiplier";
        }

        // PaperKrane: amount - 0.15m
        if (value is Call
            {
                Method.Name: "op_Subtraction",
                Arguments: [Input { Name: "amount" }, var subDelta],
            }
            && ValueText(subDelta) is { } subText)
        {
            return $"subtract {subText} from the {statusLabel} multiplier";
        }

        return null;
    }

    private string? ReadModifyXValueHook(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (result.Value is Input { Name: "originalValue" })
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }

            if (result.Value is Operation { Operator: "add", Left: Input { Name: "originalValue" }, Right: var delta }
                && ValueText(delta) is { } deltaText)
            {
                outcomes.Add(($"+{deltaText} to resolved X (applies to both Energy X-cost and Star X-cost cards)", result.Conditions));
                continue;
            }

            return null;
        }

        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        return rendered is { Count: > 0 } ? $"X-cost resolved value: {string.Join("; ", rendered)}" : null;
    }

    private string? ReadModifyAttackHitCountHook(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        var boosted = body.Returns.Where(r => r.Value is not Input { Name: "hitCount" }).ToArray();
        if (boosted.Length != 1)
            return null;

        var active = boosted[0];
        if (active.Value is not Operation { Operator: "add", Left: Input { Name: "hitCount" }, Right: var delta }
            || ValueText(delta) is not { } deltaText)
        {
            return null;
        }

        int? minHitsExclusive = null;
        var requiresCardSource = false;
        CardType? requiredCardType = null;
        var requiresOwner = false;

        foreach (var rawCond in active.Conditions)
        {
            var cond = NormalizeCondition(rawCond);
            var expr = cond.Value;
            var expected = cond.Expected;

            if (expr is Operation { Operator: "bgt" or "cgt" or "ble" or "cle", Left: Input { Name: "hitCount" }, Right: var rhs } hitOp
                && Numeric(rhs) is { } limit)
            {
                var isGt = hitOp.Operator is "bgt" or "cgt" ? expected : !expected;
                if (!isGt)
                    return null;
                minHitsExclusive = (int)limit;
                continue;
            }

            if (expected && UnwrapIsCardModelSource(expr))
            {
                requiresCardSource = true;
                continue;
            }

            if (IsEquality(cond, true, out var typeL, out var typeR)
                && typeL is Call { Method.Name: "get_Type", Receiver: var typeRec }
                && IsAttackCardSourceExpr(typeRec)
                && Numeric(typeR) is { } rawType && Enum.IsDefined(typeof(CardType), (int)rawType))
            {
                requiredCardType = (CardType)(int)rawType;
                continue;
            }

            if (IsEquality(cond, true, out var ownerL, out var ownerR)
                && ((ownerL is Call { Method.Name: "get_Owner", Receiver: var recL } && IsAttackCardSourceExpr(recL) && IsOwner(ownerR))
                    || (ownerR is Call { Method.Name: "get_Owner", Receiver: var recR } && IsAttackCardSourceExpr(recR) && IsOwner(ownerL))))
            {
                requiresOwner = true;
                continue;
            }

            return null;
        }

        if (!requiresCardSource || !requiresOwner)
            return null;

        var clauses = new List<string>();
        if (minHitsExclusive is { } minH)
            clauses.Add($"original hit count>{minH}");
        var cardDesc = requiredCardType is { } ct ? $"owned {ct} card" : "owned card";
        clauses.Add($"source is an {cardDesc}");
        var unit = deltaText == "1" ? "hit" : "hits";
        return $"Attack hit count: if {string.Join(" and ", clauses)}: +{deltaText} {unit}";
    }

    private static bool UnwrapIsCardModelSource(Value expr) => expr switch
    {
        Operation { Operator: "cgt.un", Left: var inner, Right: Constant { Data: 0 or null } } => UnwrapIsCardModelSource(inner),
        Operation { Operator: "isinst", Left: Call { Method.Name: "get_ModelSource", Receiver: Input { Name: "attack" } }, Right: Constant { Data: Type t } }
            when t == typeof(CardModel) => true,
        _ => false,
    };

    private static bool IsAttackCardSourceExpr(Value? expr) => expr switch
    {
        Operation { Operator: "isinst" or "castclass", Left: Call { Method.Name: "get_ModelSource", Receiver: Input { Name: "attack" } }, Right: Constant { Data: Type t } }
            when t == typeof(CardModel) => true,
        _ => false,
    };

    private string? ReadModifyEnergyGainHook(Type type, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (result.Value is Input { Name: "amount" })
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }
            if (Numeric(result.Value) == 0m)
            {
                outcomes.Add(("set to 0", result.Conditions));
                continue;
            }
            if (ValueText(result.Value) is { } valText)
            {
                outcomes.Add(($"set to {valText}", result.Conditions));
                continue;
            }
            return null;
        }

        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        if (rendered is not { Count: > 0 })
            return null;

        if (FindOverride(type, "AfterModifyingEnergyGain") is { } after && IsPurePresentationHook(after))
            handled.Add(after.Name);

        return $"Energy gain: {string.Join("; ", rendered)}";
    }

    private bool IsGameplayEffectCall(MethodBase method)
    {
        if (IsModelEffect(method))
            return true;
        if (method is not MethodInfo info || info.DeclaringType is not { } type
            || IsPurePresentationMember(info) || IsBenignReadOrPlumbingCall(info))
            return false;
        if (type.Namespace == typeof(CreatureCmd).Namespace
            && type.Name is not ("SfxCmd" or "VfxCmd" or "TalkCmd" or "ThinkCmd" or "Cmd"))
        {
            return true;
        }
        if (typeof(AttackCommand).IsAssignableFrom(type))
            return true;
        if (info.ReturnType is { IsGenericType: true } ret && ret.GetGenericTypeDefinition() == typeof(Task<>)
            && (typeof(AbstractModel).IsAssignableFrom(type)
                || type == typeof(Creature)
                || type == typeof(MegaCrit.Sts2.Core.Entities.Players.Player)
                || typeof(MegaCrit.Sts2.Core.Combat.ICombatState).IsAssignableFrom(type)
                || type == typeof(CombatRoom)
                || type == typeof(CardEnergyCost)))
        {
            return true;
        }
        return false;
    }

    private bool ReadUnsettlingLampHooks(Type type, string prefix, HashSet<string> handled, out string line)
    {
        line = "";
        if (FindOverride(type, "BeforeCombatStart") is not { } beforeCombat
            || FindOverride(type, "BeforePowerAmountChanged") is not { } beforePower
            || FindOverride(type, "ModifyPowerAmountGivenMultiplicative") is not { } multHook
            || FindOverride(type, "AfterCardPlayed") is not { } afterPlayed
            || FindOverride(type, "AfterCombatEnd") is not { } afterCombat)
        {
            return false;
        }

        var bcBody = Body(beforeCombat);
        var bpBody = Body(beforePower);
        var multBody = Body(multHook);
        var apBody = Body(afterPlayed);
        var acBody = Body(afterCombat);
        if (bcBody.Incomplete || bpBody.Incomplete || multBody.Incomplete || apBody.Incomplete || acBody.Incomplete
            || bcBody.Writes.Count > 0 || bpBody.Writes.Count > 0 || multBody.Writes.Count > 0
            || apBody.Writes.Count > 0 || acBody.Writes.Count > 0
            || bcBody.FieldSteps.Count > 0 || bpBody.FieldSteps.Count > 0 || multBody.FieldSteps.Count > 0
            || apBody.FieldSteps.Count > 0 || acBody.FieldSteps.Count > 0)
            return false;

        bool HasOnlyAllowedCalls(NativeRuleBody b, params string[] allowedOwnSetters) =>
            b.Calls.All(s =>
                (s.Call.Method.DeclaringType == type
                    && allowedOwnSetters.Contains(s.Call.Method.Name, StringComparer.Ordinal))
                || (s.Call.Method.DeclaringType == typeof(RelicModel)
                    && s.Call.Method.Name is "set_Status" or "Flash")
                || !IsGameplayEffectCall(s.Call.Method));

        bool ResetsLampState(NativeRuleBody b) =>
            HasOnlyAllowedCalls(b, "set_TriggeringCard", "set_IsFinishedTriggering")
            && b.Calls.Any(s => s.Call.Method.DeclaringType == type && s.Call.Method.Name == "set_TriggeringCard" && s.Call.Arguments is [Constant { Data: null }] && s.Conditions.Length == 0)
            && b.Calls.Any(s => s.Call.Method.DeclaringType == type && s.Call.Method.Name == "set_IsFinishedTriggering" && s.Call.Arguments is [Constant { Data: 0 or false }] && s.Conditions.Length == 0)
            && b.Calls.Any(s => s.Call.Method.Name == "Clear" && s.Call.Receiver is Call { Method.Name: "get_DoubledPowers" } && s.Conditions.Length == 0);

        if (!ResetsLampState(bcBody) || !ResetsLampState(acBody))
            return false;

        if (!HasOnlyAllowedCalls(bpBody, "set_TriggeringCard")
            || !HasOnlyAllowedCalls(multBody)
            || !HasOnlyAllowedCalls(apBody, "set_IsFinishedTriggering"))
            return false;

        var activeMults = multBody.Returns.Where(r => Numeric(r.Value) != 1m).ToArray();
        if (activeMults is not [var activeMult] || Numeric(activeMult.Value) is not { } factor || factor <= 1m)
            return false;

        var multChecksCard = activeMult.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && ((l is Input { Name: "cardSource" } && r is Call { Method.Name: "get_TriggeringCard" })
                || (r is Input { Name: "cardSource" } && l is Call { Method.Name: "get_TriggeringCard" })));
        var multChecksNotFinished = activeMult.Conditions.Any(c =>
            NormalizeCondition(c) is { Expected: false, Value: Call { Method.Name: "get_IsFinishedTriggering" } });
        var multChecksDebuff = activeMult.Conditions.Any(c =>
            IsEquality(NormalizeCondition(c), true, out var l, out var r)
            && l is Call { Method.Name: "GetTypeForAmount" }
            && Numeric(r) == (int)PowerType.Debuff);
        var multChecksTempSkip = activeMult.Conditions.Any(c =>
            NormalizeCondition(c) is { Expected: false, Value: Call { Method.Name: "HasDoubledTemporaryPowerSource" } });
        if (!multChecksCard || !multChecksNotFinished || !multChecksDebuff || !multChecksTempSkip)
            return false;

        var recordsTriggerCard = bpBody.Calls.Any(s =>
            s.Call.Method.Name == "set_TriggeringCard"
            && s.Call.Arguments is [Input { Name: "cardSource" }]
            && s.Conditions.Any(c => NormalizeCondition(c) is { Expected: false, Value: Call { Method.Name: "get_TriggeringCard" } })
            && s.Conditions.Any(c => NormalizeCondition(c) is { Expected: false, Value: Call { Method.Name: "get_IsFinishedTriggering" } })
            && s.Conditions.Any(c => NormalizeCondition(c) is { Expected: true, Value: Input { Name: "cardSource" } })
            && s.Conditions.Any(c =>
                IsEquality(NormalizeCondition(c), true, out var l, out var r)
                && ((l is Input { Name: "applier" } && IsOwner(r)) || (r is Input { Name: "applier" } && IsOwner(l))))
            && s.Conditions.Any(c =>
                IsEquality(NormalizeCondition(c), false, out var l, out var r)
                && l is Call { Method.Name: "get_Side", Receiver: Input { Name: "target" } }
                && r is Call { Method.Name: "get_Side", Receiver: var ownerRec } && IsOwner(ownerRec))
            && s.Conditions.Any(c => NormalizeCondition(c) is { Expected: true, Value: Call { Method.Name: "get_IsVisible" } })
            && s.Conditions.Any(c =>
                IsEquality(NormalizeCondition(c), true, out var l, out var r)
                && l is Call { Method.Name: "GetTypeForAmount" }
                && Numeric(r) == (int)PowerType.Debuff)
            && s.Conditions.Any(c =>
                NormalizeCondition(c) is { Expected: false, Value: Call { Method: { Name: "HasPower", IsGenericMethod: true } hp } }
                && hp.GetGenericArguments()[0] == typeof(ArtifactPower)));

        var addsDoubledPower = bpBody.Calls.Any(s =>
            s.Call.Method.Name == "Add" && s.Call.Receiver is Call { Method.Name: "get_DoubledPowers" });

        var marksFinished = apBody.Calls.Any(s =>
            s.Call.Method.Name == "set_IsFinishedTriggering"
            && s.Call.Arguments is [Constant { Data: 1 or true }]
            && s.Conditions.Any(c => NormalizeCondition(c) is { Expected: false, Value: Call { Method.Name: "get_IsFinishedTriggering" } })
            && s.Conditions.Any(c =>
                IsEquality(NormalizeCondition(c), true, out var l, out var r)
                && ((l is Call { Method.Name: "get_Card" } && r is Call { Method.Name: "get_TriggeringCard" })
                    || (r is Call { Method.Name: "get_Card" } && l is Call { Method.Name: "get_TriggeringCard" }))));

        if (!recordsTriggerCard || !addsDoubledPower || !marksFinished)
            return false;

        handled.Add(beforeCombat.Name);
        handled.Add(beforePower.Name);
        handled.Add(multHook.Name);
        handled.Add(afterPlayed.Name);
        handled.Add(afterCombat.Name);
        var artifact = ModelLabel(typeof(ArtifactPower));
        line = $"{prefix}Once per combat, on the first card played by owner that applies a visible Debuff to an enemy (not blocked by {artifact}), multiplies visible Debuff stacks applied by that card by ×{Number(factor)} (skipping paired temporary-power follow-ups of already-doubled powers)";
        return true;
    }

    private bool ReadOncePerCombatReplayHook(Type type, string prefix, HashSet<string> handled, out string line)
    {
        line = "";
        if (FindOverride(type, "ModifyCardPlayCount") is not { } countHook
            || FindOverride(type, "AfterModifyingCardPlayCount") is not { } afterCount
            || FindOverride(type, "AfterRoomEntered") is not { } roomEntered
            || FindOverride(type, "AfterCombatEnd") is not { } combatEnd)
        {
            return false;
        }

        var countBody = Body(countHook);
        var afterBody = Body(afterCount);
        var roomBody = Body(roomEntered);
        var endBody = Body(combatEnd);
        if (countBody.Incomplete || afterBody.Incomplete || roomBody.Incomplete || endBody.Incomplete
            || countBody.Writes.Count > 0 || afterBody.Writes.Count > 0
            || roomBody.Writes.Count > 0 || endBody.Writes.Count > 0
            || countBody.FieldSteps.Count > 0 || afterBody.FieldSteps.Count > 0
            || roomBody.FieldSteps.Count > 0 || endBody.FieldSteps.Count > 0)
            return false;

        var boosted = countBody.Returns.Where(r => r.Value is not Input { Name: "playCount" }).ToArray();
        if (boosted is not [var active]
            || active.Value is not Operation { Operator: "add", Left: Input { Name: "playCount" }, Right: Constant { Data: int extra } }
            || extra <= 0)
        {
            return false;
        }

        var hasOwnerCheck = false;
        string? usedPropGetter = null;
        foreach (var rawCond in active.Conditions)
        {
            var cond = NormalizeCondition(rawCond);
            if (IsCardOwnerCheck(cond, "card"))
            {
                hasOwnerCheck = true;
                continue;
            }
            if (!cond.Expected && cond.Value is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                && getter.DeclaringType == type && getter.ReturnType == typeof(bool) && getter.Name.StartsWith("get_", StringComparison.Ordinal))
            {
                usedPropGetter = getter.Name;
                continue;
            }
            return false;
        }

        if (!hasOwnerCheck || usedPropGetter is null)
            return false;

        var setterName = "set_" + usedPropGetter[4..];
        bool OnlyAllowedSetter(NativeRuleBody b, bool allowSetter) =>
            b.Calls.All(s =>
                (allowSetter && s.Call.Method.DeclaringType == type && s.Call.Method.Name == setterName)
                || !IsGameplayEffectCall(s.Call.Method));

        if (!OnlyAllowedSetter(countBody, allowSetter: false)
            || !OnlyAllowedSetter(afterBody, allowSetter: true)
            || !OnlyAllowedSetter(roomBody, allowSetter: true)
            || !OnlyAllowedSetter(endBody, allowSetter: true))
            return false;

        var setsUsed = afterBody.Calls.Any(s =>
            s.Call.Method.DeclaringType == type && s.Call.Method.Name == setterName
            && s.Call.Arguments is [Constant { Data: 1 or true }]
            && s.Conditions.Length == 0);
        var resetsOnRoom = roomBody.Calls.Any(s =>
            s.Call.Method.DeclaringType == type && s.Call.Method.Name == setterName
            && s.Call.Arguments is [Constant { Data: 0 or false }]
            && s.Conditions is [var roomCond]
            && NormalizeCondition(roomCond) is { Expected: true, Value: Operation { Operator: "isinst", Right: Constant { Data: Type rt } } }
            && rt == typeof(CombatRoom));
        var resetsOnEnd = endBody.Calls.Any(s =>
            s.Call.Method.DeclaringType == type && s.Call.Method.Name == setterName
            && s.Call.Arguments is [Constant { Data: 0 or false }]
            && s.Conditions.Length == 0);
        if (!setsUsed || !resetsOnRoom || !resetsOnEnd)
            return false;

        handled.Add(countHook.Name);
        handled.Add(afterCount.Name);
        handled.Add(roomEntered.Name);
        handled.Add(combatEnd.Name);
        var timesText = extra == 1 ? "1 additional time" : $"{extra} additional times";
        line = $"{prefix}Once per combat, card replay: plays the first owned card played {timesText}";
        return true;
    }

    private bool ReadMarkerPowerRule(Type powerType, string prefix, out string line)
    {
        line = "";

        if (powerType == typeof(SeekingEdgePower))
        {
            if (!VerifyCardAoeMarkerPower(typeof(SovereignBlade), powerType, "get_HasSeekingEdge"))
            {
                Gap($"{powerType.Name}/marker verification");
                return false;
            }
            line = $"{prefix}While active on owner, owned {ModelLabel(typeof(SovereignBlade))} cards target and deal damage to all enemies instead of a single enemy";
            return true;
        }

        if (powerType == typeof(FanOfKnivesPower))
        {
            if (!VerifyCardAoeMarkerPower(typeof(Shiv), powerType, "get_HasFanOfKnives"))
            {
                Gap($"{powerType.Name}/marker verification");
                return false;
            }
            line = $"{prefix}While active on owner, owned {ModelLabel(typeof(Shiv))} cards target and deal damage to all enemies instead of a single enemy";
            return true;
        }

        if (powerType == typeof(ParryPower))
        {
            if (!VerifySovereignBladeParryMarker(powerType))
            {
                Gap($"{powerType.Name}/marker verification");
                return false;
            }
            line = $"{prefix}While active on owner, playing an owned {ModelLabel(typeof(SovereignBlade))} grants owner base Block equal to stacks after its attack resolves";
            return true;
        }

        if (powerType == typeof(AccelerantPower))
        {
            if (!VerifyPoisonAccelerantMarker(powerType))
            {
                Gap($"{powerType.Name}/marker verification");
                return false;
            }
            var poison = ModelLabel(typeof(PoisonPower));
            line = $"{prefix}While owner is alive, {poison} on enemies triggers +stacks additional times per turn (each trigger deals current stacks and decrements by 1; total triggers are capped at the enemy's current {poison} stacks)";
            return true;
        }

        if (powerType == typeof(TheHuntPower))
        {
            if (!VerifyTheHuntMarker(powerType))
            {
                Gap($"{powerType.Name}/marker verification");
                return false;
            }
            line = $"{prefix}Tracks extra card rewards already added to this combat room by {ModelLabel(typeof(TheHunt))} (no further in-combat effect)";
            return true;
        }

        return false;
    }

    private static MethodInfo UnwrapAsyncMoveNext(MethodInfo method)
        => method.GetCustomAttribute<System.Runtime.CompilerServices.AsyncStateMachineAttribute>()?.StateMachineType
            .GetMethod("MoveNext", DeclaredInstanceOnly) ?? method;

    private static bool MethodHasCall(MethodInfo method, Func<MethodBase, bool> predicate)
    {
        var target = UnwrapAsyncMoveNext(method);
        if (target.GetMethodBody() is null)
            return false;
        return NativeRuleBody.ReadOriginalInstructions(target)
            .Any(inst => inst.operand is MethodBase mb && predicate(mb));
    }

    private static bool VerifyCardAoeMarkerPower(Type cardType, Type powerType, string helperGetterName)
    {
        var helper = cardType.GetMethod(helperGetterName, DeclaredInstanceOnly);
        var targetTypeGetter = cardType.GetMethod("get_TargetType", DeclaredInstanceOnly);
        var onPlay = cardType.GetMethod("OnPlay", DeclaredInstanceOnly);
        if (helper is null || targetTypeGetter is null || onPlay is null)
            return false;

        var checksPower = MethodHasCall(helper, m =>
            m.DeclaringType == typeof(Creature) && m.Name == "HasPower"
            && m.IsGenericMethod && m.GetGenericArguments()[0] == powerType);
        var switchesTarget = MethodHasCall(targetTypeGetter, m => m == helper);
        var targetsAll = MethodHasCall(onPlay, m => m == helper)
            && MethodHasCall(onPlay, m => m.DeclaringType == typeof(AttackCommand) && m.Name == nameof(AttackCommand.TargetingAllOpponents));
        return checksPower && switchesTarget && targetsAll;
    }

    private static bool VerifySovereignBladeParryMarker(Type powerType)
    {
        var getter = typeof(SovereignBlade).GetMethod("GetOwnerParryAmount", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        var onPlay = typeof(SovereignBlade).GetMethod("OnPlay", DeclaredInstanceOnly);
        if (getter is null || onPlay is null)
            return false;

        var readsPower = MethodHasCall(getter, m =>
            m.DeclaringType == typeof(Creature) && m.Name == "GetPowerAmount"
            && m.IsGenericMethod && m.GetGenericArguments()[0] == powerType);
        var gainsBlock = MethodHasCall(onPlay, m => m == getter)
            && MethodHasCall(onPlay, m => m.DeclaringType == typeof(CreatureCmd) && m.Name == nameof(CreatureCmd.GainBlock));
        return readsPower && gainsBlock;
    }

    private static bool VerifyPoisonAccelerantMarker(Type powerType)
    {
        var triggerCount = typeof(PoisonPower).GetMethod("get_TriggerCount", DeclaredInstanceOnly);
        if (triggerCount is null)
            return false;

        var sumsMin = MethodHasCall(triggerCount, m => m.DeclaringType == typeof(Math) && m.Name == nameof(Math.Min))
            && MethodHasCall(triggerCount, m => m.DeclaringType == typeof(Enumerable) && m.Name == nameof(Enumerable.Sum));
        if (!sumsMin)
            return false;

        foreach (var nested in typeof(PoisonPower).GetNestedTypes(BindingFlags.Public | BindingFlags.NonPublic))
        {
            foreach (var m in nested.GetMethods(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
            {
                if (m.Name.Contains("TriggerCount", StringComparison.Ordinal)
                    && MethodHasCall(m, called =>
                        called.DeclaringType == typeof(Creature) && called.Name == "GetPowerAmount"
                        && called.IsGenericMethod && called.GetGenericArguments()[0] == powerType))
                {
                    return true;
                }
            }
        }
        return false;
    }

    private static bool VerifyTheHuntMarker(Type powerType)
    {
        var onPlay = typeof(TheHunt).GetMethod("OnPlay", DeclaredInstanceOnly);
        if (onPlay is null)
            return false;
        return MethodHasCall(onPlay, m => m.DeclaringType == typeof(CombatRoom) && m.Name == nameof(CombatRoom.AddExtraReward))
            && MethodHasCall(onPlay, m => m is ConstructorInfo && m.DeclaringType == typeof(CardReward))
            && MethodHasCall(onPlay, m =>
                m.DeclaringType == typeof(PowerCmd) && m.Name == nameof(PowerCmd.Apply)
                && m.IsGenericMethod && m.GetGenericArguments()[0] == powerType);
    }

    // Fail-closed IL handler for prioritized random hand-card SetToFreeThisTurn hooks (e.g., MummifiedHand).
    private bool ReadPrioritizedFreeHandCardHook(Type relicType, string prefix, HashSet<string> handled, out string line)
    {
        line = "";
        if (handled.Contains("AfterCardPlayed") || FindOverride(relicType, "AfterCardPlayed") is not { } method)
            return false;

        var body = Body(method);
        if (body.Incomplete || body.Writes.Count > 0 || body.FieldSteps.Count > 0)
            return false;

        var freeSteps = body.Calls.Where(s => s.Call.Method.DeclaringType == typeof(CardModel)
            && s.Call.Method.Name == nameof(CardModel.SetToFreeThisTurn)).ToArray();
        if (freeSteps.Length == 0)
            return false;

        // Ensure no other gameplay commands appear in AfterCardPlayed.
        foreach (var step in body.Calls)
        {
            var m = step.Call.Method;
            if (m.DeclaringType == typeof(CardModel) && m.Name == nameof(CardModel.SetToFreeThisTurn))
                continue;
            if (IsGameplayEffectCall(m))
                return false;
        }

        static bool IsOwnerHandCardPoolSource(Value? expr) => expr switch
        {
            Call { Method.Name: "get_Cards", Receiver: Call { Method.Name: "GetPile", Arguments: [Constant { Data: (int)PileType.Hand }, var pileOwner] } }
                when IsOwner(pileOwner) => true,
            Call { Method.Name: nameof(Enumerable.Where) or nameof(Enumerable.ToList), Arguments: [var inner, ..] } => IsOwnerHandCardPoolSource(inner),
            _ => false,
        };

        static bool IsHandNextItemCall(Value? expr)
            => expr is Call { Method.Name: "NextItem", Method.DeclaringType.Name: "Rng", Arguments: [var src] }
                && IsOwnerHandCardPoolSource(src);

        // Verify owner, CardType, hand-selection receiver, and every condition on every reaching path to SetToFreeThisTurn.
        CardType? requiredType = null;
        foreach (var step in freeSteps)
        {
            if (!IsHandNextItemCall(step.Call.Receiver))
                return false;
            var hasOwnerCheck = false;
            CardType? stepType = null;
            foreach (var rawCond in step.Conditions)
            {
                var cond = NormalizeCondition(rawCond);
                if (IsPlayedCardOwnerCheck(cond))
                {
                    hasOwnerCheck = true;
                    continue;
                }
                if (IsEquality(cond, true, out var l, out var r)
                    && l is Call { Method.Name: "get_Type", Receiver: Call { Method.Name: "get_Card" } }
                    && Numeric(r) is { } ctVal && Enum.IsDefined(typeof(CardType), (int)ctVal))
                {
                    stepType = (CardType)(int)ctVal;
                    continue;
                }
                if (cond is { Expected: true, Value: Call { Method.Name: "get_IsInProgress" } })
                    continue;
                if (IsHandNextItemCall(cond.Value))
                    continue;
                return false;
            }
            if (!hasOwnerCheck || stepType is null)
                return false;
            if (requiredType is null)
                requiredType = stepType;
            else if (requiredType != stepType)
                return false;
        }

        if (requiredType is not { } cardType)
            return false;

        // Verify the 4-tier random hand selection predicates directly from IL.
        var hasBaseCostPred = false;
        var hasCurrentCostPred = false;
        var nestedTypes = relicType.GetNestedTypes(BindingFlags.Public | BindingFlags.NonPublic);
        foreach (var nested in nestedTypes)
        {
            foreach (var pred in nested.GetMethods(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
            {
                if (pred.ReturnType != typeof(bool) || pred.GetParameters() is not [{ ParameterType: var pt }] || pt != typeof(CardModel))
                    continue;
                if (MethodHasCall(pred, m => m.DeclaringType == typeof(CardEnergyCost) && m.Name == nameof(CardEnergyCost.GetWithModifiers))
                    && MethodHasCall(pred, m => m.DeclaringType == typeof(CardModel) && m.Name == "get_BaseStarCost"))
                    hasBaseCostPred = true;
                if (MethodHasCall(pred, m => m.DeclaringType == typeof(CardModel) && m.Name == nameof(CardModel.CostsEnergyOrStars)))
                    hasCurrentCostPred = true;
            }
        }

        var readsHand = MethodHasCall(method, m => m.Name == "GetPile")
            && MethodHasCall(method, m => m.DeclaringType?.Name == "Rng" && m.Name == "NextItem");
        if (!readsHand || !hasBaseCostPred || !hasCurrentCostPred)
            return false;

        handled.Add(method.Name);
        var article = cardType == CardType.Attack ? "an" : "a";
        line = $"{prefix}After each card play resolves: if card player=owner and played card is {article} {cardType}: pick 1 random card in owner's hand (preferring cards with positive base and current Energy/Star cost, then positive current Energy/Star cost, then positive base Energy/Star cost, else any card in hand) and set its Energy and Star cost to 0 for this turn";
        return true;
    }

    private static bool IsPlayedCardOwnerCheck(Test cond)
        => IsEquality(cond, true, out var l, out var r)
            && ((l is Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card" } } && IsOwner(r))
                || (r is Call { Method.Name: "get_Owner", Receiver: Call { Method.Name: "get_Card" } } && IsOwner(l)));

    // Fail-closed IL handler for monster AfterAddedToRoom hooks whose initial-HP de-duplication loop causes NativeRuleBody to mark them incomplete (e.g., DecimillipedeSegment).
    private List<Effect>? TryReadSpecialIncompleteHook(MethodInfo method)
    {
        if (method.Name != "AfterAddedToRoom" || method.DeclaringType is not { } monsterType
            || !typeof(MonsterModel).IsAssignableFrom(monsterType))
            return null;

        var targetMethod = UnwrapAsyncMoveNext(method);
        if (targetMethod.GetMethodBody() is null || targetMethod.ContainsGenericParameters)
            return null;

        var instructions = NativeRuleBody.ReadOriginalInstructions(targetMethod);
        var setMaxHpIdx = -1;
        var hasTeammateCheck = false;
        var hasEvenParityCheck = false;
        var applyPowerIdx = -1;
        Type? appliedPowerType = null;
        int? appliedPowerAmount = null;

        for (var i = 0; i < instructions.Count; i++)
        {
            if (instructions[i].operand is not MethodBase called)
                continue;
            var decl = called.DeclaringType;
            var name = called.Name;

            if (decl == typeof(CreatureCmd) && name == nameof(CreatureCmd.SetMaxAndCurrentHp))
            {
                setMaxHpIdx = i;
                continue;
            }
            if (decl == typeof(PowerCmd) && name == nameof(PowerCmd.Apply) && called.IsGenericMethod
                && called.GetGenericArguments() is [var pt] && typeof(PowerModel).IsAssignableFrom(pt))
            {
                if (appliedPowerType is not null)
                    return null;
                appliedPowerType = pt;
                applyPowerIdx = i;
                if (!TryReadSelfPowerApplyAmount(instructions, i, out var exactAmt))
                    return null;
                appliedPowerAmount = exactAmt;
                continue;
            }
            if (name == "GetTeammatesOf")
            {
                hasTeammateCheck = true;
                continue;
            }
            if (decl == typeof(decimal) && name == "op_Modulus"
                && i >= 2
                && instructions[i - 1].operand is ConstructorInfo { DeclaringType: var modDecT } && modDecT == typeof(decimal)
                && TryReadIntConstant(instructions[i - 2], out var divisor) && divisor == 2
                && i + 2 < instructions.Count
                && instructions[i + 1].operand is FieldInfo { DeclaringType: var oneDecT, Name: nameof(decimal.One) } && oneDecT == typeof(decimal)
                && instructions[i + 2].operand is MethodBase { DeclaringType: var eqDecT, Name: "op_Equality" } && eqDecT == typeof(decimal))
            {
                hasEvenParityCheck = true;
                continue;
            }
            if (name == "AfterAddedToRoom" || name.StartsWith("<>n__", StringComparison.Ordinal))
                continue;
            if (IsGameplayEffectCall(called))
                return null;
        }

        if (setMaxHpIdx < 0 || applyPowerIdx <= setMaxHpIdx || !hasTeammateCheck || !hasEvenParityCheck
            || appliedPowerType is null || appliedPowerAmount is null)
            return null;

        // Verify PowerCmd.Apply runs unconditionally after SetMaxAndCurrentHp completes (no branch skipping it).
        var setMaxHpResultIdx = -1;
        for (var i = setMaxHpIdx + 1; i < applyPowerIdx; i++)
        {
            if (instructions[i].operand is MethodBase { Name: "GetResult" })
            {
                setMaxHpResultIdx = i;
                break;
            }
        }
        if (setMaxHpResultIdx < 0)
            return null;
        for (var i = setMaxHpResultIdx + 1; i < applyPowerIdx; i++)
        {
            if (instructions[i].opcode.FlowControl == System.Reflection.Emit.FlowControl.Cond_Branch)
                return null;
        }

        _powers.Add(appliedPowerType);
        return
        [
            new Effect("adjust initial Max HP and current HP to a distinct even value within initial HP range"),
            new Effect($"attempt to apply {appliedPowerAmount.Value.ToString(CultureInfo.InvariantCulture)} {PowerLabel(appliedPowerType)} to self", AddedPower: appliedPowerType),
        ];
    }

    // Verifies the exact 6-argument self-apply sequence:
    //   new PlayerChoiceContext(), this.Creature, new decimal(N), this.Creature, null, false -> PowerCmd.Apply<T>
    private static bool TryReadSelfPowerApplyAmount(List<CodeInstruction> instructions, int applyIdx, out int amount)
    {
        amount = 0;
        if (applyIdx < 9)
            return false;
        // Walk backward through the exact argument push pattern right before PowerCmd.Apply<T>.
        if (!TryReadIntConstant(instructions[applyIdx - 1], out var isSilent) || isSilent != 0)
            return false;
        if (instructions[applyIdx - 2].opcode.Name != "ldnull")
            return false;
        if (instructions[applyIdx - 3].operand is not MethodBase { Name: "get_Creature" })
            return false;
        // instructions[applyIdx - 4] loads `this` (e.g. ldloc.1 / ldarg.0).
        if (instructions[applyIdx - 5].operand is not ConstructorInfo { DeclaringType: var decT } || decT != typeof(decimal))
            return false;
        if (!TryReadIntConstant(instructions[applyIdx - 6], out var rawAmt))
            return false;
        if (instructions[applyIdx - 7].operand is not MethodBase { Name: "get_Creature" })
            return false;
        // instructions[applyIdx - 8] loads `this`, instructions[applyIdx - 9] constructs PlayerChoiceContext.
        if (instructions[applyIdx - 9].operand is not ConstructorInfo { DeclaringType.Name: "ThrowingPlayerChoiceContext" or "PlayerChoiceContext" })
            return false;

        amount = rawAmt;
        return true;
    }

    private static bool TryReadIntConstant(CodeInstruction inst, out int value)
    {
        value = 0;
        var op = inst.opcode.Name;
        switch (op)
        {
            case "ldc.i4.0": value = 0; return true;
            case "ldc.i4.1": value = 1; return true;
            case "ldc.i4.2": value = 2; return true;
            case "ldc.i4.3": value = 3; return true;
            case "ldc.i4.4": value = 4; return true;
            case "ldc.i4.5": value = 5; return true;
            case "ldc.i4.6": value = 6; return true;
            case "ldc.i4.7": value = 7; return true;
            case "ldc.i4.8": value = 8; return true;
            case "ldc.i4.m1": value = -1; return true;
            case "ldc.i4.s" when inst.operand is sbyte sb: value = sb; return true;
            case "ldc.i4" when inst.operand is int iv: value = iv; return true;
            default: return false;
        }
    }
}
