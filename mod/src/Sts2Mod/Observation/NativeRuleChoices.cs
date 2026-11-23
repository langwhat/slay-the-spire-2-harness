using System.Collections;
using System.Reflection;
using System.Runtime.CompilerServices;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    private readonly Dictionary<FieldInfo, int> _choiceRoundCounts = [];
    private readonly HashSet<FieldInfo> _choiceCursors = [];

    // The encounter offers a static table of card models, but invokes a callback on the
    // selected instance. Inspect the callbacks and captured parameters without choosing a card.
    private string? ReadImmediateChoice(Call call, MethodInfo caller)
    {
        if (call.Method is not MethodInfo chooser || chooser == caller || chooser.DeclaringType != caller.DeclaringType
            || chooser.ReturnType != typeof(Task) || call.Receiver != Self
            || call.Arguments is not [Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator",
                Receiver: Input { Name: "targets" } } }])
            return null;
        var selectionBody = Body(chooser);
        if (selectionBody.Incomplete || selectionBody.Writes.Count != 0
            || selectionBody.Calls.Where(step => step.Call.Method.DeclaringType == typeof(CardSelectCmd)
            && step.Call.Method.Name == nameof(CardSelectCmd.FromChooseACardScreen)).DistinctBy(step => step.Index).ToArray() is not [var screen])
            return null;
        var selection = screen.Call;
        if (Argument(selection, "context") is not Call { Method: ConstructorInfo { DeclaringType: var context } }
            || context != typeof(BlockingPlayerChoiceContext)
            || Argument(selection, "canSkip") is not Constant { Data: 0 }
            || Argument(selection, "cards") is not Call { Method.Name: nameof(Enumerable.ToList), Method.DeclaringType: var toList,
                Arguments: [Call { Method.Name: nameof(Enumerable.Select), Method.DeclaringType: var select, Arguments:
                    [Call { Method.Name: "get_Item", Receiver: Field { Member: var table, Receiver: null },
                        Arguments: [Call { Method: MethodInfo cursor, Receiver: Input { Name: "this" }, Arguments.Length: 0 }] },
                    Call { Method: ConstructorInfo, Arguments: [Call { Method: ConstructorInfo } closure, Constant { Data: MethodInfo mapper }] }] }] }
            || toList != typeof(Enumerable) || select != typeof(Enumerable)
            || closure.Method.DeclaringType != mapper.DeclaringType
            || mapper.DeclaringType?.IsDefined(typeof(CompilerGeneratedAttribute), inherit: false) != true
            || table.DeclaringType != chooser.DeclaringType || !table.IsInitOnly
            || !typeof(IEnumerable).IsAssignableFrom(table.FieldType)
            || Body(cursor) is not { Incomplete: false, Returns: [{ Value: Field { Receiver: Input { Name: "this" }, Member: var counter }, Conditions.Length: 0 }] }
            || selectionBody.FieldSteps.Any(step => Key(step.Assignment.Target.Receiver) != Key(closure))
            || table.GetValue(null) is not IEnumerable rows)
            return null;
        if (Argument(selection, "player") is not Call { Method.DeclaringType: var creature, Method.Name: "get_Player", Receiver: Field target }
            || creature != typeof(Creature) || Key(target.Receiver) != Key(closure)
            || CapturedValue(selectionBody, target) is not Input { Type: var targetType } || targetType != typeof(Creature)
            || !screen.Conditions.Any(test => test is { Expected: false, Value: Call { Method.DeclaringType: var type,
                Method.Name: "get_IsDead", Receiver: { } receiver } } && type == typeof(Creature) && Key(receiver) == Key(target))
            || ReadSelectedCallback(selectionBody, selection) is not { } callback)
            return null;

        var copy = Body(mapper);
        if (copy is not { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 } || !CopiesChoiceCard(copy, target, callback.DeclaringType)
            || ReadChoiceOverrides(copy, selectionBody, closure, cursor) is not { } overrides
            || !copy.Calls.All(step => IsCardCopyCall(step.Call.Method))
            || !selectionBody.Calls.All(step => IsChoiceScreenCall(step.Call.Method, selection.Method, callback, cursor, closure.Method, overrides)))
            return null;

        var rounds = new List<string>();
        foreach (var row in rows)
        {
            if (row is not IEnumerable options || rounds.Count >= 16)
                return null;
            var choices = options.Cast<object>().ToArray();
            if (choices.Length is 0 or > 16 || choices.Any(choice => choice is not CardModel))
                return null;
            var summaries = choices.Cast<CardModel>().Select(card => ChoiceOption(card, callback, overrides, rounds.Count)).ToArray();
            if (summaries.Any(summary => summary is null))
                return null;
            rounds.Add($"Round {rounds.Count + 1}: {string.Join(" or ", summaries)}");
        }
        if (rounds.Count == 0 || overrides.Any(entry => entry.Values.Length != rounds.Count))
            return null;
        _choiceRoundCounts[table] = rounds.Count;
        _choiceCursors.Add(counter);
        return $"each living player target chooses one displayed option. It resolves immediately on selection without entering a pile or the deck. "
            + string.Join("; ", rounds);
    }

    private static MethodInfo? ReadSelectedCallback(NativeRuleBody body, Call selection)
    {
        // The selection itself, its null check, and the awaited callback must refer to the same card.
        var offers = body.Calls.Where(step => step.Call is { Method: MethodInfo
                { DeclaringType.IsInterface: true, Name: "OnChosen", ReturnType: var task },
                Receiver: Operation { Operator: "castclass", Left: { } chosen }, Arguments.Length: 0 }
            && task == typeof(Task) && IsSelectionResult(chosen, selection)
            && step.Conditions.Any(test => test.Expected && IsSelectionResult(test.Value, selection)))
            .DistinctBy(step => step.Index).ToArray();
        if (offers is not [var callback])
            return null;
        return body.Calls.Any(step => step.Call is { Method.Name: "GetResult",
            Receiver: Call { Method.Name: "GetAwaiter", Receiver: { } awaited } } && Key(awaited) == Key(callback.Call))
            ? (MethodInfo)callback.Call.Method
            : null;
    }

    private static bool IsSelectionResult(Value value, Call selection)
        => value is Call { Method.Name: "GetResult", Receiver: Call { Method.Name: "GetAwaiter", Receiver: Call screen } }
            && Key(screen) == Key(selection);

    private static Value? CapturedValue(NativeRuleBody body, Field capture)
    {
        var writes = body.FieldSteps.Where(step => Key(step.Assignment.Target) == Key(capture))
            .DistinctBy(step => (step.Index, Key(step.Assignment.Assigned))).ToArray();
        return writes is [var write] ? write.Assignment.Assigned : null;
    }

    private static bool CopiesChoiceCard(NativeRuleBody mapper, Field target, Type? callbackType)
        => mapper.Returns.Count > 0 && mapper.Returns.All(result => result.Value is Call
        {
            Method.DeclaringType: var state, Method.Name: "CreateCard", Arguments:
            [
                Operation { Operator: "castclass", Left: Input { Type: var option }, Right: Constant { Data: Type card } },
                Call { Method.DeclaringType: var creature, Method.Name: "get_Player", Receiver: Field { Receiver: Input { Name: "this" } } captured },
            ],
        } && state == typeof(ICombatState) && creature == typeof(Creature) && card == typeof(CardModel)
            && option == callbackType && captured.Member == target.Member);

    private sealed record ChoiceOverride(Type Card, string Variable, MethodInfo Source, int[] Values);

    private List<ChoiceOverride>? ReadChoiceOverrides(NativeRuleBody mapper, NativeRuleBody chooser, Call closure, MethodInfo cursor)
    {
        var overrides = new List<ChoiceOverride>();
        foreach (var step in mapper.Calls.Where(step => step.Call.Method.DeclaringType == typeof(DynamicVar)
            && step.Call.Method.Name == "set_BaseValue").DistinctBy(step => step.Index))
        {
            if (step.Call is not { Receiver: Call { Method.DeclaringType: var set, Method.Name: "get_Item",
                    Receiver: Call { Method.DeclaringType: var model, Method.Name: "get_DynamicVars", Receiver: Call copied },
                    Arguments: [Constant { Data: string variable }] }, Arguments: [var assigned] }
                || set != typeof(DynamicVarSet) || model != typeof(CardModel)
                || step.Conditions is not [{ Expected: true, Value: Operation
                    { Operator: "isinst", Left: var instance, Right: Constant { Data: Type card } } }]
                || Key(instance) != Key(copied)
                || assigned is not Call { Method.DeclaringType: var convert, Method.Name: "op_Implicit", Arguments: [Field capture] }
                || convert != typeof(decimal)
                || CapturedValue(chooser, capture with { Receiver = closure }) is not Operation { Operator: "ldelem.i4",
                    Left: Call { Method: MethodInfo arrayGetter, Receiver: null, Arguments.Length: 0 },
                    Right: Call { Method: MethodInfo index, Receiver: Input { Name: "this" }, Arguments.Length: 0 } }
                || index != cursor || arrayGetter.DeclaringType != cursor.DeclaringType || ReadIntArray(arrayGetter) is not { } values)
                return null;
            overrides.Add(new ChoiceOverride(card, variable, arrayGetter, values));
        }
        return overrides;
    }

    private int[]? ReadIntArray(MethodInfo getter)
    {
        var body = Body(getter);
        if (body.Incomplete || body.Returns.Where(result => result.Conditions.All(IsCurrentRun)).ToArray() is not
            [{ Value: Operation { Operator: "newarr", Left: Constant { Data: int length }, Right: Constant { Data: Type element } } array } result]
            || element != typeof(int) || length is < 1 or > 16 || !result.Conditions.All(IsDreadTest))
            return null;
        if (body.Calls.Where(step => step.Conditions.All(IsCurrentRun) && step.Call.Method.DeclaringType == typeof(RuntimeHelpers)
            && step.Call.Method.Name == nameof(RuntimeHelpers.InitializeArray)).ToArray() is not
            [{ Call.Arguments: [var destination, Constant { Data: FieldInfo data }] }] || Key(destination) != Key(array))
            return null;
        var values = new int[length];
        RuntimeHelpers.InitializeArray(values, data.FieldHandle);
        return values;
    }

    private string? ChoiceOption(CardModel card, MethodInfo callback, List<ChoiceOverride> overrides, int round)
    {
        var cardType = card.GetType();
        if (callback.DeclaringType is not { } contract || !contract.IsAssignableFrom(cardType))
            return null;
        var map = cardType.GetInterfaceMap(contract);
        var slot = Array.IndexOf(map.InterfaceMethods, callback);
        if (slot < 0)
            return null;
        var body = Body(map.TargetMethods[slot]);
        if (body is not { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 }
            || body.Calls.Where(step => step.Call.Method.DeclaringType == typeof(PowerCmd))
            .DistinctBy(step => step.Index).ToArray() is not [{ Conditions.Length: 0, Call: var apply }]
            || apply.Method.Name != nameof(PowerCmd.Apply) || !apply.Method.IsGenericMethod
            || Argument(apply, "target") is not Call { Method.Name: "get_Creature",
                Receiver: Call { Method.DeclaringType: var model, Method.Name: "get_Owner", Receiver: Input { Name: "this" } } }
            || model != typeof(CardModel)
            || Argument(apply, "amount") is not { } amount
            || ChoiceAmount(amount, card, overrides, round) is not { } stacks
            || !body.Calls.All(step => IsChoiceResolutionCall(step.Call.Method, apply.Method)))
            return null;
        var power = apply.Method.GetGenericArguments()[0];
        var rule = ReadChoicePower(power);
        if (rule is null)
            return null;
        return $"{ModelLabel(cardType)} (attempt to apply {Number(stacks)} {ModelLabel(power)} to the chooser; {rule})";
    }

    private decimal? ChoiceAmount(Value amount, CardModel card, List<ChoiceOverride> overrides, int round)
    {
        if (amount is Call { Method.DeclaringType: var convert, Method.Name: "op_Implicit", Arguments: [var inner] }
            && convert == typeof(decimal))
            amount = inner;
        if (amount is not Call { Method.DeclaringType: var variable, Method.Name: "get_BaseValue" or "get_IntValue", Receiver: var receiver }
            || variable != typeof(DynamicVar) || DynamicKey(receiver) is not { } key || !card.DynamicVars.TryGetValue(key, out var original))
            return Numeric(amount);
        var replacement = overrides.FirstOrDefault(entry => entry.Card == card.GetType() && entry.Variable == key);
        if (replacement is not null)
            return round < replacement.Values.Length ? replacement.Values[round] : null;
        return original.BaseValue;
    }

    // A decoded call can still have an unknown effect. Only describe this selection
    // lifecycle when every call is part of it or is known to read data.
    private static bool IsChoiceScreenCall(MethodBase method, MethodBase screen, MethodInfo callback, MethodInfo cursor,
        MethodBase closure, IReadOnlyList<ChoiceOverride> overrides)
    {
        if (method == screen || method == callback || method == cursor || method == closure
            || overrides.Any(entry => entry.Source == method) || IsChoiceAwait(method))
            return true;
        var owner = method.DeclaringType;
        if (method is ConstructorInfo)
            return owner == typeof(BlockingPlayerChoiceContext) || GenericType(owner) == typeof(Func<,>);
        return (owner, method.Name) switch
        {
            (var type, "get_Player" or "get_IsDead") when type == typeof(Creature) => true,
            (var type, nameof(Enumerable.Select) or nameof(Enumerable.ToList)) when type == typeof(Enumerable) => true,
            (var type, "get_Item") when GenericType(type) == typeof(IReadOnlyList<>) => true,
            _ => false,
        };
    }

    private static bool IsCardCopyCall(MethodBase method)
        => IsChoiceValueCall(method) || (method.DeclaringType, method.Name) switch
        {
            (var type, "get_CombatState") when type == typeof(MonsterModel) => true,
            (var type, "get_Player") when type == typeof(Creature) => true,
            (var type, "CreateCard") when type == typeof(ICombatState) => true,
            (var type, "set_BaseValue") when type == typeof(DynamicVar) => true,
            _ => false,
        };

    private static bool IsChoiceResolutionCall(MethodBase method, MethodBase apply)
        => method == apply || IsChoiceValueCall(method) || IsChoiceAwait(method) || (method.DeclaringType, method.Name) switch
        {
            (var type, ".ctor") when type == typeof(ThrowingPlayerChoiceContext) => true,
            (var type, "get_Owner") when type == typeof(CardModel) => true,
            (var type, "get_Creature") when type == typeof(Player) => true,
            _ => false,
        };

    private static bool IsChoiceValueCall(MethodBase method)
        => (method.DeclaringType, method.Name) switch
        {
            (var type, "get_DynamicVars") when type == typeof(CardModel) => true,
            (var type, "get_Item") when type == typeof(DynamicVarSet) => true,
            (var type, "get_BaseValue" or "get_IntValue") when type == typeof(DynamicVar) => true,
            (var type, "op_Implicit") when type == typeof(decimal) => true,
            _ => false,
        };

    private static bool IsChoiceAwait(MethodBase method)
        => (GenericType(method.DeclaringType), method.Name) switch
        {
            (var type, "GetAwaiter") when type == typeof(Task) || type == typeof(Task<>) => true,
            (var type, "GetResult" or "get_IsCompleted") when type == typeof(TaskAwaiter) || type == typeof(TaskAwaiter<>) => true,
            (var type, "SetResult") when type == typeof(AsyncTaskMethodBuilder) => true,
            _ => false,
        };

    private static Type? GenericType(Type? type) => type?.IsGenericType == true ? type.GetGenericTypeDefinition() : type;
}
