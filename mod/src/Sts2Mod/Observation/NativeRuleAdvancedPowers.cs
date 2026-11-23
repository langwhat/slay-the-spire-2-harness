using System.Reflection;
using System.Runtime.CompilerServices;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Commands.Builders;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

internal sealed partial class NativeRuleText
{
    private readonly HashSet<Type> _generatedCards = [];
    private readonly Dictionary<Type, List<PropertyInfo>> _summonTrueFlags = [];
    private readonly Dictionary<Type, HashSet<string>> _mutableDynamicKeys = [];
    private readonly Dictionary<Type, HashSet<FieldInfo>> _gameplayFields = [];

    internal void ConfigureSummon(MonsterModel copy)
    {
        if (!_summonTrueFlags.TryGetValue(copy.GetType(), out var properties))
            return;
        foreach (var property in properties)
            property.SetValue(copy, true);
    }

    private HashSet<string> MutableDynamicKeys(Type power)
    {
        if (_mutableDynamicKeys.TryGetValue(power, out var cached))
            return cached;
        var keys = new HashSet<string>(StringComparer.Ordinal);
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
        foreach (var method in power.GetMethods(flags))
        {
            if (IsPurePresentationMember(method) || method.IsSpecialName && method.Name.StartsWith("get_", StringComparison.Ordinal))
                continue;
            foreach (var step in Body(method).Calls)
            {
                if (!typeof(DynamicVar).IsAssignableFrom(step.Call.Method.DeclaringType))
                    continue;
                if (step.Call.Method.Name is not ("set_BaseValue" or "UpgradeValueBy" or "Add" or "Multiply"))
                    continue;
                if (DynamicVarKeyFromReceiver(power, step.Call.Receiver) is { } key)
                    keys.Add(key);
            }
        }
        _mutableDynamicKeys[power] = keys;
        return keys;
    }

    private string? DynamicVarKeyFromReceiver(Type owner, Value? receiver)
    {
        if (DynamicKey(receiver) is { } direct)
            return direct;
        if (receiver is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            && getter.Name.StartsWith("get_", StringComparison.Ordinal))
        {
            if (getter.Name == "get_Strength")
                return "StrengthPower";
            if (Body(getter) is { Incomplete: false, Returns: [{ Value: var ret }] } && DynamicKey(ret) is { } inner)
                return inner;
            return getter.Name[4..];
        }
        return null;
    }

    // Check whether a field on a monster, power, or power InternalData is ever read by move graphs,
    // conditions, or gameplay commands. Fields read only by animation, audio, or fade hooks are presentation-only.
    private bool IsGameplayField(FieldInfo field)
    {
        if (field.DeclaringType is not { } declaring)
            return true;
        var owner = declaring.IsNested && declaring.DeclaringType is { } outer && typeof(AbstractModel).IsAssignableFrom(outer)
            ? outer
            : declaring;
        if (!typeof(AbstractModel).IsAssignableFrom(owner))
            return true;
        if (!_gameplayFields.TryGetValue(owner, out var active))
        {
            active = CollectGameplayFields(owner);
            _gameplayFields[owner] = active;
        }
        return active.Contains(field);
    }

    private HashSet<FieldInfo> CollectGameplayFields(Type owner)
    {
        var result = new HashSet<FieldInfo>();
        var deferredWrites = new List<(FieldInfo Target, Value Assigned, Test[] Conditions)>();
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

        // Engine-override properties can be read outside the declaring model by combat dispatchers.
        foreach (var prop in owner.GetProperties(flags))
        {
            if (prop.GetMethod is not { } getter || IsPurePresentationMember(getter))
                continue;
            if (IsHookOverride(getter))
            {
                if (BackingField(owner, getter) is { } backing)
                    result.Add(backing);
                else
                    foreach (var ret in Body(getter).Returns)
                        CollectFieldsFromValue(ret.Value, owner, result);
            }
        }

        var inspectedTypes = new List<Type> { owner };
        foreach (var nested in owner.GetNestedTypes(BindingFlags.Public | BindingFlags.NonPublic))
        {
            if (nested.IsDefined(typeof(CompilerGeneratedAttribute), inherit: false)
                && !typeof(IAsyncStateMachine).IsAssignableFrom(nested))
                inspectedTypes.Add(nested);
        }

        foreach (var inspected in inspectedTypes)
        {
            foreach (var method in inspected.GetMethods(flags))
            {
                if (IsPurePresentationMember(method))
                    continue;
                if (inspected == owner && method.IsSpecialName
                    && (method.Name.StartsWith("get_", StringComparison.Ordinal) || method.Name.StartsWith("set_", StringComparison.Ordinal)))
                    continue;
                if (method.ContainsGenericParameters)
                    continue;
                var body = Body(method);
                if (body.Incomplete)
                {
                    foreach (var fallbackField in AllOwnedFields(owner))
                        result.Add(fallbackField);
                    return result;
                }
                foreach (var ret in body.Returns)
                {
                    CollectFieldsFromValue(ret.Value, owner, result);
                    foreach (var cond in ret.Conditions)
                        if (!IsPresentationTest(cond.Value))
                            CollectFieldsFromValue(cond.Value, owner, result);
                }
                foreach (var loop in body.Loops)
                    CollectFieldsFromValue(loop.Count, owner, result);
                foreach (var fstep in body.FieldSteps)
                {
                    if (OwnedField(fstep.Assignment.Target, owner) is { } ownedTarget)
                    {
                        deferredWrites.Add((ownedTarget, fstep.Assignment.Assigned, fstep.Conditions));
                        continue;
                    }
                    CollectFieldsFromValue(fstep.Assignment.Assigned, owner, result);
                    foreach (var cond in fstep.Conditions)
                        if (!IsPresentationTest(cond.Value))
                            CollectFieldsFromValue(cond.Value, owner, result);
                }
                foreach (var step in body.Calls)
                {
                    if (IsPresentationCall(step.Call.Method))
                        continue;
                    if (step.Call.Receiver == Self && step.Call.Method is MethodInfo accessor
                        && accessor.IsSpecialName && accessor.Name.StartsWith("get_", StringComparison.Ordinal))
                        continue;
                    if (step.Call.Receiver == Self && step.Call.Method is MethodInfo setter
                        && setter.Name.StartsWith("set_", StringComparison.Ordinal)
                        && BackingField(owner, setter) is { } mutated && step.Call.Arguments is [var rhs])
                    {
                        deferredWrites.Add((mutated, rhs, step.Conditions));
                        continue;
                    }
                    if (step.Call.Receiver == Self && step.Call.Method is MethodInfo presentationHelper
                        && presentationHelper.ReturnType == typeof(void) && presentationHelper.DeclaringType == owner
                        && !presentationHelper.ContainsGenericParameters
                        && Body(presentationHelper) is { Incomplete: false, Writes.Count: 0, FieldSteps.Count: 0 } phBody
                        && phBody.Calls.All(c => IsBenignReadOrPlumbingCall(c.Call.Method)))
                        continue;
                    CollectFieldsFromCall(step.Call, owner, result);
                    foreach (var cond in step.Conditions)
                        if (!IsPresentationTest(cond.Value))
                            CollectFieldsFromValue(cond.Value, owner, result);
                }
            }
        }

        bool added;
        do
        {
            added = false;
            foreach (var (mutated, rhs, conditions) in deferredWrites)
            {
                if (!result.Contains(mutated))
                    continue;
                var before = result.Count;
                CollectFieldsFromValue(rhs, owner, result);
                foreach (var cond in conditions)
                    if (!IsPresentationTest(cond.Value))
                        CollectFieldsFromValue(cond.Value, owner, result);
                if (result.Count > before)
                    added = true;
            }
        } while (added);

        return result;
    }

    private static IEnumerable<FieldInfo> AllOwnedFields(Type owner)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;
        foreach (var field in owner.GetFields(flags))
            yield return field;
        foreach (var nested in owner.GetNestedTypes(BindingFlags.Public | BindingFlags.NonPublic))
        {
            if (!nested.IsDefined(typeof(CompilerGeneratedAttribute), inherit: false))
                foreach (var field in nested.GetFields(flags))
                    yield return field;
        }
    }

    private static FieldInfo? OwnedField(Field field, Type owner)
    {
        if (field.Receiver is Input { Name: "this" } && field.Member.DeclaringType == owner)
            return field.Member;
        if (InternalDataField(field) is { } internalMember && internalMember.DeclaringType?.DeclaringType == owner)
            return internalMember;
        return null;
    }

    private FieldInfo? BackingField(Type owner, MethodInfo accessor)
    {
        var propName = accessor.Name[4..];
        var prop = owner.GetProperty(propName, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        if (prop?.GetMethod is not { } getter)
            return null;
        return Body(getter) is { Incomplete: false, Returns: [{ Value: Field { Receiver: Input { Name: "this" }, Member: var field }, Conditions.Length: 0 }] }
            ? field
            : null;
    }

    private void CollectFieldsFromCall(Call call, Type owner, HashSet<FieldInfo> result, int depth = 0)
    {
        if (depth > 6)
            return;
        if (call.Receiver is not null)
            CollectFieldsFromValue(call.Receiver, owner, result, depth + 1);
        foreach (var arg in call.Arguments)
            CollectFieldsFromValue(arg, owner, result, depth + 1);
    }

    private void CollectFieldsFromValue(Value value, Type owner, HashSet<FieldInfo> result, int depth = 0)
    {
        if (depth > 6)
            return;
        switch (value)
        {
            case Field field when OwnedField(field, owner) is { } member:
                result.Add(member);
                break;
            case Field { Receiver: { } rec }:
                CollectFieldsFromValue(rec, owner, result, depth + 1);
                break;
            case Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                when getter.DeclaringType == owner && getter.Name.StartsWith("get_", StringComparison.Ordinal)
                    && BackingField(owner, getter) is { } backing:
                result.Add(backing);
                break;
            case Call { Method: MethodInfo composite, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                when composite.DeclaringType == owner && composite.Name.StartsWith("get_", StringComparison.Ordinal):
                foreach (var ret in Body(composite).Returns)
                    CollectFieldsFromValue(ret.Value, owner, result, depth + 1);
                break;
            case Call nested:
                CollectFieldsFromCall(nested, owner, result, depth + 1);
                break;
            case Operation op:
                CollectFieldsFromValue(op.Left, owner, result, depth + 1);
                if (op.Right is not null)
                    CollectFieldsFromValue(op.Right, owner, result, depth + 1);
                break;
            case Constant { Data: MethodInfo lambda }
                when (lambda.DeclaringType == owner || lambda.DeclaringType?.DeclaringType == owner) && !lambda.ContainsGenericParameters:
                var body = Body(lambda);
                foreach (var ret in body.Returns)
                    CollectFieldsFromValue(ret.Value, owner, result, depth + 1);
                foreach (var fstep in body.FieldSteps)
                {
                    if (OwnedField(fstep.Assignment.Target, owner) is { } ownedTarget)
                        result.Add(ownedTarget);
                    CollectFieldsFromValue(fstep.Assignment.Assigned, owner, result, depth + 1);
                }
                foreach (var step in body.Calls)
                    if (!IsPresentationCall(step.Call.Method))
                        CollectFieldsFromCall(step.Call, owner, result, depth + 1);
                break;
        }
    }

    private static bool IsPurePresentationMember(MethodInfo method)
        => method.Name is "GenerateAnimator" or "GenerateBestiaryMoveList" or "ShouldShowMoveInBestiary"
            or "SetupSkins" or "BeforeAnimPlayedInCompendium" or "BeforeRemovedFromRoom"
            or "get_ShouldFadeAfterDeath" or "get_DeathSfx" or "get_HurtSfx" or "get_CastSfx"
            or "get_Title" or "get_AssetPaths" or "get_Background";

    private static bool IsPresentationTest(Value value)
        => value is Call { Method: var method } && IsPresentationCall(method);

    internal static bool IsPresentationCall(MethodBase method)
    {
        var type = method.DeclaringType;
        var ns = type?.Namespace ?? "";
        if (ns.StartsWith("Godot", StringComparison.Ordinal)
            || ns.StartsWith("MegaCrit.Sts2.Core.Nodes", StringComparison.Ordinal)
            || ns.StartsWith("MegaCrit.Sts2.Core.Audio", StringComparison.Ordinal)
            || ns.StartsWith("MegaCrit.Sts2.Core.Animation", StringComparison.Ordinal)
            || ns.StartsWith("MegaCrit.Sts2.Core.Bindings.MegaSpine", StringComparison.Ordinal)
            || ns.StartsWith("MegaCrit.Sts2.Core.Assets", StringComparison.Ordinal)
            || ns.StartsWith("MegaCrit.Sts2.Core.Saves", StringComparison.Ordinal))
            return true;
        return (type?.Name, method.Name) switch
        {
            ("SfxCmd" or "VfxCmd" or "TalkCmd" or "ThinkCmd" or "Cmd" or "LocalContext" or "NodeHelper" or "GodotTreeExtensions", _) => true,
            ("CreatureCmd", "TriggerAnim" or "ChangeCreatureAnim") => true,
            ("PowerModel", "Flash" or "InvokeDisplayAmountChanged") => true,
            ("MonsterModel", "AssertMutable" or "L10NMonsterLookup") => true,
            ("Creature", "GetCreatureNode" or "GetVfxContainer" or "GetBackVfxContainer" or "SetNodeVisible") => true,
            ("ExtraRunFields", _) => true,
            ("Mathf", _) => true,
            _ => false,
        };
    }

    private static bool IsBenignReadOrPlumbingCall(MethodBase method)
    {
        if (IsPresentationCall(method))
            return true;
        var type = method.DeclaringType;
        var ns = type?.Namespace ?? "";
        if (ns is "System.Runtime.CompilerServices" or "System.Threading.Tasks"
            || type == typeof(IDisposable) || type == typeof(Math) || type == typeof(string) || type == typeof(object))
            return true;
        if (method.Name is "GetEnumerator" or "MoveNext" or "get_Current" or "get_IsOff" or "get_IsOn" or "GetInternalData")
            return true;
        if (method is MethodInfo info && info.IsSpecialName && info.Name.StartsWith("get_", StringComparison.Ordinal)
            && info.GetParameters().Length == 0)
            return true;
        if (type == typeof(Enumerable) || ns.StartsWith("System.Collections", StringComparison.Ordinal)
            && method.Name is "get_Count" or "get_Item" or "Contains" or "ContainsKey" or "TryGetValue")
            return true;
        return false;
    }

    private bool IsPresentationEventSubscription(Call call, Type? callerType)
    {
        if (!call.Method.IsSpecialName || call.Method.DeclaringType != typeof(Creature)
            || !call.Method.Name.StartsWith("add_", StringComparison.Ordinal)
            || call.Arguments is not [Call { Method: ConstructorInfo, Arguments: [_, Constant { Data: MethodInfo handler }] }])
            return false;
        if (IsPresentationCall(handler))
            return true;
        if (handler.DeclaringType != callerType && handler.DeclaringType?.DeclaringType != callerType)
            return false;
        var body = Body(handler);
        if (body.Incomplete || body.Writes.Count > 0)
            return false;
        if (body.FieldSteps.Any(step => IsGameplayField(step.Assignment.Target.Member)))
            return false;
        foreach (var step in body.Calls)
        {
            var m = step.Call.Method;
            if (m.IsSpecialName && m.DeclaringType == typeof(Creature) && m.Name.StartsWith("remove_", StringComparison.Ordinal))
                continue;
            if (IsBenignReadOrPlumbingCall(m))
                continue;
            if (step.Call.Receiver == Self && m is MethodInfo setter && setter.Name.StartsWith("set_", StringComparison.Ordinal)
                && setter.DeclaringType is { } owner && BackingField(owner, setter) is { } field && !IsGameplayField(field))
                continue;
            if (m is MethodInfo inner && step.Call.Receiver == Self && inner.ReturnType == typeof(void)
                && IsPresentationVoidHelper(inner))
                continue;
            return false;
        }
        return true;
    }

    private bool IsSkippedPresentationHelper(Call call, MethodInfo caller)
    {
        if (call.Method.Name == "RunSafely" && call.Method.DeclaringType?.Name == "TaskHelper"
            && call.Arguments is [Call { Method: MethodInfo bg }] && !bg.IsGenericMethod && IsPresentationTaskHelper(bg))
            return true;
        if (call.Method is MethodInfo taskHelper && taskHelper.ReturnType == typeof(Task)
            && !taskHelper.IsSpecialName && !taskHelper.IsGenericMethod
            && taskHelper.DeclaringType == caller.DeclaringType && IsPresentationTaskHelper(taskHelper))
            return true;
        if (call.Method is MethodInfo voidHelper && voidHelper.ReturnType == typeof(void)
            && !voidHelper.IsSpecialName && voidHelper.DeclaringType != typeof(MonsterModel) && voidHelper.DeclaringType != typeof(PowerModel)
            && (call.Receiver == Self || IsSameMonsterTypeLookup(call.Receiver, voidHelper.DeclaringType))
            && IsPresentationVoidHelper(voidHelper))
            return true;
        return false;
    }

    private bool IsPresentationVoidHelper(MethodInfo helper, int depth = 0)
    {
        if (depth > 2)
            return false;
        var body = Body(helper);
        if (body.Incomplete || body.Writes.Count > 0)
            return false;
        if (body.FieldSteps.Any(step => IsGameplayField(step.Assignment.Target.Member)))
            return false;
        foreach (var step in body.Calls)
        {
            var m = step.Call.Method;
            if (IsBenignReadOrPlumbingCall(m))
                continue;
            if (step.Call.Receiver == Self && m is MethodInfo setter && setter.Name.StartsWith("set_", StringComparison.Ordinal)
                && setter.DeclaringType is { } owner && BackingField(owner, setter) is { } field && !IsGameplayField(field))
                continue;
            if (m is MethodInfo nested && nested != helper && nested.ReturnType == typeof(void)
                && nested.DeclaringType == helper.DeclaringType && IsPresentationVoidHelper(nested, depth + 1))
                continue;
            if (m.Name == "RunSafely" && m.DeclaringType?.Name == "TaskHelper"
                && step.Call.Arguments is [Call { Method: MethodInfo bg }] && !bg.IsGenericMethod && IsPresentationTaskHelper(bg))
                continue;
            return false;
        }
        return true;
    }

    private bool IsPresentationTaskHelper(MethodInfo helper)
    {
        var body = Body(helper);
        if (body.Incomplete || body.Writes.Count > 0 || body.FieldSteps.Count > 0)
            return false;
        return body.Calls.All(step => IsBenignReadOrPlumbingCall(step.Call.Method));
    }

    private void ReadSpecialPowerHooks(Type type, string powerPrefix, HashSet<string> handled, List<string> lines)
    {
        if (ReadVigorHooks(type, powerPrefix, out var vigorLine))
        {
            handled.Add("BeforeAttack");
            handled.Add("ModifyDamageAdditive");
            handled.Add("AfterAttack");
            lines.Add(vigorLine);
        }
        if (ReadDampenHooks(type, powerPrefix, out var dampenLines))
        {
            handled.Add("AfterApplied");
            handled.Add("AfterDeath");
            handled.Add("AfterRemoved");
            lines.AddRange(dampenLines);
        }
        if (ReadPainfulStabsHook(type, powerPrefix, out var stabsLine))
        {
            handled.Add("AfterAttack");
            lines.Add(stabsLine);
        }
        if (ReadSurroundedHooks(type, powerPrefix, out var surroundedLines))
        {
            handled.Add("ModifyDamageMultiplicative");
            handled.Add("BeforeCardPlayed");
            handled.Add("BeforePotionUsed");
            handled.Add("AfterDeath");
            lines.AddRange(surroundedLines);
        }
    }

    private bool ReadVigorHooks(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "BeforeAttack") is not { } before
            || FindOverride(power, "ModifyDamageAdditive") is not { } add
            || FindOverride(power, "AfterAttack") is not { } after
            || Body(before) is not { Incomplete: false } beforeBody
            || Body(add) is not { Incomplete: false } addBody
            || Body(after) is not { Incomplete: false } afterBody)
            return false;
        var recordsCommand = beforeBody.FieldSteps.Any(step => InternalDataField(step.Assignment.Target)?.FieldType == typeof(AttackCommand)
            && step.Assignment.Assigned is Input { Name: "command" });
        var recordsAmount = beforeBody.FieldSteps.Any(step => InternalDataField(step.Assignment.Target)?.FieldType == typeof(int)
            && IsStackAmount(step.Assignment.Assigned));
        var addsStacks = addBody.Returns.Any(ret => IsStackAmount(ret.Value));
        var subtractsRecorded = afterBody.Calls.Any(step => step.Call.Method.DeclaringType == typeof(PowerCmd)
            && step.Call.Method.Name == nameof(PowerCmd.ModifyAmount)
            && step.Call.Arguments.Length >= 3
            && step.Call.Arguments[1] is Input { Name: "this" }
            && step.Call.Arguments[2] is Call { Method.Name: "op_Implicit", Arguments: [Operation { Operator: "neg", Left: Field recorded }] }
            && InternalDataField(recorded)?.FieldType == typeof(int));
        if (!recordsCommand || !recordsAmount || !addsStacks || !subtractsRecorded)
            return false;
        line = $"{prefix}Next powered attack bonus: adds +this power's current stacks damage per hit (before multipliers and block) to holder's first powered attack, then subtracts the stacks present when that attack started after the entire attack resolves";
        return true;
    }

    private bool ReadDampenHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "AfterApplied") is not { } applied
            || FindOverride(power, "AfterDeath") is not { } death
            || FindOverride(power, "AfterRemoved") is not { } removed
            || Body(applied) is not { Incomplete: false } appliedBody
            || Body(death) is not { Incomplete: false } deathBody
            || Body(removed) is not { Incomplete: false } removedBody)
            return false;
        var downgrades = appliedBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardCmd) && s.Call.Method.Name == nameof(CardCmd.Downgrade));
        var removesCaster = deathBody.Calls.Any(s => s.Call.Method.Name == "Remove" && s.Call.Receiver is Field f && InternalDataField(f) is not null)
            && deathBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(PowerCmd) && s.Call.Method.Name == nameof(PowerCmd.Remove));
        var restoresUpgrades = removedBody.Calls.Any(s => s.Call.Method.DeclaringType == typeof(CardCmd) && s.Call.Method.Name == nameof(CardCmd.Upgrade));
        if (!downgrades || !removesCaster || !restoresUpgrades)
            return false;
        lines.Add($"{prefix}After this power is applied: record the upgrade level of each upgraded combat card and downgrade it");
        lines.Add($"{prefix}On death event: if death was not prevented and event target is a registered caster of this power: unregister event target; if no registered casters remain: remove {PowerLabel(power)}");
        lines.Add($"{prefix}After this power is removed: restore the recorded upgrade levels on those downgraded combat cards");
        return true;
    }

    private bool ReadPainfulStabsHook(Type power, string prefix, out string line)
    {
        line = "";
        if (FindOverride(power, "AfterAttack") is not { } after || Body(after) is not { Incomplete: false } body)
            return false;
        var adds = body.Calls.Where(s => s.Call.Method.DeclaringType == typeof(CardPileCmd)
            && s.Call.Method.Name == nameof(CardPileCmd.AddToCombatAndPreview) && s.Call.Method.IsGenericMethod)
            .DistinctBy(s => s.Index).ToArray();
        if (adds is not [var step] || Numeric(Argument(step.Call, "pileType")) is not { } pileNum
            || Argument(step.Call, "count") is not Operation { Operator: "mul", Left: var mulL, Right: var mulR }
            || (!IsStackAmount(mulL) && !IsStackAmount(mulR)))
            return false;
        var checksAttacker = step.Conditions.Any(t => IsEquality(t, true, out var l, out var r)
            && l is Call { Method.Name: "get_Attacker", Receiver: Input { Name: "command" } } && IsOwner(r));
        var checksPowered = step.Conditions.Any(t => t is { Expected: true, Value: Call { Method.Name: "IsPoweredAttack" } });
        if (!checksAttacker || !checksPowered)
            return false;
        var pile = (int)pileNum;
        var position = Numeric(Argument(step.Call, "position")) is { } place ? (CardPilePosition)(int)place : CardPilePosition.Bottom;
        var where = (PileType)pile == PileType.Draw && position != CardPilePosition.Bottom ? $" ({position.ToString().ToLowerInvariant()})" : "";
        var cardType = step.Call.Method.GetGenericArguments()[0];
        _generatedCards.Add(cardType);
        line = $"{prefix}After an attack resolves: if attacker=holder and target side≠holder's side and attack has Move without Unpowered: for each player hit for nonzero unblocked damage, add (this power's current stacks × unblocked hits on that player) {ModelLabel(cardType)} to that player's {((PileType)pile).ToString().ToLowerInvariant()} pile{where}";
        return true;
    }

    private bool ReadSurroundedHooks(Type power, string prefix, out List<string> lines)
    {
        lines = [];
        if (FindOverride(power, "ModifyDamageMultiplicative") is not { } mult
            || FindOverride(power, "BeforeCardPlayed") is not { } beforeCard
            || FindOverride(power, "BeforePotionUsed") is not { } beforePotion
            || FindOverride(power, "AfterDeath") is not { } afterDeath
            || Body(mult) is not { Incomplete: false } multBody
            || Body(beforeCard) is not { Incomplete: false, Writes.Count: 0 } beforeCardBody
            || Body(beforePotion) is not { Incomplete: false, Writes.Count: 0 } beforePotionBody
            || Body(afterDeath) is not { Incomplete: false, Writes.Count: 0 } afterDeathBody
            || ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(power)) is not { } canonicalPower)
            return false;

        var facingField = AllOwnedFields(power).FirstOrDefault(f => f.FieldType.IsEnum);
        if (facingField is null)
            return false;
        var initialFacing = ResolveInitialEnumValue(canonicalPower, facingField);
        if (initialFacing is null)
            return false;
        if (!HookUpdatesFacingField(beforeCardBody, power, facingField)
            || !HookUpdatesFacingField(beforePotionBody, power, facingField)
            || !HookUpdatesFacingField(afterDeathBody, power, facingField))
            return false;

        var boostedReturns = multBody.Returns.Where(r => Numeric(r.Value) is { } v && v != 1m).ToArray();
        var factors = boostedReturns.Select(r => Numeric(r.Value)!.Value).Distinct().ToArray();
        if (factors is not [var factor])
            return false;

        var pairs = new List<(string FacingName, Type BackPower)>();
        foreach (var ret in boostedReturns)
        {
            var matchedPower = ret.Conditions
                .Select(t => t is { Expected: true, Value: Call { Method: { Name: "HasPower", IsGenericMethod: true } m } } ? m.GetGenericArguments()[0] : null)
                .OfType<Type>().FirstOrDefault();
            var matchedFacing = ExtractFacingFromConditions(ret.Conditions, power, facingField);
            if (matchedPower is not null && matchedFacing is not null && !pairs.Any(p => p.FacingName == matchedFacing && p.BackPower == matchedPower))
                pairs.Add((matchedFacing, matchedPower));
        }
        if (pairs.Count != 2)
            return false;

        var clauses = pairs.Select(p => $"(Facing={p.FacingName} and damage dealer has {PowerLabel(p.BackPower)})");
        var updates = pairs.Select(p =>
        {
            var opposite = pairs.First(other => other.BackPower != p.BackPower).FacingName;
            return $"{PowerLabel(p.BackPower)} sets Facing={opposite}";
        }).ToArray();
        var deathUpdates = pairs.Select(p =>
        {
            var opposite = pairs.First(other => other.BackPower != p.BackPower).FacingName;
            return $"if all remaining hittable enemies have {PowerLabel(p.BackPower)} set Facing={opposite}";
        });

        lines.Add($"{prefix}Damage multiplier per hit, after additions: if target=holder and ({string.Join(" or ", clauses)}): ×{Number(factor)} (initial Facing={initialFacing})");
        lines.Add($"{prefix}Facing updates: before holder plays a targeted card or uses a targeted combat potion, face toward that target ({string.Join("; ", updates)}); on enemy death, {string.Join(", and ", deathUpdates)}");
        return true;
    }

    private static string? ResolveInitialEnumValue(PowerModel canonicalPower, FieldInfo enumField)
    {
        if (enumField.DeclaringType == canonicalPower.GetType())
            return enumField.GetValue(canonicalPower)?.ToString();
        if (enumField.DeclaringType?.IsNested == true && Activator.CreateInstance(enumField.DeclaringType, nonPublic: true) is { } data)
            return enumField.GetValue(data)?.ToString();
        return null;
    }

    private bool HookUpdatesFacingField(NativeRuleBody body, Type power, FieldInfo facingField, int depth = 0)
    {
        if (depth > 2)
            return false;
        if (body.FieldSteps.Any(s => OwnedField(s.Assignment.Target, power) == facingField))
            return true;
        foreach (var step in body.Calls)
        {
            if (step.Call.Method is not MethodInfo m || m.DeclaringType != power)
                continue;
            if (m.Name.StartsWith("set_", StringComparison.Ordinal) && BackingField(power, m) == facingField)
                return true;
            if (!m.IsSpecialName && !m.ContainsGenericParameters && Body(m) is { Incomplete: false } helperBody
                && HookUpdatesFacingField(helperBody, power, facingField, depth + 1))
                return true;
        }
        return false;
    }

    private string? ExtractFacingFromConditions(Test[] conditions, Type power, FieldInfo facingField)
    {
        foreach (var test in conditions)
        {
            if (IsEquality(test, true, out var left, out var right))
            {
                if (ReadsFacingField(left, power, facingField) && Numeric(right) is { } rn)
                    return Enum.GetName(facingField.FieldType, (int)rn);
                if (ReadsFacingField(right, power, facingField) && Numeric(left) is { } ln)
                    return Enum.GetName(facingField.FieldType, (int)ln);
            }
        }
        foreach (var test in conditions)
        {
            // Enum == 0 is also compiled as brfalse on the field/getter load directly.
            if (!test.Expected && ReadsFacingField(test.Value, power, facingField))
                return Enum.GetName(facingField.FieldType, 0);
            if (test.Expected && ReadsFacingField(test.Value, power, facingField)
                && Enum.GetValues(facingField.FieldType).Length == 2)
                return Enum.GetName(facingField.FieldType, 1);
        }
        return null;
    }

    private bool ReadsFacingField(Value? value, Type power, FieldInfo facingField)
        => value is Field f && OwnedField(f, power) == facingField
            || value is Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
                && getter.DeclaringType == power && getter.Name.StartsWith("get_", StringComparison.Ordinal)
                && BackingField(power, getter) == facingField;
}
