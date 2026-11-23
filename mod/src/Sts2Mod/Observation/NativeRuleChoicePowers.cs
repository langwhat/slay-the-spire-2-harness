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
    private bool HasReadableChoicePower(Type power)
    {
        var probe = new NativeRuleText(monster);
        var lines = new List<string>();
        probe.ReadHooks(power, lines);
        return lines.Count > 0 && probe.Gaps.Count == 0;
    }

    private void ReadPlayerResourceHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (FindOverride(type, "ModifyHandDraw") is { } drawHook)
        {
            handled.Add(drawHook.Name);
            if (PlayerSubtraction(type, drawHook.Name, "count", floorAtZero: true))
                lines.Add($"{powerPrefix}Turn-start card draw: if player=holder: reduce by this power's current stacks (minimum 0)");
            else
                Gap($"{type.Name}.{drawHook.Name}");
        }
        if (FindOverride(type, "ModifyMaxEnergy") is { } energyHook)
        {
            handled.Add(energyHook.Name);
            if (PlayerSubtraction(type, energyHook.Name, "amount", floorAtZero: false))
                lines.Add($"{powerPrefix}Maximum Energy: if player=holder: reduce by this power's current stacks");
            else
                Gap($"{type.Name}.{energyHook.Name}");
        }
        if (HasCardPlayLimit(type))
        {
            handled.Add("BeforeCardPlayed");
            handled.Add("BeforeSideTurnStart");
            handled.Add("ShouldPlay");
            lines.Add($"{powerPrefix}Card play limit: prevent holder from starting another card play once the turn's play count (including replays and autoplay) reaches this power's current stacks; resets before holder's turn starts");
        }
    }

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
                ? $"(reduce applied stacks to 0; attempt to subtract one {PowerLabel(type)} stack)"
                : "reduce applied stacks to 0";
            lines.Add($"{powerPrefix}When a power would be applied to holder: if applied power is a visible Debuff: {outcome}");
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
            lines.Add($"{powerPrefix}HP loss cap per side turn: total unblocked HP lost by holder during each side turn is capped at this power's current stacks (resets before every side turn starts)");
        }
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

    private bool PlayerSubtraction(Type power, string hook, string operand, bool floorAtZero)
    {
        if (FindOverride(power, hook) is not { } method || Body(method) is not { Incomplete: false, Returns.Count: 2 } body)
            return false;
        var affected = body.Returns.SingleOrDefault(result => result.Conditions is [var test] && IsHolderPlayer(test, "player", true));
        var unchanged = body.Returns.SingleOrDefault(result => result.Conditions is [var test] && IsHolderPlayer(test, "player", false));
        if (affected is null || unchanged?.Value is not Input passThrough || passThrough.Name != operand)
            return false;
        var value = affected.Value;
        if (floorAtZero)
        {
            if (value is not Call { Method.DeclaringType: var math, Method.Name: nameof(Math.Max), Arguments: [var minimum, var inner] }
                || math != typeof(Math) || Numeric(minimum) != 0m)
                return false;
            value = inner;
        }
        return value is Call { Method.DeclaringType: var number, Method.Name: "op_Subtraction",
            Arguments: [Input source, var amount] } && number == typeof(decimal) && source.Name == operand && IsStackAmount(amount);
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
