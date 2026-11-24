using System.Globalization;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.TestSupport;

namespace Sts2Mod.Observation;

// Read the ordinary continuation of a method without invoking game code. Recorded paths
// carry the branch tests that govern each call, including tests before an await.
internal sealed class NativeRuleBody
{
    internal abstract record Value;
    internal sealed record Constant(object? Data) : Value;
    internal sealed record Input(string Name, Type? Type = null) : Value;
    internal sealed record LocalAddress(int Slot) : Value;
    internal sealed record Field(FieldInfo Member, Value? Receiver) : Value;
    internal sealed record Call(MethodBase Method, Value? Receiver, Value[] Arguments) : Value;
    internal sealed record Operation(string Operator, Value Left, Value? Right = null) : Value;
    internal sealed record Assignment(Field Target, Value Assigned) : Value;
    internal sealed record ArrayInit(Type? ElementType, Value Length, int Id) : Value;
    internal sealed record ArrayValue(Type? ElementType, Value Length, Value[] Elements) : Value;
    internal sealed record Test(Value Value, bool Expected, int Offset = -1);
    internal sealed record Step(int Index, Call Call, Test[] Conditions, int Epoch = 0, int Order = 0);
    internal sealed record FieldStep(int Index, Assignment Assignment, Test[] Conditions, int Epoch = 0, int Order = 0);
    internal sealed record OutStore(Input Parameter, Value Assigned, Test[] Conditions);
    internal sealed record Result(Value Value, Test[] Conditions);
    // A completed path with the instructions it executed. Paths that miss an instruction
    // bound the conditions under which that instruction runs.
    internal sealed record Terminal(Test[] Conditions, IReadOnlySet<(int Epoch, int Index)> Visited, bool Returned);
    // A counted loop runs the instructions from Start up to its back branch at End Count times.
    internal sealed record Loop(int Start, int End, Value Count);

    private sealed class Path
    {
        public int Index;
        public int StepOrder;
        public List<Value> Stack { get; } = [];
        public Dictionary<int, Value> Locals { get; } = [];
        public Dictionary<FieldInfo, Value> StateFields { get; } = [];
        public Dictionary<int, int> Visits { get; } = [];
        public HashSet<(int Epoch, int Index)> CumulativeVisited { get; } = [];
        public Dictionary<(int Start, int End), int> ActiveUnrolledLoops { get; } = [];
        public Dictionary<int, Dictionary<int, Value>> ArrayElements { get; } = [];
        public HashSet<int> InvalidatedArrays { get; } = [];
        public int NextArrayId;
        public List<Test> Conditions { get; } = [];

        public int EpochAt(int instructionIndex)
        {
            var epoch = 0;
            foreach (var ((start, end), iteration) in ActiveUnrolledLoops)
                if (instructionIndex >= start && instructionIndex <= end)
                    epoch = epoch * 31 + iteration + 1;
            return epoch;
        }

        public Path Fork(int index)
        {
            var copy = new Path { Index = index, StepOrder = StepOrder, NextArrayId = NextArrayId };
            copy.Stack.AddRange(Stack);
            copy.Conditions.AddRange(Conditions);
            foreach (var (key, value) in Locals)
                copy.Locals.Add(key, value);
            foreach (var (key, value) in StateFields)
                copy.StateFields.Add(key, value);
            foreach (var (key, value) in Visits)
                copy.Visits.Add(key, value);
            foreach (var item in CumulativeVisited)
                copy.CumulativeVisited.Add(item);
            foreach (var (key, value) in ActiveUnrolledLoops)
                copy.ActiveUnrolledLoops.Add(key, value);
            foreach (var (arrayId, elems) in ArrayElements)
                copy.ArrayElements.Add(arrayId, new Dictionary<int, Value>(elems));
            foreach (var invalidated in InvalidatedArrays)
                copy.InvalidatedArrays.Add(invalidated);
            return copy;
        }

        public Value Materialize(Value value)
            => Materialize(value, [], 0);

        private Value Materialize(Value value, HashSet<int> visiting, int depth) => value switch
        {
            ArrayInit init => MaterializeArray(init, visiting, depth),
            LocalAddress addr => depth >= 4 ? Missing : Materialize(Locals.GetValueOrDefault(addr.Slot, Missing), visiting, depth + 1),
            _ => value,
        };

        private Value MaterializeArray(ArrayInit init, HashSet<int> visiting, int depth)
        {
            if (depth >= 4
                || InvalidatedArrays.Contains(init.Id)
                || !visiting.Add(init.Id))
            {
                return new Operation("newarr", init.Length, new Constant(init.ElementType));
            }
            try
            {
                if (init.Length is not Constant { Data: int len } || len < 0 || len > 16
                    || !ArrayElements.TryGetValue(init.Id, out var elems)
                    || elems.Count == 0)
                {
                    return new Operation("newarr", init.Length, new Constant(init.ElementType));
                }
                var values = new Value[len];
                for (var i = 0; i < len; i++)
                    values[i] = elems.TryGetValue(i, out var item) ? Materialize(item, visiting, depth + 1) : Missing;
                return new ArrayValue(init.ElementType, init.Length, values);
            }
            finally
            {
                visiting.Remove(init.Id);
            }
        }

        public Value Pop()
        {
            if (Stack.Count == 0)
                return Missing;
            var value = Stack[^1];
            Stack.RemoveAt(Stack.Count - 1);
            return value;
        }

        public Value Deref(Value value)
            => value is LocalAddress addr ? Locals.GetValueOrDefault(addr.Slot, Missing) : value;
    }

    private static readonly Input Missing = new("?");
    private static readonly Input StateMachine = new("async_state");
    internal static readonly Input Self = new("this");

    public List<Step> Calls { get; } = [];
    public List<FieldStep> FieldSteps { get; } = [];
    public List<OutStore> OutStores { get; } = [];
    public List<Result> Returns { get; } = [];
    public List<Terminal> Terminals { get; } = [];
    public HashSet<FieldInfo> Writes { get; } = [];
    public HashSet<Loop> Loops { get; } = [];
    public bool Incomplete { get; private set; }
    public bool ReadsParameters { get; private set; }

    public static NativeRuleBody Read(MethodInfo method)
    {
        var body = new NativeRuleBody();
        body.ReadMethod(method);
        return body;
    }

    private void ReadMethod(MethodInfo original)
    {
        if (original.ContainsGenericParameters || original.IsGenericMethodDefinition)
        {
            Incomplete = true;
            return;
        }
        var stateMachine = original.GetCustomAttribute<AsyncStateMachineAttribute>()?.StateMachineType;
        var method = stateMachine is null ? original : AccessTools.Method(stateMachine, "MoveNext");
        if (method?.GetMethodBody() is null)
        {
            Incomplete = true;
            return;
        }

        var instructions = PatchProcessor.GetOriginalInstructions(method);
        var labels = new Dictionary<Label, int>();
        for (var index = 0; index < instructions.Count; index++)
        {
            foreach (var label in instructions[index].labels)
                labels[label] = index;
        }

        var arguments = original.GetParameters().Select(p => new Input(p.Name ?? "?", p.ParameterType)).ToArray();
        var isMonsterMove = typeof(MonsterModel).IsAssignableFrom(original.DeclaringType)
            && original.GetParameters() is [{ Name: "targets", ParameterType: var moveTargetsType }]
            && moveTargetsType == typeof(IReadOnlyList<MegaCrit.Sts2.Core.Entities.Creatures.Creature>);
        var pending = new Stack<Path>();
        pending.Push(new Path());
        var remainingInstructions = 24000;
        while (pending.TryPop(out var path))
        {
            while (path.Index < instructions.Count)
            {
                if (--remainingInstructions == 0)
                {
                    Incomplete = true;
                    return;
                }

                var index = path.Index++;
                var epoch = path.EpochAt(index);
                var instruction = instructions[index];
                var code = instruction.opcode;
                var operand = instruction.operand;
                var visits = path.Visits.GetValueOrDefault(index);
                path.Visits[index] = visits + 1;
                path.CumulativeVisited.Add((epoch, index));
                if (visits >= 2)
                {
                    Incomplete = true;
                    break;
                }

                if (TryLoad(path, code, operand, arguments, original.IsStatic, stateMachine))
                    continue;
                if (code == OpCodes.Call || code == OpCodes.Callvirt || code == OpCodes.Newobj)
                {
                    if (operand is not MethodBase target)
                    {
                        Incomplete = true;
                        break;
                    }
                    var args = new Value[target.GetParameters().Length];
                    for (var i = args.Length - 1; i >= 0; i--)
                        args[i] = path.Pop();
                    var receiver = code != OpCodes.Newobj && !target.IsStatic ? path.Deref(path.Pop()) : null;
                    if (target.Name == "Deconstruct" && target.DeclaringType is { IsGenericType: true } pair
                        && pair.GetGenericTypeDefinition() == typeof(KeyValuePair<,>)
                        && receiver is not null && args is [LocalAddress keySlot, LocalAddress valueSlot])
                    {
                        path.Locals[keySlot.Slot] = new Operation("kvp_key", receiver);
                        path.Locals[valueSlot.Slot] = new Operation("kvp_value", receiver);
                        continue;
                    }
                    for (var i = 0; i < args.Length; i++)
                        args[i] = path.Materialize(path.Deref(args[i]));
                    var call = new Call(target, receiver is not null ? path.Materialize(receiver) : null, args);
                    Calls.Add(new Step(index, call, [.. path.Conditions], epoch, path.StepOrder++));
                    if (target is MethodInfo { ReturnType: var type } && type == typeof(void))
                        continue;
                    path.Stack.Add(IsAwaitCompletion(target) ? new Constant(true) : call);
                    continue;
                }

                if (code.FlowControl is FlowControl.Branch or FlowControl.Cond_Branch)
                {
                    if (code == OpCodes.Switch)
                    {
                        var selector = path.Pop();
                        if (operand is not Label[] choices)
                        {
                            Incomplete = true;
                            break;
                        }
                        if ((selector as Constant ?? Fold(selector)) is { Data: int selection })
                        {
                            if (selection >= 0 && selection < choices.Length)
                                path.Index = labels[choices[selection]];
                            continue;
                        }
                        if (choices.Length > 8)
                        {
                            Incomplete = true;
                            break;
                        }
                        for (var arm = 0; arm < choices.Length; arm++)
                        {
                            var match = CaseMatch(selector, arm);
                            var fork = path.Fork(labels[choices[arm]]);
                            fork.Conditions.Add(new Test(match, true, index));
                            pending.Push(fork);
                            path.Conditions.Add(new Test(match, false, index));
                        }
                        continue;
                    }
                    if (operand is not Label label || !labels.TryGetValue(label, out var destination))
                    {
                        Incomplete = true;
                        break;
                    }
                    if (code.FlowControl == FlowControl.Branch)
                    {
                        if (code == OpCodes.Leave || code == OpCodes.Leave_S)
                            path.Stack.Clear();
                        path.Index = destination;
                        continue;
                    }

                    var branchName = code.Name!;
                    if (branchName.EndsWith(".s", StringComparison.Ordinal))
                        branchName = branchName[..^2];
                    var expected = branchName != "brfalse";
                    var condition = branchName is "brtrue" or "brfalse"
                        ? path.Pop()
                        : ReadComparison(path, branchName);
                    if (condition is Call { Method.Name: "MoveNext", Receiver: Call { Method.Name: "GetEnumerator", Receiver: var enumerated } })
                    {
                        var bodyStart = expected ? destination : path.Index;
                        var afterLoop = expected ? path.Index : destination;
                        if (enumerated is Call { Method.Name: "Empty" })
                        {
                            path.Index = afterLoop;
                            continue;
                        }
                        // Quantify the body over the enumerator's elements. The second visit
                        // leaves the representative iteration and proceeds after the loop.
                        if (visits == 0)
                            pending.Push(path.Fork(afterLoop));
                        path.Index = visits == 0 ? bodyStart : afterLoop;
                        continue;
                    }
                    if (destination < index
                        && condition is Call { Method.DeclaringType: var decType, Method.Name: "op_LessThan", Arguments: [var decLeft, var decRight] }
                        && decType == typeof(decimal))
                    {
                        var isDecZero = decLeft is Call { Method.Name: "op_Implicit", Arguments: [Constant { Data: 0 }] }
                            || decLeft is Field { Member.Name: "Zero", Member.DeclaringType: var zt } && zt == typeof(decimal);
                        var isDecIncremented = decLeft is Call { Method.Name: "op_Increment" or "op_Addition" };
                        if (visits == 0 && expected && isDecZero && IsCountedLoopBound(decRight))
                        {
                            Loops.Add(new Loop(destination, index, decRight));
                            path.Index = destination;
                            continue;
                        }
                        if (visits == 0 && expected && isDecIncremented && IsCountedLoopBound(decRight))
                        {
                            // Bottom-tested decimal loop already ran its representative iteration.
                            Loops.Add(new Loop(destination, index, decRight));
                            continue;
                        }
                        if (visits == 1 && Loops.Any(loop => loop.Start == destination && loop.End == index)
                            && isDecIncremented)
                            continue;
                    }
                    if (destination < index && condition is Operation { Right: var rawLimit } counter)
                    {
                        var limit = rawLimit is Operation { Operator: "conv.i4" or "conv.i8", Left: var unwrappedLimit }
                            ? unwrappedLimit
                            : rawLimit;
                        if (expected && counter.Operator is "blt" or "ble"
                            && (counter.Left as Constant ?? Fold(counter.Left)) is { Data: int current }
                            && limit is Constant { Data: int bound }
                            && BodyBranchesOnLoopCounter(instructions, destination, index))
                        {
                            var total = bound + (counter.Operator == "ble" ? 1 : 0);
                            var loopRange = (destination, index);
                            var taken = path.ActiveUnrolledLoops.GetValueOrDefault(loopRange);
                            var active = counter.Operator == "ble" ? current <= bound : current < bound;
                            if (total is > 0 and <= 8 && taken <= total)
                            {
                                if (active)
                                {
                                    path.ActiveUnrolledLoops[loopRange] = taken + 1;
                                    for (var inner = destination; inner <= index; inner++)
                                        path.Visits.Remove(inner);
                                    path.Index = destination;
                                }
                                else
                                {
                                    path.ActiveUnrolledLoops.Remove(loopRange);
                                }
                                continue;
                            }
                        }
                        // A counter loop also runs one representative iteration and records its count.
                        if (visits == 0 && expected && counter is { Operator: "blt" or "ble" }
                            && (counter.Left as Constant ?? Fold(counter.Left)) is { Data: int start })
                        {
                            var alreadyIncremented = counter.Left is Operation { Operator: "add", Right: Constant { Data: 1 } };
                            if (limit is Constant { Data: int fixedBound })
                            {
                                var count = fixedBound - (alreadyIncremented ? start - 1 : start) + (counter.Operator == "ble" ? 1 : 0);
                                if (count > 0)
                                {
                                    Loops.Add(new Loop(destination, index, new Constant(count)));
                                    if (!alreadyIncremented)
                                        path.Index = destination;
                                }
                                continue;
                            }
                            if ((start == 0 || (alreadyIncremented && start == 1)) && counter.Operator == "blt"
                                && limit is not null && IsCountedLoopBound(limit))
                            {
                                Loops.Add(new Loop(destination, index, limit));
                                if (!alreadyIncremented)
                                    path.Index = destination;
                                continue;
                            }
                            if ((start == 0 || (alreadyIncremented && start == 1)) && counter.Operator == "blt"
                                && limit is Operation { Operator: "ldlen" }
                                && IsReadOnlyArrayScanLoop(instructions, destination, index))
                            {
                                // Pure read-only array scan loop (e.g. searching static priority predicates): run one representative iteration.
                                if (!alreadyIncremented)
                                {
                                    pending.Push(path.Fork(path.Index));
                                    path.Index = destination;
                                }
                                continue;
                            }
                        }
                        if (visits == 1 && counter.Left is Operation { Operator: "add", Right: Constant { Data: 1 } }
                            && (Loops.Any(loop => loop.Start == destination && loop.End == index)
                                || (limit is Operation { Operator: "ldlen" } && IsReadOnlyArrayScanLoop(instructions, destination, index))))
                            continue;
                    }
                    if (TryTruth(condition, out var truth, isMonsterMove))
                    {
                        if (truth == expected)
                            path.Index = destination;
                        continue;
                    }
                    // A value stored in a local and tested again keeps the outcome of its first test.
                    if (path.Conditions.FirstOrDefault(test => ReferenceEquals(test.Value, condition)) is { } decided)
                    {
                        if (decided.Expected == expected)
                            path.Index = destination;
                        continue;
                    }

                    var branch = path.Fork(destination);
                    branch.Conditions.Add(new Test(condition, expected, index));
                    pending.Push(branch);
                    path.Conditions.Add(new Test(condition, !expected, index));
                    continue;
                }

                if (code == OpCodes.Ret)
                {
                    if (stateMachine is null && original.ReturnType != typeof(void))
                        Returns.Add(new Result(path.Materialize(path.Pop()), [.. path.Conditions]));
                    Terminals.Add(new Terminal([.. path.Conditions], path.CumulativeVisited.ToHashSet(), true));
                    break;
                }
                if (code.FlowControl == FlowControl.Throw || code == OpCodes.Endfinally)
                {
                    Terminals.Add(new Terminal([.. path.Conditions], path.CumulativeVisited.ToHashSet(), false));
                    break;
                }

                if (code == OpCodes.Pop)
                    _ = path.Pop();
                else if (code == OpCodes.Initobj)
                {
                    var targetAddr = path.Pop();
                    if (targetAddr is LocalAddress initSlot)
                        path.Locals[initSlot.Slot] = new Constant(0);
                    else if (targetAddr is Input initParam)
                        OutStores.Add(new OutStore(initParam, new Constant(0m), [.. path.Conditions]));
                    else if (targetAddr is Field initField && initField.Receiver != StateMachine)
                        FieldSteps.Add(new FieldStep(index, new Assignment(initField, new Constant(0m)), [.. path.Conditions], epoch, path.StepOrder++));
                }
                else if (code == OpCodes.Stobj || code.Name?.StartsWith("stind", StringComparison.Ordinal) == true)
                {
                    var storedValue = path.Pop();
                    var address = path.Pop();
                    if (address is LocalAddress storeSlot)
                        path.Locals[storeSlot.Slot] = storedValue;
                    else if (address is Input storeParam)
                        OutStores.Add(new OutStore(storeParam, storedValue, [.. path.Conditions]));
                    else if (address is Field storeField && storeField.Receiver != StateMachine)
                        FieldSteps.Add(new FieldStep(index, new Assignment(storeField, storedValue), [.. path.Conditions], epoch, path.StepOrder++));
                }
                else if (code == OpCodes.Dup)
                    path.Stack.Add(path.Stack.Count == 0 ? Missing : path.Stack[^1]);
                else if (code == OpCodes.Stfld && operand is FieldInfo field)
                {
                    var value = path.Pop();
                    var receiver = path.Deref(path.Pop());
                    if (receiver == StateMachine)
                        path.StateFields[field] = value;
                    else
                        FieldSteps.Add(new FieldStep(index, new Assignment(new Field(field, receiver), value), [.. path.Conditions], epoch, path.StepOrder++));
                }
                else if (code == OpCodes.Stsfld && operand is FieldInfo staticField)
                {
                    _ = path.Pop();
                    if (!IsCompilerDelegateCache(staticField))
                        Writes.Add(staticField);
                }
                else if (code == OpCodes.Isinst || code == OpCodes.Castclass || code == OpCodes.Box
                    || code == OpCodes.Unbox_Any)
                {
                    var value = path.Pop();
                    // A monster reaching itself through its creature is the same receiver.
                    if (code == OpCodes.Castclass && operand is Type cast && typeof(MonsterModel).IsAssignableFrom(cast)
                        && value is Call { Method.Name: "get_Monster", Receiver: Call { Method.Name: "get_Creature", Receiver: var owner } }
                        && owner == Self)
                        path.Stack.Add(Self);
                    else
                        path.Stack.Add(code == OpCodes.Isinst || code == OpCodes.Castclass
                            ? new Operation(code.Name!, value, new Constant(operand))
                            : value);
                }
                else if (code == OpCodes.Neg || code == OpCodes.Not)
                    path.Stack.Add(new Operation(code.Name!, path.Pop()));
                else if (code == OpCodes.Ceq || code == OpCodes.Cgt || code == OpCodes.Cgt_Un
                    || code == OpCodes.Clt || code == OpCodes.Clt_Un || code == OpCodes.Add
                    || code == OpCodes.Sub || code == OpCodes.Mul || code == OpCodes.Div
                    || code == OpCodes.Rem || code == OpCodes.And || code == OpCodes.Or)
                {
                    var right = path.Pop();
                    path.Stack.Add(new Operation(code.Name!, path.Pop(), right));
                }
                else if (code.Name?.StartsWith("conv.", StringComparison.Ordinal) == true)
                    path.Stack.Add(new Operation(code.Name, path.Pop()));
                else if (code == OpCodes.Newarr)
                {
                    var len = path.Pop();
                    var arrId = ++path.NextArrayId;
                    path.ArrayElements[arrId] = [];
                    path.Stack.Add(new ArrayInit(operand as Type, len, arrId));
                }
                else if (code == OpCodes.Ldlen)
                {
                    var arr = path.Pop();
                    path.Stack.Add(arr switch
                    {
                        ArrayInit { Length: var initLen } => initLen,
                        ArrayValue { Length: var valLen } => valLen,
                        Operation { Operator: "newarr", Left: var len } => len,
                        _ => new Operation("ldlen", arr),
                    });
                }
                else if (code.Name?.StartsWith("stelem", StringComparison.Ordinal) == true)
                {
                    var elemVal = path.Pop();
                    var elemIdx = path.Pop();
                    var arr = path.Pop();
                    if (arr is ArrayInit init)
                    {
                        if (elemIdx is Constant { Data: int idx } && idx >= 0
                            && !path.InvalidatedArrays.Contains(init.Id)
                            && path.ArrayElements.TryGetValue(init.Id, out var slots))
                        {
                            slots[idx] = elemVal;
                        }
                        else
                        {
                            // Dynamic or out-of-range index write invalidates constant element tracking for this array.
                            path.InvalidatedArrays.Add(init.Id);
                            path.ArrayElements.Remove(init.Id);
                        }
                    }
                }
                else if (code.Name?.StartsWith("ldelem", StringComparison.Ordinal) == true)
                {
                    var element = path.Pop();
                    var arr = path.Pop();
                    if (arr is ArrayInit init && !path.InvalidatedArrays.Contains(init.Id)
                        && element is Constant { Data: int idx }
                        && path.ArrayElements.TryGetValue(init.Id, out var slots)
                        && slots.TryGetValue(idx, out var stored))
                    {
                        path.Stack.Add(stored);
                    }
                    else
                    {
                        path.Stack.Add(new Operation(code.Name, arr, element));
                    }
                }
                else if (code != OpCodes.Nop && code.OpCodeType != OpCodeType.Prefix)
                {
                    Incomplete = true;
                    break;
                }
            }
        }
    }

    private static bool IsReadOnlyArrayScanLoop(List<CodeInstruction> instructions, int start, int end)
    {
        for (var cursor = start; cursor < end; cursor++)
        {
            var code = instructions[cursor].opcode;
            if (code == OpCodes.Stfld || code == OpCodes.Stsfld || code.Name?.StartsWith("stelem", StringComparison.Ordinal) == true)
                return false;
            if ((code == OpCodes.Call || code == OpCodes.Callvirt) && instructions[cursor].operand is MethodBase called)
            {
                var declaring = called.DeclaringType;
                if (declaring != typeof(Enumerable) && declaring?.Namespace != "System.Linq" && !called.Name.StartsWith("get_", StringComparison.Ordinal))
                    return false;
            }
        }
        return true;
    }

    private static bool IsCountedLoopBound(Value? limit) => limit switch
    {
        Call { Method.Name: "op_Implicit", Arguments: [var inner] } => IsCountedLoopBound(inner),
        Call { Method.DeclaringType: var owner, Method.Name: "get_Amount", Receiver: Input { Name: "this" } }
            when owner == typeof(PowerModel) => true,
        Call { Method: MethodInfo getter, Receiver: Input { Name: "this" }, Arguments.Length: 0 }
            when getter.ReturnType == typeof(int) && typeof(AbstractModel).IsAssignableFrom(getter.DeclaringType) => true,
        Call { Method.Name: "get_IntValue" or "get_BaseValue", Method.DeclaringType: var dvType }
            when typeof(MegaCrit.Sts2.Core.Localization.DynamicVars.DynamicVar).IsAssignableFrom(dvType) => true,
        Call { Method.Name: "get_Count" } => true,
        Operation { Operator: "kvp_value" } => true,
        _ => false,
    };

    private static bool BodyBranchesOnLoopCounter(List<CodeInstruction> instructions, int start, int end)
    {
        // Look at the operand loaded immediately before the back-branch limit (`i < bound`).
        FieldInfo? counterField = null;
        int? counterSlot = null;
        for (var cursor = Math.Max(start, end - 4); cursor < end; cursor++)
        {
            var code = instructions[cursor].opcode;
            if (code == OpCodes.Ldfld && instructions[cursor].operand is FieldInfo field)
                counterField = field;
            else if (code.Name?.StartsWith("ldloc", StringComparison.Ordinal) == true
                && !code.Name.StartsWith("ldloca", StringComparison.Ordinal))
                counterSlot = Slot(code.Name, instructions[cursor].operand);
        }
        var loads = 0;
        for (var cursor = start; cursor < end; cursor++)
        {
            var code = instructions[cursor].opcode;
            if (counterField is not null && code == OpCodes.Ldfld && Equals(instructions[cursor].operand, counterField))
                loads++;
            else if (counterSlot is not null && code.Name?.StartsWith("ldloc", StringComparison.Ordinal) == true
                && !code.Name.StartsWith("ldloca", StringComparison.Ordinal)
                && Slot(code.Name, instructions[cursor].operand) == counterSlot)
                loads++;
        }
        // One load is for the loop increment (`i++`) and one is for the back-branch test (`i < bound`).
        // Any additional load means the loop body itself reads the iteration index.
        return loads > 2;
    }

    private static Operation CaseMatch(Value selector, int arm)
        => selector is Operation { Operator: "sub", Left: var inner, Right: Constant { Data: int offset } }
            ? new Operation("ceq", inner, new Constant(arm + offset))
            : new Operation("ceq", selector, new Constant(arm));

    private static bool IsCompilerDelegateCache(FieldInfo field)
        => typeof(Delegate).IsAssignableFrom(field.FieldType)
            && (field.Name.StartsWith("<>9__", StringComparison.Ordinal)
                || field.DeclaringType?.Name == "<>O"
                || field.DeclaringType?.IsDefined(typeof(CompilerGeneratedAttribute), inherit: false) == true
                    && field.Name.StartsWith("<", StringComparison.Ordinal));

    private bool TryLoad(Path path, OpCode code, object? operand, Input[] arguments, bool isStatic, Type? stateMachine)
    {
        var name = code.Name!;
        if (name.StartsWith("ldarg", StringComparison.Ordinal))
        {
            var index = Slot(name, operand);
            // MoveNext has a state-machine receiver even when the original method is static.
            if (index == 0 && (!isStatic || stateMachine is not null))
                path.Stack.Add(stateMachine is null ? Self : StateMachine);
            else
            {
                ReadsParameters = true;
                path.Stack.Add(arguments[isStatic ? index : index - 1]);
            }
        }
        else if (name.StartsWith("ldloca", StringComparison.Ordinal))
            path.Stack.Add(new LocalAddress(Slot(name, operand)));
        else if (name.StartsWith("ldloc", StringComparison.Ordinal))
            path.Stack.Add(path.Locals.GetValueOrDefault(Slot(name, operand), Missing));
        else if (name.StartsWith("stloc", StringComparison.Ordinal))
            path.Locals[Slot(name, operand)] = path.Pop();
        else if ((code == OpCodes.Ldfld || code == OpCodes.Ldflda) && operand is FieldInfo field)
        {
            var receiver = path.Deref(path.Pop());
            Value value = new Field(field, receiver);
            if (receiver == StateMachine)
            {
                if (field.Name == "<>4__this")
                    value = Self;
                else if (path.StateFields.TryGetValue(field, out var stored))
                    value = stored;
                else if (field.Name == "<>1__state")
                    value = new Constant(-1);
                else if (arguments.FirstOrDefault(arg => arg.Name == field.Name) is { } argument)
                {
                    ReadsParameters = true;
                    value = argument;
                }
            }
            path.Stack.Add(value);
        }
        else if ((code == OpCodes.Ldsfld || code == OpCodes.Ldsflda) && operand is FieldInfo staticField)
            path.Stack.Add(IsCompilerDelegateCache(staticField)
                ? new Constant(null)
                : new Field(staticField, null));
        else if (code == OpCodes.Ldnull)
            path.Stack.Add(new Constant(null));
        else if (code == OpCodes.Ldstr || code == OpCodes.Ldftn || code == OpCodes.Ldtoken
            || code == OpCodes.Ldc_R4 || code == OpCodes.Ldc_R8 || code == OpCodes.Ldc_I8)
            path.Stack.Add(new Constant(operand));
        else if (code == OpCodes.Ldvirtftn)
        {
            _ = path.Pop();
            path.Stack.Add(new Constant(operand));
        }
        else if (name.StartsWith("ldc.i4", StringComparison.Ordinal))
            path.Stack.Add(new Constant(code == OpCodes.Ldc_I4_M1 ? -1 : Slot(name, operand)));
        else
            return false;
        return true;
    }

    private static int Slot(string opcode, object? operand) => operand switch
    {
        LocalVariableInfo local => local.LocalIndex,
        ParameterInfo argument => argument.Position + (argument.Member is MethodBase { IsStatic: true } ? 0 : 1),
        not null => Convert.ToInt32(operand, CultureInfo.InvariantCulture),
        _ => int.Parse(opcode.AsSpan(opcode.LastIndexOf('.') + 1), CultureInfo.InvariantCulture),
    };

    private static Value ReadComparison(Path path, string branch)
    {
        var right = path.Pop();
        return new Operation(branch, path.Pop(), right);
    }

    internal static bool TryTruth(Value value, out bool truth, bool allowMoveTargetsPlayer = false)
    {
        // Async dispatch compares arithmetic over the constant initial state.
        if (value is Operation { Right: { } comparedRight } compared)
            value = compared with { Left = Fold(compared.Left) ?? compared.Left, Right = Fold(comparedRight) ?? comparedRight };
        switch (value)
        {
            case Constant { Data: null }:
                truth = false;
                return true;
            case Constant { Data: bool flag }:
                truth = flag;
                return true;
            case Constant { Data: int number }:
                truth = number != 0;
                return true;
            case Constant { Data: MemberInfo or string }:
                truth = true;
                return true;
            case Call { Method: ConstructorInfo }:
                truth = true;
                return true;
            // Godot scene nodes only guard visual and audio presentation; combat rules run without them.
            case Call { Method: MethodInfo nodeLookup }
                when nodeLookup.ReturnType.Namespace?.StartsWith("MegaCrit.Sts2.Core.Nodes", StringComparison.Ordinal) == true
                    || nodeLookup.ReturnType.Namespace?.StartsWith("Godot", StringComparison.Ordinal) == true:
                truth = false;
                return true;
            // Test-runner switches bypass presentation code while preserving exact combat semantics.
            case Call { Method: { Name: "get_IsOff" or "get_IsOn" } testMode, Receiver: null }
                when testMode.DeclaringType == typeof(TestMode):
                truth = testMode.Name == "get_IsOn";
                return true;
            case Call { Method: { Name: "IsDread" } dread, Receiver: null }
                when dread.DeclaringType == typeof(AscensionHelper):
                truth = AscensionHelper.IsDread();
                return true;
            // The rules describe a combat in progress.
            case Call { Method: { Name: "IsLiveCombat" or "get_IsInProgress" } live } when live.DeclaringType is { } combat
                && (typeof(ICombatState).IsAssignableFrom(combat) || combat == typeof(CombatManager)):
                truth = true;
                return true;
            case Call { Method: { Name: "get_PlayerCombatState" } pcs, Arguments.Length: 0 }
                when pcs.DeclaringType == typeof(MegaCrit.Sts2.Core.Entities.Players.Player):
                truth = true;
                return true;
            case Call { Method: { Name: "get_AllCards" } allCards, Arguments.Length: 0 }
                when allCards.DeclaringType == typeof(MegaCrit.Sts2.Core.Entities.Players.PlayerCombatState):
                truth = true;
                return true;
            case Call { Method: { Name: "get_Player" } gp, Arguments.Length: 0,
                Receiver: Call { Method.Name: "get_Current", Receiver: Call { Method.Name: "GetEnumerator", Receiver: Input { Name: "targets" } } } }
                when allowMoveTargetsPlayer && gp.DeclaringType == typeof(MegaCrit.Sts2.Core.Entities.Creatures.Creature):
                truth = true;
                return true;
            // The model's own creature is in that combat, and the combat has an encounter.
            case Call { Method.Name: "get_CombatState" or "get_Encounter" } member when RootedAtSelf(member):
                truth = true;
                return true;
            case Operation { Left: Constant left, Right: Constant right } operation:
                if (left.Data is int a && right.Data is int b)
                {
                    truth = operation.Operator switch
                    {
                        "beq" or "ceq" => a == b,
                        "bne.un" => a != b,
                        "bgt" or "cgt" => a > b,
                        "bge" => a >= b,
                        "blt" or "clt" => a < b,
                        "ble" => a <= b,
                        "bgt.un" => (uint)a > (uint)b,
                        "bge.un" => (uint)a >= (uint)b,
                        "blt.un" => (uint)a < (uint)b,
                        "ble.un" => (uint)a <= (uint)b,
                        "cgt.un" => (uint)a > (uint)b,
                        "clt.un" => (uint)a < (uint)b,
                        _ => false,
                    };
                    return operation.Operator is "beq" or "ceq" or "bne.un" or "bgt" or "cgt"
                        or "bge" or "blt" or "clt" or "ble" or "cgt.un" or "clt.un"
                        or "bgt.un" or "bge.un" or "blt.un" or "ble.un";
                }
                if (operation.Operator is "beq" or "ceq" or "bne.un")
                {
                    truth = Equals(left.Data, right.Data) == (operation.Operator != "bne.un");
                    return true;
                }
                break;
        }
        truth = false;
        return false;
    }

    private static Constant? Fold(Value value)
        => value is Operation { Operator: "add" or "sub", Left: var left, Right: { } right } operation
            && (left as Constant ?? Fold(left)) is { Data: int a } && (right as Constant ?? Fold(right)) is { Data: int b }
            ? new Constant(operation.Operator == "add" ? a + b : a - b)
            : null;

    private static bool RootedAtSelf(Value value) => value switch
    {
        _ when value == Self => true,
        Call { Method.Name: "get_Owner" or "get_Creature" or "get_CombatState", Arguments.Length: 0, Receiver: { } receiver }
            => RootedAtSelf(receiver),
        Call { Method.Name: "get_Encounter", Receiver: { } receiver } => RootedAtSelf(receiver),
        _ => false,
    };

    private static bool IsAwaitCompletion(MethodBase method)
        => method.Name == "get_IsCompleted"
            && method.DeclaringType?.Namespace == "System.Runtime.CompilerServices"
            && method.DeclaringType.Name.StartsWith("TaskAwaiter", StringComparison.Ordinal);
}
