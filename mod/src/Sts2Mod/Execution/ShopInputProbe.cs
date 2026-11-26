using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Models.Relics;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events.Custom;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Mod.Execution;

/// <summary>
/// Availability checks for native merchant room open/close/leave controls and async shop settlement.
/// </summary>
internal static class ShopInputProbe
{
    private static readonly FieldInfo? MerchantInputBlockedField =
        typeof(NMerchantInventory).GetField("_isInputBlocked", BindingFlags.Instance | BindingFlags.NonPublic);

    private static readonly FieldInfo? MerchantPurchaseInFlightField =
        typeof(MerchantInventory).GetField("_purchaseInFlight", BindingFlags.Instance | BindingFlags.NonPublic);

    private static readonly FieldInfo? MerchantSynchronizerInventoriesField =
        typeof(MerchantSynchronizer).GetField("_inventories", BindingFlags.Instance | BindingFlags.NonPublic);

    public static bool IsCurrent(NMerchantRoom room) =>
        GodotObject.IsInstanceValid(room)
        && room.IsInsideTree()
        && RunManager.Instance.IsInProgress
        && RunManager.Instance.DebugOnlyGetState() is { CurrentRoom: MerchantRoom } run
        && TryGetLocalInventory(run) is { } localInventory
        && GodotObject.IsInstanceValid(room.Inventory)
        && ReferenceEquals(room.Inventory.Inventory, localInventory);

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

    /// <summary>
    /// True while a merchant inventory or shop relic (such as Lord's Parasol) is still running async purchases
    /// and no interactive reward overlay spawned by that purchase is awaiting player action.
    /// </summary>
    public static bool IsShopSettling(IRunState? run)
    {
        var rewardsOverlayActive = NOverlayStack.Instance is { } overlays
                                   && GodotObject.IsInstanceValid(overlays)
                                   && overlays.Peek() is NRewardsScreen;
        var hasIncompleteParasol = run?.CurrentRoom is MerchantRoom
                                   && run.Players.Count == 1
                                   && run.Players[0].Relics.OfType<LordsParasol>()
                                       .Any(relic => relic.LastPurchaseEverythingTask is { IsCompleted: false });
        var merchantPanel = run?.CurrentRoom is MerchantRoom
                            && NMerchantRoom.Instance is { } merchantRoom
                            && GodotObject.IsInstanceValid(merchantRoom)
                            && GodotObject.IsInstanceValid(merchantRoom.Inventory)
            ? merchantRoom.Inventory
            : null;
        var fakePanel = NEventRoom.Instance is { } eventRoom
                        && GodotObject.IsInstanceValid(eventRoom)
                        && eventRoom.CustomEventNode is NFakeMerchant fakeMerchant
                        && GodotObject.IsInstanceValid(fakeMerchant)
                        && GodotObject.IsInstanceValid(fakeMerchant.Inventory)
            ? fakeMerchant.Inventory
            : null;
        var inputBlocked = IsInventoryInputBlocked(merchantPanel) || IsInventoryInputBlocked(fakePanel);
        var purchaseInFlight = IsPurchaseInFlight(merchantPanel?.Inventory)
                               || IsPurchaseInFlight(fakePanel?.Inventory)
                               || (run?.CurrentRoom is MerchantRoom
                                   && IsPurchaseInFlight(TryGetLocalInventory(run)));
        return IsShopSettlingCore(hasIncompleteParasol, inputBlocked, purchaseInFlight, rewardsOverlayActive);
    }

    internal static bool IsShopSettlingCore(
        bool hasIncompleteAutoPurchaseTask,
        bool inventoryInputBlocked,
        bool purchaseInFlight,
        bool rewardsOverlayActive = false) =>
        !rewardsOverlayActive && (hasIncompleteAutoPurchaseTask || inventoryInputBlocked || purchaseInFlight);

    internal static MerchantInventory? TryGetLocalInventory(IRunState? run)
    {
        if (run is null || run.Players.Count != 1)
            return null;
        var player = run.Players[0];
        if (RunManager.Instance?.MerchantSynchronizer is not { } synchronizer
            || MerchantSynchronizerInventoriesField?.GetValue(synchronizer)
                is not IReadOnlyList<MerchantInventory> inventories
            || inventories.Count == 0)
        {
            return null;
        }
        return inventories.FirstOrDefault(inventory => ReferenceEquals(inventory.Player, player));
    }

    private static bool IsInventoryInputBlocked(NMerchantInventory? panel) =>
        panel is not null
        && GodotObject.IsInstanceValid(panel)
        && MerchantInputBlockedField?.GetValue(panel) is true;

    private static bool IsPurchaseInFlight(MerchantInventory? inventory) =>
        inventory is not null
        && MerchantPurchaseInFlightField?.GetValue(inventory) is true;

    private static bool IsIdle(NMerchantRoom room) =>
        IsCurrent(room)
        && !CombatManager.Instance.IsInProgress
        && CardSelectionInterceptor.GetPending() is null
        && NOverlayStack.Instance?.Peek() is null
        && RunManager.Instance.ActionQueueSet.IsEmpty
        && RunManager.Instance.ActionExecutor.CurrentlyRunningAction is null
        && !IsShopSettling(RunManager.Instance.DebugOnlyGetState())
        && room.GetTree().Paused == false;
}
