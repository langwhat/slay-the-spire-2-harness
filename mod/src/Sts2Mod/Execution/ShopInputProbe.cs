using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Mod.Execution;

/// <summary>
/// Availability checks for native merchant room open/close/leave controls.
/// </summary>
internal static class ShopInputProbe
{
    public static bool IsCurrent(NMerchantRoom room) =>
        GodotObject.IsInstanceValid(room)
        && room.IsInsideTree()
        && RunManager.Instance.IsInProgress
        && RunManager.Instance.DebugOnlyGetState()?.CurrentRoom is MerchantRoom
        && ReferenceEquals(room.Inventory.Inventory, RunManager.Instance.MerchantSynchronizer.GetLocalInventory());

    public static bool CanOpen(NMerchantRoom room) =>
        IsIdle(room)
        && !room.Inventory.IsOpen
        && NMapScreen.Instance?.IsOpen != true;

    public static bool CanClose(NMerchantRoom room) =>
        IsIdle(room)
        && room.Inventory.IsOpen
        && NMapScreen.Instance?.IsOpen != true;

    public static bool CanLeave(NMerchantRoom room) =>
        IsIdle(room)
        && !room.Inventory.IsOpen
        && NMapScreen.Instance is { IsOpen: false };

    public static bool IsVisible(Control control) =>
        GodotObject.IsInstanceValid(control)
        && !control.IsQueuedForDeletion()
        && control.IsVisibleInTree();

    public static bool IsCloseControl(NMerchantRoom room, object control) =>
        control is NBackButton back
        && GodotObject.IsInstanceValid(back)
        && room.Inventory.IsAncestorOf(back);

    private static bool IsIdle(NMerchantRoom room) =>
        IsCurrent(room)
        && !CombatManager.Instance.IsInProgress
        && CardSelectionInterceptor.GetPending() is null
        && NOverlayStack.Instance?.Peek() is null
        && RunManager.Instance.ActionQueueSet.IsEmpty
        && RunManager.Instance.ActionExecutor.CurrentlyRunningAction is null
        && room.GetTree().Paused == false;
}
