using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.ValueProps;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    private string? ReadChoicePower(Type power)
    {
        if (FindOverride(power, "AfterSideTurnEndLate") is { } endTurn && Body(endTurn) is { Incomplete: false } end
            && end.Calls.Where(step => step.Call.Method.DeclaringType == typeof(CreatureCmd)
                && step.Call.Method.Name == nameof(CreatureCmd.Damage)).DistinctBy(step => step.Index).ToArray() is [var damage]
            && damage.Conditions is [var participating] && IsHolderParticipating(participating)
            && IsOwner(Argument(damage.Call, "target")) && IsOwner(Argument(damage.Call, "dealer"))
            && IsStackAmount(Argument(damage.Call, "amount"))
            && Argument(damage.Call, "props") is Constant { Data: (int)ValueProp.Unpowered })
            return "at the end of the holder's turn, deal unpowered damage equal to the power's current stacks to the holder";

        if (PlayerSubtraction(power, "ModifyHandDraw", "count", floorAtZero: true))
            return "reduce the holder's turn-start draw by the power's current stacks, to a minimum of 0";
        if (PlayerSubtraction(power, "ModifyMaxEnergy", "amount", floorAtZero: false))
            return "reduce the holder's maximum Energy by the power's current stacks";
        if (HasCardPlayLimit(power))
            return "prevent the holder from starting another card play once the turn's play count, including replays and autoplay, reaches the power's current stacks";
        return null;
    }

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
            && left is Call { Method.Name: "get_Creature", Receiver: Call { Method.DeclaringType: var model,
                Method.Name: "get_Owner", Receiver: Input { Name: "card" } } }
            && model == typeof(CardModel) && IsOwner(right);

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
