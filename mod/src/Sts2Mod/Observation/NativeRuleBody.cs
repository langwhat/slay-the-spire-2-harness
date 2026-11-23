using System.Globalization;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
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
    internal sealed record Field(FieldInfo Member, Value? Receiver) : Value;
    internal sealed record Call(MethodBase Method, Value? Receiver, Value[] Arguments) : Value;
    internal sealed record Operation(string Operator, Value Left, Value? Right = null) : Value;
    internal sealed record Test(Value Value, bool Expected, int Offset = -1);
    internal sealed record Step(int Index, Call Call, Test[] Conditions);
    internal sealed record Result(Value Value, Test[] Conditions);
    // A completed path with the instructions it executed. Paths that miss an instruction
    // bound the conditions under which that instruction runs.
    internal sealed record Terminal(Test[] Conditions, IReadOnlySet<int> Visited, bool Returned);
    // A counted loop runs the instructions from Start up to its back branch at End Count times.
    internal sealed record Loop(int Start, int End, int Count);

    private sealed class Path
    {
        public int Index;
        public List<Value> Stack { get; } = [];
        public Dictionary<int, Value> Locals { get; } = [];
        public Dictionary<FieldInfo, Value> StateFields { get; } = [];
        public Dictionary<int, int> Visits { get; } = [];
        public List<Test> Conditions { get; } = [];

        public Path Fork(int index)
        {
            var copy = new Path { Index = index };
            copy.Stack.AddRange(Stack);
            copy.Conditions.AddRange(Conditions);
            foreach (var (key, value) in Locals)
                copy.Locals.Add(key, value);
            foreach (var (key, value) in StateFields)
                copy.StateFields.Add(key, value);
            foreach (var (key, value) in Visits)
                copy.Visits.Add(key, value);
            return copy;
        }

        public Value Pop()
        {
            if (Stack.Count == 0)
                return Missing;
            var value = Stack[^1];
            Stack.RemoveAt(Stack.Count - 1);
            return value;
        }
    }

    private static readonly Input Missing = new("?");
    private static readonly Input StateMachine = new("async_state");
    internal static readonly Input Self = new("this");

    public List<Step> Calls { get; } = [];
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
        var pending = new Stack<Path>();
        pending.Push(new Path());
        var remainingInstructions = 16000;
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
                var instruction = instructions[index];
                var code = instruction.opcode;
                var operand = instruction.operand;
                var visits = path.Visits.GetValueOrDefault(index);
                path.Visits[index] = visits + 1;
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
                    var receiver = code != OpCodes.Newobj && !target.IsStatic ? path.Pop() : null;
                    var call = new Call(target, receiver, args);
                    Calls.Add(new Step(index, call, [.. path.Conditions]));
                    if (target is MethodInfo { ReturnType: var type } && type == typeof(void))
                        continue;
                    path.Stack.Add(IsAwaitCompletion(target) ? new Constant(true) : call);
                    continue;
                }

                if (code.FlowControl is FlowControl.Branch or FlowControl.Cond_Branch)
                {
                    if (code == OpCodes.Switch)
                    {
                        if (path.Pop() is not Constant { Data: int selection } || operand is not Label[] choices)
                        {
                            Incomplete = true;
                            break;
                        }
                        if (selection >= 0 && selection < choices.Length)
                            path.Index = labels[choices[selection]];
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
                    if (condition is Call { Method.Name: "MoveNext", Receiver: Call { Method.Name: "GetEnumerator" } })
                    {
                        // Quantify the body over the enumerator's elements. The second visit
                        // leaves the representative iteration and proceeds after the loop.
                        if (visits == 0)
                            pending.Push(path.Fork(path.Index));
                        if (expected == (visits == 0))
                            path.Index = destination;
                        continue;
                    }
                    if (destination < index && condition is Operation { Right: Constant { Data: int bound } } counter)
                    {
                        // A counter loop also runs one representative iteration and records its count.
                        if (visits == 0 && expected && counter is { Operator: "blt" or "ble", Left: Constant { Data: int start } })
                        {
                            var count = bound - start + (counter.Operator == "ble" ? 1 : 0);
                            if (count > 0)
                            {
                                Loops.Add(new Loop(destination, index, count));
                                path.Index = destination;
                            }
                            continue;
                        }
                        if (visits == 1 && Loops.Any(loop => loop.Start == destination && loop.End == index)
                            && counter.Left is Operation { Operator: "add", Left: Constant { Data: int }, Right: Constant { Data: 1 } })
                            continue;
                    }
                    if (TryTruth(condition, out var truth))
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
                        Returns.Add(new Result(path.Pop(), [.. path.Conditions]));
                    Terminals.Add(new Terminal([.. path.Conditions], path.Visits.Keys.ToHashSet(), true));
                    break;
                }
                if (code.FlowControl == FlowControl.Throw || code == OpCodes.Endfinally)
                {
                    Terminals.Add(new Terminal([.. path.Conditions], path.Visits.Keys.ToHashSet(), false));
                    break;
                }

                if (code == OpCodes.Pop || code == OpCodes.Initobj)
                    _ = path.Pop();
                else if (code == OpCodes.Dup)
                    path.Stack.Add(path.Stack.Count == 0 ? Missing : path.Stack[^1]);
                else if (code == OpCodes.Stfld && operand is FieldInfo field)
                {
                    var value = path.Pop();
                    if (path.Pop() == StateMachine)
                        path.StateFields[field] = value;
                    else
                        Writes.Add(field);
                }
                else if (code == OpCodes.Stsfld && operand is FieldInfo staticField)
                {
                    _ = path.Pop();
                    if (!staticField.Name.StartsWith("<>9__", StringComparison.Ordinal))
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
                // Arrays only carry results between calls; their elements are not tracked.
                else if (code == OpCodes.Newarr)
                    path.Stack.Add(new Operation("newarr", path.Pop(), new Constant(operand)));
                else if (code.Name?.StartsWith("stelem", StringComparison.Ordinal) == true)
                {
                    _ = path.Pop();
                    _ = path.Pop();
                    _ = path.Pop();
                }
                else if (code.Name?.StartsWith("ldelem", StringComparison.Ordinal) == true)
                {
                    var element = path.Pop();
                    path.Stack.Add(new Operation(code.Name, path.Pop(), element));
                }
                else if (code != OpCodes.Nop && code.OpCodeType != OpCodeType.Prefix)
                {
                    Incomplete = true;
                    break;
                }
            }
        }
    }

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
        else if (name.StartsWith("ldloc", StringComparison.Ordinal))
            path.Stack.Add(path.Locals.GetValueOrDefault(Slot(name, operand), Missing));
        else if (name.StartsWith("stloc", StringComparison.Ordinal))
            path.Locals[Slot(name, operand)] = path.Pop();
        else if ((code == OpCodes.Ldfld || code == OpCodes.Ldflda) && operand is FieldInfo field)
        {
            var receiver = path.Pop();
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
            path.Stack.Add(staticField.Name.StartsWith("<>9__", StringComparison.Ordinal)
                && typeof(Delegate).IsAssignableFrom(staticField.FieldType)
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

    internal static bool TryTruth(Value value, out bool truth)
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
            // Test-runner switches only guard presentation code. Read the running process's value.
            case Call { Method: { Name: "get_IsOff" or "get_IsOn" } testMode, Receiver: null }
                when testMode.DeclaringType == typeof(TestMode):
                truth = testMode.Name == "get_IsOff" ? TestMode.IsOff : TestMode.IsOn;
                return true;
            // The rules describe a combat in progress.
            case Call { Method: { Name: "IsLiveCombat" } live } when live.DeclaringType is { } combat
                && typeof(ICombatState).IsAssignableFrom(combat):
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
