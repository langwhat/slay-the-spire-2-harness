using System.Reflection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Powers;
using MegaCrit.Sts2.Core.Models;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    [ThreadStatic]
    private static HashSet<Type>? _activeChoicePowerProbes;

    private bool HasReadableChoicePower(Type power)
    {
        _activeChoicePowerProbes ??= [];
        if (!_activeChoicePowerProbes.Add(power))
            return false;
        try
        {
            var probe = new NativeRuleText(monster);
            var lines = new List<string>();
            probe.ReadHooks(power, lines);
            return lines.Count > 0 && probe.Gaps.Count == 0;
        }
        finally
        {
            _activeChoicePowerProbes.Remove(power);
        }
    }

    private void ReadPlayerResourceHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "ModifyHandDraw") is { } drawHook)
        {
            handled.Add(drawHook.Name);
            if (ReadPlayerDeltaHook(type, drawHook, "count", "Turn-start card draw") is { } drawLine)
                lines.Add($"{powerPrefix}{drawLine}");
            else
                Gap($"{type.Name}.{drawHook.Name}");
        }
        if (FindOverride(type, "ModifyMaxEnergy") is { } energyHook)
        {
            handled.Add(energyHook.Name);
            if (ReadPlayerDeltaHook(type, energyHook, "amount", "Maximum Energy") is { } energyLine)
                lines.Add($"{powerPrefix}{energyLine}");
            else
                Gap($"{type.Name}.{energyHook.Name}");
        }
        if (HasCardPlayLimit(type))
        {
            handled.Add("BeforeCardPlayed");
            handled.Add("BeforeSideTurnStart");
            handled.Add("ShouldPlay");
            lines.Add($"{powerPrefix}Card play limit: prevent owner from starting another card play once the turn's play count (including replays and autoplay) reaches stacks; resets before owner's turn starts");
        }
        foreach (var costHookName in new[] { "TryModifyEnergyCostInCombat", "TryModifyEnergyCostInCombatLate" })
        {
            if (FindOverride(type, costHookName) is not { } costHook)
                continue;
            handled.Add(costHook.Name);
            if (ReadEnergyCostHook(costHook) is { } costLine)
                lines.Add($"{powerPrefix}{costLine}");
            else
                Gap($"{type.Name}.{costHook.Name}");
        }
        if (FindOverride(type, "ModifyCardPlayCount") is { } playCountHook)
        {
            handled.Add(playCountHook.Name);
            if (ReadCardPlayCountHook(type, playCountHook, handled) is { } playCountLine)
                lines.Add($"{powerPrefix}{playCountLine}");
            else
                Gap($"{type.Name}.{playCountHook.Name}");
        }
        if (FindOverride(type, "ModifyCardPlayResultLocation") is { } playLocHook)
        {
            handled.Add(playLocHook.Name);
            if (ReadCardPlayResultLocationHook(type, playLocHook, handled) is { } playLocLine)
                lines.Add($"{powerPrefix}{playLocLine}");
            else
                Gap($"{type.Name}.{playLocHook.Name}");
        }
    }

    private string? ReadEnergyCostHook(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var active = body.Returns.Where(r => TryTruth(r.Value, out var m) && m).ToArray();
        var store = body.OutStores.LastOrDefault(s => s.Assigned is not Input);
        if (active is not [var ret] || store is null)
            return null;

        CardType? filteredType = null;
        Type? requiredAffliction = null;
        foreach (var cond in ret.Conditions)
        {
            if (IsEquality(cond, true, out var l, out var r) && l is Call { Method.Name: "get_Type" } && Numeric(r) is { } ct
                && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                filteredType = (CardType)(int)ct;
            }
            if (cond is { Expected: true, Value: Operation { Operator: "isinst",
                    Left: Call { Method.Name: "get_Affliction" }, Right: Constant { Data: Type affType } } }
                && typeof(AfflictionModel).IsAssignableFrom(affType))
            {
                requiredAffliction = affType;
            }
        }
        var subject = requiredAffliction is not null
            ? $"owned cards afflicted with {ModelLabel(requiredAffliction)}"
            : filteredType is { } kind
                ? $"owned {kind} cards"
                : "owned cards";
        if (Numeric(store.Assigned) == 0m)
            return $"In-combat Energy cost: {subject} cost 0 Energy";
        if (store.Assigned is Call { Method.Name: "op_Addition", Arguments: [Input { Name: "originalCost" }, var delta] }
            && IsStackAmount(delta))
        {
            return $"In-combat Energy cost: {subject} cost +stacks Energy";
        }
        return null;
    }

    private string? ReadCardPlayCountHook(Type power, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var boosted = body.Returns.Where(r => r.Value is Operation { Operator: "add", Left: Input { Name: "playCount" }, Right: Constant { Data: 1 } }).ToArray();
        if (boosted is not [var active])
            return null;

        CardType? filteredType = null;
        var firstPlaysPerTurn = false;
        foreach (var cond in active.Conditions)
        {
            if (IsEquality(cond, true, out var l, out var r) && l is Call { Method.Name: "get_Type" } && Numeric(r) is { } ct
                && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                filteredType = (CardType)(int)ct;
            }
            if (cond.Value is Operation { Left: Call { Method.Name: nameof(Enumerable.Count), Arguments: [Call { Method.Name: "get_CardPlaysStarted" }, ..] } })
                firstPlaysPerTurn = true;
        }

        var consumeSuffix = "";
        if (FindOverride(power, "AfterModifyingCardPlayCount") is { } afterHook)
        {
            if (IsSelfDecrement(afterHook))
            {
                handled.Add(afterHook.Name);
                consumeSuffix = $", then subtract 1 {PowerLabel(power)} stack";
            }
            else if (IsPurePresentationHook(afterHook))
            {
                handled.Add(afterHook.Name);
            }
        }

        var subject = firstPlaysPerTurn
            ? "each of owner's first N non-dupe card plays each turn (where N = stacks)"
            : filteredType is { } kind
                ? $"owned non-dupe {kind} cards"
                : "owned non-dupe cards";
        return $"Card replay: plays {subject} 1 additional time{consumeSuffix}";
    }

    private string? ReadCardPlayResultLocationHook(Type power, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;

        int? rawPile = null;
        Test[]? conditions = null;
        var redirected = body.Returns.Where(r => r.Value is Constant { Data: int p } && Enum.IsDefined(typeof(PileType), p)).ToArray();
        if (redirected is [var activeRet] && activeRet.Value is Constant { Data: int retPile })
        {
            rawPile = retPile;
            conditions = activeRet.Conditions;
        }
        else if (body.FieldSteps.FirstOrDefault(f => f.Assignment.Target.Receiver is Input { Name: "location" }
                && f.Assignment.Assigned is Constant { Data: int fp } && Enum.IsDefined(typeof(PileType), fp)) is { } locStep
            && locStep.Assignment.Assigned is Constant { Data: int fieldPile })
        {
            rawPile = fieldPile;
            conditions = locStep.Conditions;
        }
        if (rawPile is null || conditions is null)
            return null;

        CardType? filteredType = null;
        foreach (var cond in conditions)
        {
            if (IsEquality(cond, true, out var l, out var r) && l is Call { Method.Name: "get_Type" } && Numeric(r) is { } ct
                && Enum.IsDefined(typeof(CardType), (int)ct))
            {
                filteredType = (CardType)(int)ct;
            }
        }
        if (FindOverride(power, "AfterModifyingCardPlayResultLocation") is { } afterLoc && IsPurePresentationHook(afterLoc))
            handled.Add(afterLoc.Name);

        var subject = filteredType is { } kind ? $"owned {kind} cards" : "owned cards";
        var destPile = ((PileType)rawPile.Value).ToString().ToLowerInvariant();
        return $"Card destination after play: {subject} move to {destPile} pile instead of discard pile";
    }

    private bool IsPurePresentationHook(MethodInfo method)
        => Body(method) is { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 } body
            && body.Calls.All(step => IsBenignReadOrPlumbingCall(step.Call.Method));

    private void ReadPowerReceptionHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "TryModifyPowerAmountReceived") is not { } modify)
            return;
        handled.Add(modify.Name);
        if (IsVisibleDebuffNegation(modify))
        {
            var decrement = FindOverride(type, "AfterModifyingPowerAmountReceived") is { } after && IsSelfDecrement(after);
            if (decrement)
                handled.Add("AfterModifyingPowerAmountReceived");
            var outcome = decrement
                ? $"(reduce applied stacks to 0; attempt to subtract 1 {PowerLabel(type)} stack)"
                : "reduce applied stacks to 0";
            lines.Add($"{powerPrefix}When a power would be applied to owner: if applied power is a visible Debuff: {outcome}");
        }
        else
            Gap($"{type.Name}.{modify.Name}");
    }

    private bool IsVisibleDebuffNegation(MethodInfo method)
    {
        if (Body(method) is not { Incomplete: false } body)
            return false;
        var blocked = body.Returns.Where(result => TryTruth(result.Value, out var modified) && modified).ToArray();
        var passed = body.Returns.Where(result => TryTruth(result.Value, out var modified) && !modified).ToArray();
        if (blocked.Length != 1 || passed.Length == 0)
            return false;
        var writesZero = body.OutStores.Any(store => store.Parameter.Name == "modifiedAmount" && Numeric(store.Assigned) == 0m);
        if (!writesZero)
            return false;
        var conditions = blocked[0].Conditions;
        var checksOwner = conditions.Any(test => IsEquality(test, true, out var left, out var right)
            && left is Input { Name: "target" } && IsOwner(right));
        var checksDebuff = conditions.Any(test => IsEquality(test, true, out var left, out var right)
            && left is Call { Method.Name: "GetTypeForAmount", Receiver: Input { Name: "canonicalPower" } }
            && Numeric(right) == (int)PowerType.Debuff);
        var checksVisible = conditions.Any(test => test is { Expected: true, Value: Call
            { Method.Name: "get_IsVisible", Receiver: Input { Name: "canonicalPower" } } });
        return checksOwner && checksDebuff && checksVisible;
    }

    private bool IsSelfDecrement(MethodInfo method)
        => Body(method) is { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 } body
            && body.Calls.Where(step => step.Call.Method.DeclaringType == typeof(PowerCmd)).DistinctBy(step => step.Index).ToArray()
                is [{ Conditions.Length: 0, Call: { Method.Name: nameof(PowerCmd.Decrement), Arguments: [Input { Name: "this" }] } }];

    private void ReadDamageCapHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "ModifyDamageCap") is { } capHook)
        {
            handled.Add(capHook.Name);
            var rendered = ReadDamageCap(capHook);
            if (rendered is null)
                Gap($"{type.Name}.{capHook.Name}");
            else if (rendered.Count > 0)
                lines.Add($"{powerPrefix}Damage cap per hit: {string.Join("; ", rendered)}");
        }
        if (FindOverride(type, "ModifyHpLostAfterOsty") is { } hpCapHook)
        {
            handled.Add(hpCapHook.Name);
            var rendered = ReadHpLossCap(hpCapHook);
            if (rendered is null)
                Gap($"{type.Name}.{hpCapHook.Name}");
            else if (rendered.Count > 0)
                lines.Add($"{powerPrefix}HP loss cap per hit, after block: {string.Join("; ", rendered)}");
        }
        if (HasSideTurnHpLossCap(type))
        {
            handled.Add("ModifyHpLostBeforeOstyLate");
            handled.Add("AfterDamageReceived");
            handled.Add("BeforeSideTurnStart");
            lines.Add($"{powerPrefix}HP loss cap per side turn: total unblocked HP lost by owner during each side turn is capped at stacks (resets before every side turn starts)");
        }
        if (FindOverride(type, "ModifyHpLostAfterOstyLate") is { } bufferHook)
        {
            handled.Add(bufferHook.Name);
            if (ReadZeroHpLossLateHook(type, bufferHook, handled) is { } bufferLine)
                lines.Add($"{powerPrefix}{bufferLine}");
            else
                Gap($"{type.Name}.{bufferHook.Name}");
        }
    }

    private string? ReadZeroHpLossLateHook(Type power, MethodInfo hook, HashSet<string> handled)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var zeroed = body.Returns.Where(r => Numeric(r.Value) == 0m).ToArray();
        if (zeroed.Length == 0)
            return null;
        var consumeSuffix = "";
        if (FindOverride(power, "AfterModifyingHpLostAfterOsty") is { } afterHook && IsSelfDecrement(afterHook))
        {
            handled.Add(afterHook.Name);
            consumeSuffix = $" and subtract 1 {PowerLabel(power)} stack";
        }
        return $"Prevent next HP loss: when owner would lose nonzero HP (checked after Block and {ModelLabel(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty))} redirection), reduce HP loss to 0{consumeSuffix}";
    }

    private List<string>? ReadDamageCap(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            if (Numeric(result.Value) == decimal.MaxValue)
                outcomes.Add(("", result.Conditions));
            else if (ValueText(result.Value) is { } cap)
                outcomes.Add(($"at most {cap}", result.Conditions));
            else
                return null;
        }
        return RenderOutcomes(outcomes, body.Terminals.Where(terminal => !terminal.Returned).Select(terminal => terminal.Conditions));
    }

    private List<string>? ReadHpLossCap(MethodInfo hook)
    {
        var body = Body(hook);
        if (body.Incomplete)
            return null;
        var capped = body.Returns.Where(result => result.Value is not Input { Name: "amount" }).ToArray();
        if (capped is not [var active] || ValueText(active.Value) is not { } bound)
            return null;
        var ownerOnly = active.Conditions.Where(test =>
            !(test.Value is Call { Method.Name: "op_LessThan", Method.DeclaringType: var d, Arguments: [Input { Name: "amount" }, var limit] }
              && d == typeof(decimal) && ValueText(limit) == bound)).ToArray();
        return Guard(ownerOnly) is { } guard && guard != Never
            ? [When(guard, $"at most {bound}")]
            : null;
    }

    private bool HasSideTurnHpLossCap(Type power)
    {
        if (FindOverride(power, "ModifyHpLostBeforeOstyLate") is not { } cap
            || FindOverride(power, "AfterDamageReceived") is not { } track
            || FindOverride(power, "BeforeSideTurnStart") is not { } reset
            || Body(cap) is not { Incomplete: false } capBody
            || Body(track) is not { Incomplete: false } trackBody
            || Body(reset) is not { Incomplete: false, FieldSteps: [var clear] })
            return false;
        if (clear.Conditions.Length != 0 || InternalDataField(clear.Assignment.Target) is not { } counter
            || Numeric(clear.Assignment.Assigned) != 0m)
            return false;
        var usesRemaining = capBody.Returns.Any(result => result.Value is Call
            { Method.Name: nameof(Math.Min), Arguments: [Input { Name: "amount" },
                Call { Method.Name: "op_Subtraction", Arguments: [var max, Field recorded] }] }
            && IsStackAmount(max) && InternalDataField(recorded) == counter);
        var addsUnblocked = trackBody.FieldSteps.Any(step => InternalDataField(step.Assignment.Target) == counter
            && step.Assignment.Assigned is Call { Method.Name: "op_Addition" });
        return usesRemaining && addsUnblocked;
    }

    private static FieldInfo? InternalDataField(Field field)
        => field.Receiver is Call { Method: { Name: "GetInternalData", IsGenericMethod: true } getter,
            Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && getter.DeclaringType == typeof(PowerModel)
            && getter.GetGenericArguments()[0] == field.Member.DeclaringType
                ? field.Member
                : null;

    private string? ReadPlayerDeltaHook(Type power, MethodInfo hook, string operand, string label)
    {
        var body = Body(hook);
        if (body.Incomplete || body.Returns.Count == 0)
            return null;

        var outcomes = new List<(string Text, Test[] Conditions)>();
        foreach (var result in body.Returns)
        {
            var value = result.Value;
            if (value is Input pass && pass.Name == operand)
            {
                outcomes.Add(("", result.Conditions));
                continue;
            }
            var floored = false;
            if (value is Call { Method.DeclaringType: var math, Method.Name: nameof(Math.Max), Arguments: [var minimum, var inner] }
                && math == typeof(Math) && Numeric(minimum) == 0m)
            {
                floored = true;
                value = inner;
            }
            if (value is Call { Method.DeclaringType: var number, Method.Name: "op_Addition" or "op_Subtraction", Arguments: [Input source, var delta] }
                && number == typeof(decimal) && source.Name == operand && ValueText(delta) is { } deltaText)
            {
                var verb = value is Call { Method.Name: "op_Addition" } ? "increase by" : "reduce by";
                var suffix = floored ? " (minimum 0)" : "";
                outcomes.Add(($"{verb} {deltaText}{suffix}", result.Conditions));
                continue;
            }
            return null;
        }
        var rendered = RenderOutcomes(outcomes, body.Terminals.Where(t => !t.Returned).Select(t => t.Conditions));
        return rendered is { Count: > 0 } ? $"{label}: {string.Join("; ", rendered)}" : null;
    }

    private bool HasCardPlayLimit(Type power)
    {
        if (FindOverride(power, "ShouldPlay") is not { } check || Body(check) is not { Incomplete: false, Returns.Count: 2 } query
            || query.Returns.SingleOrDefault(result => result.Conditions is [var test] && IsCardOwner(test, true))?.Value
                is not Operation { Operator: "clt", Left: Field { Receiver: Input { Name: "this" } } counter, Right: var limit }
            || !IsStackAmount(limit)
            || query.Returns.SingleOrDefault(result => result.Conditions is [var test] && IsCardOwner(test, false))?.Value is not Constant { Data: 1 }
            || FindOverride(power, "BeforeCardPlayed") is not { } increment
            || Body(increment) is not { Incomplete: false, FieldSteps: [var add] }
            || add.Assignment.Target != counter || add.Assignment.Assigned is not Operation { Operator: "add", Left: Field previous, Right: Constant { Data: 1 } }
            || previous != counter || add.Conditions is not [var playing] || !IsHolderPlayer(playing, "cardPlay", true)
            || FindOverride(power, "BeforeSideTurnStart") is not { } reset
            || Body(reset) is not { Incomplete: false, FieldSteps: [var clear] }
            || clear.Assignment.Target != counter || clear.Assignment.Assigned is not Constant { Data: 0 }
            || clear.Conditions is not [var participating] || !IsHolderParticipating(participating))
            return false;
        return true;
    }

    private static bool IsCardOwner(Test test, bool expected)
        => IsEquality(test, expected, out var left, out var right)
            && left is Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_Owner",
                Receiver: Input { Name: "card" } } }
            && IsOwner(right);

    private static bool IsHolderPlayer(Test test, string operand, bool expected)
        => IsEquality(test, expected, out var left, out var right)
            && right is Call { Method.DeclaringType: var creature, Method.Name: "get_Player", Receiver: var holder }
            && creature == typeof(Creature) && IsOwner(holder)
            && (left is Input input && input.Name == operand
                || left is Call { Method.DeclaringType: var model, Method.Name: "get_Owner",
                    Receiver: Call { Method.DeclaringType: var play, Method.Name: "get_Card", Receiver: Input cardPlay } }
                && model == typeof(CardModel) && play == typeof(CardPlay) && cardPlay.Name == operand);

    private static bool IsEquality(Test test, bool expected, out Value? left, out Value? right)
    {
        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un" } comparison
            && test.Expected == (expected == (comparison.Operator != "bne.un")))
        {
            left = comparison.Left;
            right = comparison.Right;
            return true;
        }
        left = right = null;
        return false;
    }

    private static bool IsHolderParticipating(Test test)
        => test is { Expected: true, Value: Call { Method.DeclaringType: var enumerable, Method.Name: nameof(Enumerable.Contains),
            Arguments: [Input { Name: "participants" }, var holder] } } && enumerable == typeof(Enumerable) && IsOwner(holder);

    private static bool IsStackAmount(Value? amount)
    {
        if (amount is Call { Method.DeclaringType: var convert, Method.Name: "op_Implicit", Arguments: [var inner] } && convert == typeof(decimal))
            amount = inner;
        return amount is Call { Method.DeclaringType: var power, Method.Name: "get_Amount", Receiver: Input { Name: "this" } }
            && power == typeof(PowerModel);
    }
}
