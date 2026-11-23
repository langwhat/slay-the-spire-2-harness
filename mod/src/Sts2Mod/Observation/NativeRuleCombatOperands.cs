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
        if (Argument(call, "card") is not Call { Method: { Name: "CreateCard", IsGenericMethod: true } create, Arguments: [var player] }
            || create.DeclaringType != typeof(ICombatState) || !typeof(CardModel).IsAssignableFrom(create.GetGenericArguments()[0])
            || ValueText(player) is not { } recipient || Argument(call, "newPileType") is not Constant { Data: int pile }
            || Argument(call, "position") is not Constant { Data: int position })
            return null;
        var where = (PileType)pile == PileType.Draw && (CardPilePosition)position != CardPilePosition.Bottom
            ? $" ({((CardPilePosition)position).ToString().ToLowerInvariant()} position)"
            : "";
        return new Effect($"add 1 {ModelLabel(create.GetGenericArguments()[0])} to {recipient}'s {((PileType)pile).ToString().ToLowerInvariant()} pile{where}");
    }

    private static string? CardSlot(Field field)
        => field.Member.FieldType == typeof(CardModel)
            && field.Receiver is Call { Method: { Name: "GetInternalData", DeclaringType: var origin, IsGenericMethod: true } method,
                Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && origin == typeof(PowerModel) && method.GetGenericArguments()[0] == field.Member.DeclaringType
                ? "recorded card instance"
                : null;

    private static Effect? DescribeCardSlot(Assignment assignment, MethodInfo method)
        => assignment.Assigned switch
        {
            Constant { Data: null } => new Effect("clear the recorded card instance",
                Trigger: method.Name == "AfterCardPlayed" ? "After each card play fully resolves (after all its hits)" : null, ChangesGuard: true),
            Input { Name: "cardSource", Type: var type } when type == typeof(CardModel) && method.Name == "AfterDamageReceived"
                => new Effect("record the source card instance", Trigger: AfterEachReceivedHit, ChangesGuard: true),
            _ => null,
        };

    private static bool IsUnchangedAssignment(EffectStep step)
        => step.Action is Assignment { Assigned: Input { Name: "cardSource" } source } assignment
            && step.Conditions.Any(test => test is { Expected: true, Value: Operation { Operator: "beq", Left: var left, Right: var right } }
                && (left == source && Key(right) == Key(assignment.Target) || right == source && Key(left) == Key(assignment.Target)));

    // Keep the original decision in scope when an effect writes to a value tested by it.
    private List<GuardedEffects> GroupGuardedChanges(List<GuardedEffects> effects)
    {
        var groups = new List<GuardedEffects>();
        for (var index = 0; index < effects.Count;)
        {
            var first = effects[index++];
            if (first.Repetitions is not null || first.Guard.Length == 0 || !first.Effects.Any(effect => effect.ChangesGuard))
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
            groups.Add(first with { Effects = [Combine(steps, $"({string.Join("; ", steps.Select(step => step.Text))})")] });
        }
        return groups;
    }

    private string? CombatCondition(Test test)
    {
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

        if (test.Value is Field field && CardSlot(field) is { } slot)
            return expected ? $"{slot} exists" : $"no {slot}";

        if (test.Value is Operation { Operator: "beq" or "ceq" or "bne.un", Left: Input { Name: "side" },
            Right: Call { Method.DeclaringType: var creature, Method.Name: "get_Side", Receiver: var holder } } comparison
            && creature == typeof(Creature) && IsOwner(holder))
            return expected == (comparison.Operator != "bne.un") ? "turn side is holder's side" : "turn side is opposing side";

        return null;
    }

    private string? CombatValue(Value value) => value switch
    {
        Field { Member: var field, Receiver: Input { Name: "this" } } when _choiceCursors.Contains(field) => "choice rounds completed",
        Field field => CardSlot(field),
        Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            when getter.DeclaringType == monster.GetType() && _choiceCursors.Count != 0
            && Body(getter) is { Incomplete: false, Returns: [{ Value: Field { Receiver: Input { Name: "this" }, Member: var cursor }, Conditions.Length: 0 }] }
            && _choiceCursors.Contains(cursor) => "choice rounds completed",
        Call { Method.Name: "get_Monster", Method.DeclaringType: var type, Receiver: Input { Name: "dealer" } } when type == typeof(Creature)
            => "damage dealer",
        Call { Method.Name: "get_PetOwner", Method.DeclaringType: var type, Receiver: Input { Name: "dealer" } } when type == typeof(Creature)
            => "damage dealer's owner",
        Call { Method.Name: "get_Player", Method.DeclaringType: var type, Receiver: Call { Method.Name: "get_Creature", Receiver: Call { Method.Name: "get_PetOwner" } pet } }
            when type == typeof(Creature) => ValueText(pet),
        Call { Method.Name: "get_UnblockedDamage" or "get_TotalDamage", Method.DeclaringType: var type, Receiver: { } hit } damage
            when type == typeof(DamageResult) && (AttackHit(hit) ?? ValueText(hit)) is { } label
            => $"{label} {(damage.Method.Name == "get_UnblockedDamage" ? "unblocked" : "total")} damage",
        Input { Type: var type } when type == typeof(DamageResult) => "this hit's",
        Call { Method.Name: "get_Amount", Method.DeclaringType: var type, Receiver: { } power }
            when type == typeof(PowerModel) && OwnedPower(power) is { } kind => $"self's {ModelLabel(kind)} stacks",
        Call call when OwnedPower(call) is { } power => $"self's {ModelLabel(power)}",
        _ => AttackHit(value),
    };

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
        return "the first hit on the holder in this attack";
    }
}
