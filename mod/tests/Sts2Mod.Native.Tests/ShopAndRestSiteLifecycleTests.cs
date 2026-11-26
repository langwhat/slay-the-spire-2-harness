using System.Reflection;
using System.Runtime.CompilerServices;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Models.Relics;
using MegaCrit.Sts2.Core.Multiplayer.Game;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using Sts2Mod.Core.Identity;
using Sts2Mod.Execution;
using Sts2Mod.Observation;
using Xunit;

namespace Sts2Mod.Native.Tests;

public sealed class ShopAndRestSiteLifecycleTests
{
    [Theory]
    [InlineData(false, false, false, false, false)]
    [InlineData(true, false, false, false, true)]
    [InlineData(false, true, false, false, true)]
    [InlineData(false, false, true, false, true)]
    [InlineData(true, true, true, false, true)]
    [InlineData(true, true, true, true, false)]
    [InlineData(false, false, true, true, false)]
    public void ShopInputProbe_IsShopSettlingCore_RespectsPurchaseStateAndRewardsOverlay(
        bool hasIncompleteAutoPurchaseTask,
        bool inventoryInputBlocked,
        bool purchaseInFlight,
        bool rewardsOverlayActive,
        bool expected)
    {
        Assert.Equal(
            expected,
            ShopInputProbe.IsShopSettlingCore(
                hasIncompleteAutoPurchaseTask,
                inventoryInputBlocked,
                purchaseInFlight,
                rewardsOverlayActive));
    }

    [Fact]
    public void ShopInputProbe_NativeSettlementMembers_ExistWithExpectedTypes()
    {
        var parasolTask = typeof(LordsParasol).GetProperty(
            nameof(LordsParasol.LastPurchaseEverythingTask),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(parasolTask);
        Assert.Equal(typeof(Task), parasolTask.PropertyType);

        var inputBlocked = typeof(NMerchantInventory).GetField(
            "_isInputBlocked",
            BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(inputBlocked);
        Assert.Equal(typeof(bool), inputBlocked.FieldType);

        var purchaseInFlight = typeof(MerchantInventory).GetField(
            "_purchaseInFlight",
            BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(purchaseInFlight);
        Assert.Equal(typeof(bool), purchaseInFlight.FieldType);

        var syncInventories = typeof(MerchantSynchronizer).GetField(
            "_inventories",
            BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(syncInventories);
        Assert.True(typeof(IReadOnlyList<MerchantInventory>).IsAssignableFrom(syncInventories.FieldType));
    }

    [Fact]
    public void RunSurfaceLifecycle_RegisterRestSiteInteraction_KeepsStableRefAcrossButtonRecreation()
    {
        var registry = new NodeObjectRegistry();
        registry.EnterNode(new object());

        var option = (RestSiteOption)RuntimeHelpers.GetUninitializedObject(typeof(HealRestSiteOption));
        var firstButton = new object();
        var secondButton = new object();

        registry.Register(option, PublicObjectKind.Option);
        RunSurfaceLifecycle.RegisterRestSiteInteraction(registry, firstButton, option);
        var originalRef = RunSurfaceLifecycle.GetInteractionRef(registry, firstButton);

        // Even if the recreated button had a prior standalone binding, rebinding retires it and preserves the option ref.
        RunSurfaceLifecycle.RegisterInteraction(registry, secondButton, secondButton, secondButton);
        var temporaryRef = RunSurfaceLifecycle.GetInteractionRef(registry, secondButton);
        Assert.NotEqual(originalRef, temporaryRef);

        RunSurfaceLifecycle.RegisterRestSiteInteraction(registry, secondButton, option);
        var reboundRef = RunSurfaceLifecycle.GetInteractionRef(registry, secondButton);

        Assert.Equal(originalRef, reboundRef);
        Assert.False(registry.TryResolve<RunSurfaceLifecycle.NativeInteractionBinding>(temporaryRef, out _));
        Assert.True(registry.TryResolve<RunSurfaceLifecycle.NativeInteractionBinding>(reboundRef, out var binding));
        Assert.NotNull(binding);
        Assert.Same(secondButton, binding.Control);
        Assert.Same(secondButton, binding.Owner);
        Assert.Same(option, binding.Subject);
        Assert.Throws<IdentityUnregisteredException>(() => RunSurfaceLifecycle.GetInteractionRef(registry, firstButton));

        // Entering a new node allocates a fresh ref rather than reusing the previous node's binding.
        registry.EnterNode(new object());
        var nextNodeButton = new object();
        registry.Register(option, PublicObjectKind.Option);
        RunSurfaceLifecycle.RegisterRestSiteInteraction(registry, nextNodeButton, option);
        var nextNodeRef = RunSurfaceLifecycle.GetInteractionRef(registry, nextNodeButton);

        Assert.NotEqual(originalRef, nextNodeRef);
        Assert.StartsWith("interaction:2:", nextNodeRef, StringComparison.Ordinal);
    }

    [Fact]
    public void RunSurfaceLifecycle_RegisterRestSiteInteraction_ClearsRetiredOptionTrackingOnReassignment()
    {
        var registry = new NodeObjectRegistry();
        registry.EnterNode(new object());

        var optionA = (RestSiteOption)RuntimeHelpers.GetUninitializedObject(typeof(HealRestSiteOption));
        var optionB = (RestSiteOption)RuntimeHelpers.GetUninitializedObject(typeof(SmithRestSiteOption));
        var sharedButton = new object();
        var replacementButton = new object();

        registry.Register(optionA, PublicObjectKind.Option);
        registry.Register(optionB, PublicObjectKind.Option);

        RunSurfaceLifecycle.RegisterRestSiteInteraction(registry, sharedButton, optionA);
        var refA1 = RunSurfaceLifecycle.GetInteractionRef(registry, sharedButton);

        // Reassigning the same control to another option retires optionA's binding and clears its tracking entry.
        RunSurfaceLifecycle.RegisterRestSiteInteraction(registry, sharedButton, optionB);
        var refB = RunSurfaceLifecycle.GetInteractionRef(registry, sharedButton);
        Assert.NotEqual(refA1, refB);
        Assert.False(registry.TryResolve<RunSurfaceLifecycle.NativeInteractionBinding>(refA1, out _));

        // Re-registering optionA on a fresh button allocates a new ref without throwing on the retired binding.
        RunSurfaceLifecycle.RegisterRestSiteInteraction(registry, replacementButton, optionA);
        var refA2 = RunSurfaceLifecycle.GetInteractionRef(registry, replacementButton);
        Assert.NotEqual(refA1, refA2);
        Assert.NotEqual(refB, refA2);
    }
}
