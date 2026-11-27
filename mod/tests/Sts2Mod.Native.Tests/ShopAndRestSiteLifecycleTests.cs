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
    public ShopAndRestSiteLifecycleTests()
    {
        NativeTestBootstrap.EnsureModelDb();
    }
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

    [Fact]
    public void RunSurfaceLifecycle_ShopCardEggUpgrade_PreservesInteractionRefWhileRestockRetiresIt()
    {
        var registry = new NodeObjectRegistry();
        registry.EnterNode(new object());

        var entry = (MerchantCardEntry)RuntimeHelpers.GetUninitializedObject(typeof(MerchantCardEntry));
        var creationField = typeof(MerchantCardEntry).GetField(
            "<CreationResult>k__BackingField",
            BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(creationField);

        var hitbox = new object();
        var baseCard = MegaCrit.Sts2.Core.Models.ModelDb.Card<MegaCrit.Sts2.Core.Models.Cards.Prepared>().ToMutable();
        var creation = new MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult(baseCard);
        creationField.SetValue(entry, creation);

        RunSurfaceLifecycle.RegisterInteraction(registry, hitbox, entry, RunSurfaceLifecycle.MerchantSubject(entry));
        var initialRef = RunSurfaceLifecycle.GetInteractionRef(registry, hitbox);

        // Egg-style in-place upgrade mutates CreationResult.Card while keeping the same CardCreationResult instance.
        var upgradedCard = MegaCrit.Sts2.Core.Models.ModelDb.Card<MegaCrit.Sts2.Core.Models.Cards.Prepared>().ToMutable();
        upgradedCard.UpgradeInternal();
        creation.ModifyCard(upgradedCard, MegaCrit.Sts2.Core.Models.ModelDb.Relic<MegaCrit.Sts2.Core.Models.Relics.ToxicEgg>());
        RunSurfaceLifecycle.RegisterInteraction(registry, hitbox, entry, RunSurfaceLifecycle.MerchantSubject(entry));
        var upgradedRef = RunSurfaceLifecycle.GetInteractionRef(registry, hitbox);
        Assert.Equal(initialRef, upgradedRef);

        // Courier-style restock replaces MerchantCardEntry.CreationResult with a new instance: retire old ref.
        var differentCard = MegaCrit.Sts2.Core.Models.ModelDb.Card<MegaCrit.Sts2.Core.Models.Cards.Backflip>().ToMutable();
        creationField.SetValue(entry, new MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult(differentCard));
        RunSurfaceLifecycle.RegisterInteraction(registry, hitbox, entry, RunSurfaceLifecycle.MerchantSubject(entry));
        var restockedRef = RunSurfaceLifecycle.GetInteractionRef(registry, hitbox);
        Assert.NotEqual(initialRef, restockedRef);
        Assert.False(registry.TryResolve<RunSurfaceLifecycle.NativeInteractionBinding>(initialRef, out _));
    }

    [Fact]
    public void GameObserver_RemoveCoveredPowerHoverTips_MatchesByExactPowerIdNotLocalizedTitle()
    {
        var mechanisms = new List<Sts2Mod.Core.Observation.MechanismView>
        {
            new(Sts2Mod.Core.Observation.MechanismKind.Other, "POWER.RINGING_POWER", "Ringing", "Type: Debuff\n...", "player"),
            new(Sts2Mod.Core.Observation.MechanismKind.Power, "POWER.RINGING_POWER", "Ringing", "Can only play 1 card.", "player"),
            new(Sts2Mod.Core.Observation.MechanismKind.Keyword, null, "Ringing", "Affliction description.", "c1"),
            new(Sts2Mod.Core.Observation.MechanismKind.Power, "POWER.TORIC_TOUGHNESS_POWER", "Toric Toughness", "Gain 5 Block.", "player"),
        };

        GameObserver.RemoveCoveredPowerHoverTips(
            mechanisms,
            new HashSet<string>(StringComparer.Ordinal) { "POWER.RINGING_POWER" });

        Assert.Equal(3, mechanisms.Count);
        Assert.Contains(mechanisms, m => m.Kind == Sts2Mod.Core.Observation.MechanismKind.Other && m.DefinitionId == "POWER.RINGING_POWER");
        Assert.DoesNotContain(mechanisms, m => m.Kind == Sts2Mod.Core.Observation.MechanismKind.Power && m.DefinitionId == "POWER.RINGING_POWER");
        Assert.Contains(mechanisms, m => m.Kind == Sts2Mod.Core.Observation.MechanismKind.Keyword && m.Name == "Ringing");
        Assert.Contains(mechanisms, m => m.Kind == Sts2Mod.Core.Observation.MechanismKind.Power && m.DefinitionId == "POWER.TORIC_TOUGHNESS_POWER");
    }

    [Fact]
    public void NativeLifecycleAndMechanismMembers_ExtractExpectedRules()
    {
        // EventSynchronizer hooks for preload registration exist with expected names.
        Assert.NotNull(typeof(EventSynchronizer).GetMethod(
            nameof(EventSynchronizer.BeginEvent),
            BindingFlags.Instance | BindingFlags.Public));
        Assert.NotNull(typeof(EventSynchronizer).GetMethod(
            nameof(EventSynchronizer.ResumeEvents),
            BindingFlags.Instance | BindingFlags.Public));

        // NRewardsScreen proceeding guard inspects both IsComplete and _disableProceedForever.
        var rewards = (MegaCrit.Sts2.Core.Nodes.Screens.NRewardsScreen)RuntimeHelpers.GetUninitializedObject(
            typeof(MegaCrit.Sts2.Core.Nodes.Screens.NRewardsScreen));
        Assert.False(GameObserver.IsRewardsScreenProceeding(rewards));
        var disableField = typeof(MegaCrit.Sts2.Core.Nodes.Screens.NRewardsScreen).GetField(
            "_disableProceedForever",
            BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(disableField);
        disableField.SetValue(rewards, true);
        Assert.True(GameObserver.IsRewardsScreenProceeding(rewards));

        // WitheringPresencePower and PanachePower link their CardsLeft counter to display_amount.
        var (witherText, witherGaps) = PowerMechanismReader.Describe(
            typeof(MegaCrit.Sts2.Core.Models.Powers.WitheringPresencePower),
            includeMultiplayerScale: false);
        Assert.Empty(witherGaps);
        Assert.NotNull(witherText);
        Assert.Contains("counter cards left starts at 6 (shown as display_amount)", witherText);

        var (panacheText, panacheGaps) = PowerMechanismReader.Describe(
            typeof(MegaCrit.Sts2.Core.Models.Powers.PanachePower),
            includeMultiplayerScale: false);
        Assert.Empty(panacheGaps);
        Assert.NotNull(panacheText);
        Assert.Contains("(shown as display_amount)", panacheText);

        // DislikeYouPower exposes its Debuff polarity so Artifact interactions are explicit.
        var (dislikeText, dislikeGaps) = PowerMechanismReader.Describe(
            typeof(MegaCrit.Sts2.Core.Models.Powers.DislikeYouPower),
            includeMultiplayerScale: false);
        Assert.Empty(dislikeGaps);
        Assert.NotNull(dislikeText);
        Assert.Contains("Type: Debuff", dislikeText);

        // Temporary 0-cost discovery potions explain single-play / end-of-turn cost reset.
        foreach (var potion in new MegaCrit.Sts2.Core.Models.PotionModel[]
        {
            MegaCrit.Sts2.Core.Models.ModelDb.Potion<MegaCrit.Sts2.Core.Models.Potions.AttackPotion>(),
            MegaCrit.Sts2.Core.Models.ModelDb.Potion<MegaCrit.Sts2.Core.Models.Potions.SkillPotion>(),
            MegaCrit.Sts2.Core.Models.ModelDb.Potion<MegaCrit.Sts2.Core.Models.Potions.PowerPotion>(),
            MegaCrit.Sts2.Core.Models.ModelDb.Potion<MegaCrit.Sts2.Core.Models.Potions.ColorlessPotion>(),
        })
        {
            var note = ItemMechanismReader.DescribePotion(potion);
            Assert.NotNull(note);
            Assert.Contains("until played or until the turn ends", note);
        }
    }
}
