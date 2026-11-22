using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Hooks;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Mod.Observation;

// Index gameplay dispatchers once. Custom callbacks and calls from individual models
// still need discovery from the effect path that registers or invokes them.
internal static class NativeHookDispatch
{
    private const BindingFlags Declared = BindingFlags.DeclaredOnly | BindingFlags.Public | BindingFlags.NonPublic
        | BindingFlags.Static | BindingFlags.Instance;

    private sealed record DispatchIndex(HashSet<MethodInfo> Slots, IReadOnlyList<string> Unread);
    private static readonly Lazy<DispatchIndex> Index = new(ReadDispatchers);

    internal static IReadOnlyList<string> Unread => Index.Value.Unread;

    internal static IEnumerable<MethodInfo> Overrides(Type type) => type.GetMethods(Declared & ~BindingFlags.DeclaredOnly)
        .Where(method => method.IsVirtual && !method.IsSpecialName && method.DeclaringType is { } declaring
            && declaring != typeof(AbstractModel) && declaring != typeof(MonsterModel) && declaring != typeof(PowerModel)
            && Index.Value.Slots.Contains(method.GetBaseDefinition()));

    private static DispatchIndex ReadDispatchers()
    {
        var slots = new HashSet<MethodInfo>();
        var unread = new List<string>();
        foreach (var dispatcher in new[] { typeof(Hook), typeof(Creature), typeof(CreatureCmd), typeof(PowerCmd) })
            ReadType(dispatcher, slots, unread);
        return new DispatchIndex(slots, unread);
    }

    private static void ReadType(Type type, HashSet<MethodInfo> slots, List<string> unread)
    {
        foreach (var method in type.GetMethods(Declared))
        {
            if (method.GetMethodBody() is null)
                continue;
            try
            {
                // The generator is only used for instruction labels, including in open generic methods.
                var labels = new DynamicMethod("NativeHookLabels", typeof(void), Type.EmptyTypes).GetILGenerator();
                foreach (var instruction in PatchProcessor.GetOriginalInstructions(method, labels))
                {
                    if (instruction.opcode == OpCodes.Callvirt && instruction.operand is MethodInfo { IsVirtual: true } call
                        && typeof(AbstractModel).IsAssignableFrom(call.DeclaringType))
                        slots.Add(call.GetBaseDefinition());
                }
            }
            catch (Exception error) when (error is ArgumentException or InvalidOperationException or NotSupportedException)
            {
                unread.Add($"{type.Name}.{method.Name} ({error.GetType().Name})");
            }
        }
        foreach (var nested in type.GetNestedTypes(BindingFlags.Public | BindingFlags.NonPublic))
            ReadType(nested, slots, unread);
    }
}
