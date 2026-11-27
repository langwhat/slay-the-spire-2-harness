using System.Diagnostics.CodeAnalysis;
using System.Reflection;
using System.Runtime.CompilerServices;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.RestSite;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Nodes.Screens.TreasureRoomRelic;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Identity;

namespace Sts2Mod.Observation;

internal static class RunSurfaceLifecycle
{
    internal sealed class NativeInteractionBinding(object control, object owner, object subject)
    {
        public object Control { get; private set; } = control;
        public object Owner { get; private set; } = owner;
        public object Subject { get; } = subject;

        internal void RebindControl(object control, object owner)
        {
            Control = control;
            Owner = owner;
        }
    }

    internal abstract record CardChoicePresentation
    {
        public ulong PresentedAtMsec { get; } = Time.GetTicksMsec();
    }

    internal sealed record NativeCardChoice(IReadOnlyList<CardModel> Cards, bool CanSkip) : CardChoicePresentation;

    internal sealed record NativeRewardChoice(IReadOnlyList<CardCreationResult> Cards) : CardChoicePresentation;

    private sealed class InteractionRegistration(NativeInteractionBinding binding, NodeObjectRegistry registry)
    {
        public NativeInteractionBinding Binding { get; } = binding;
        public NodeObjectRegistry Registry { get; set; } = registry;
        public string NodeId { get; set; } = registry.NodeId;
    }

    private sealed class CardRewardBindingLifetime;

    private sealed class MerchantRegistration;

    private static readonly ConditionalWeakTable<object, InteractionRegistration> Interactions = new();
    private static readonly ConditionalWeakTable<RestSiteOption, InteractionRegistration> RestSiteOptionInteractions = new();
    private static readonly ConditionalWeakTable<NChooseACardSelectionScreen, NativeCardChoice> CardChoices = new();
    private static readonly ConditionalWeakTable<NCardRewardSelectionScreen, NativeRewardChoice> RewardChoices = new();
    private static readonly ConditionalWeakTable<MerchantEntry, MerchantRegistration> Merchants = new();

    private static readonly FieldInfo? SpecialCardField =
        typeof(SpecialCardReward).GetField("_card", BindingFlags.Instance | BindingFlags.NonPublic);

    private static readonly FieldInfo? CardRewardAlternativesField =
        typeof(NCardRewardSelectionScreen).GetField("_extraOptions", BindingFlags.Instance | BindingFlags.NonPublic);

    private static readonly FieldInfo? CardRewardScreenField =
        typeof(CardReward).GetField("_currentlyShownScreen", BindingFlags.Instance | BindingFlags.NonPublic);

    public static CardModel? GetSpecialRewardCard(SpecialCardReward reward) =>
        SpecialCardField?.GetValue(reward) as CardModel;

    public static NCardRewardSelectionScreen? GetCardRewardScreen(CardReward reward) =>
        CardRewardScreenField?.GetValue(reward) as NCardRewardSelectionScreen;

    public static IReadOnlyList<CardRewardAlternative> GetCardRewardAlternatives(NCardRewardSelectionScreen screen) =>
        CardRewardAlternativesField?.GetValue(screen) as IReadOnlyList<CardRewardAlternative> ?? [];

    public static string GetInteractionRef(object control) =>
        GetInteractionRef(ObservationHub.Identities?.Registry ?? throw new IdentityUnregisteredException(), control);

    internal static string GetInteractionRef(NodeObjectRegistry registry, object control) =>
        Interactions.TryGetValue(control, out var registration)
            ? registry.GetRef(registration.Binding)
            : throw new IdentityUnregisteredException();

    internal static void RegisterInteraction(NodeObjectRegistry registry, object control, object owner, object subject)
    {
        if (Interactions.TryGetValue(control, out var previous))
        {
            if (ReferenceEquals(previous.Binding.Owner, owner) && ReferenceEquals(previous.Binding.Subject, subject))
            {
                registry.Register(previous.Binding, PublicObjectKind.Interaction);
                previous.Registry = registry;
                previous.NodeId = registry.NodeId;
                return;
            }
            if (ReferenceEquals(previous.Registry, registry) && previous.NodeId == registry.NodeId)
                RetireInteractionBinding(registry, previous.Binding);
            Interactions.Remove(control);
        }

        var binding = new NativeInteractionBinding(control, owner, subject);
        registry.Register(binding, PublicObjectKind.Interaction);
        Interactions.Add(control, new InteractionRegistration(binding, registry));
    }

    private static void RetireInteractionBinding(NodeObjectRegistry registry, NativeInteractionBinding binding)
    {
        registry.Retire(binding);
        if (binding.Subject is RestSiteOption retiredOption
            && RestSiteOptionInteractions.TryGetValue(retiredOption, out var tracked)
            && ReferenceEquals(tracked.Binding, binding))
        {
            RestSiteOptionInteractions.Remove(retiredOption);
        }
    }

    internal static void RegisterRestSiteInteraction(NodeObjectRegistry registry, object button, RestSiteOption option)
    {
        if (RestSiteOptionInteractions.TryGetValue(option, out var existing)
            && ReferenceEquals(existing.Registry, registry)
            && existing.NodeId == registry.NodeId)
        {
            if (Interactions.TryGetValue(button, out var previousButton)
                && !ReferenceEquals(previousButton.Binding, existing.Binding))
            {
                if (ReferenceEquals(previousButton.Registry, registry) && previousButton.NodeId == registry.NodeId)
                    RetireInteractionBinding(registry, previousButton.Binding);
                Interactions.Remove(button);
            }

            if (!ReferenceEquals(existing.Binding.Control, button))
            {
                Interactions.Remove(existing.Binding.Control);
                existing.Binding.RebindControl(button, button);
            }

            Interactions.Remove(button);
            Interactions.Add(button, existing);
            registry.Register(existing.Binding, PublicObjectKind.Interaction);
            return;
        }

        RegisterInteraction(registry, button, button, option);
        if (Interactions.TryGetValue(button, out var created))
        {
            RestSiteOptionInteractions.Remove(option);
            RestSiteOptionInteractions.Add(option, created);
        }
    }

    internal static RelicModel? GetInitializedTreasureRelic(NTreasureRoomRelicHolder holder) =>
        Interactions.TryGetValue(holder, out var registration) ? registration.Binding.Subject as RelicModel : null;

    internal static void RegisterTreasureRelic(NTreasureRoomRelicHolder holder, RelicModel relic)
    {
        if (ObservationHub.Identities?.Registry is not { } registry)
            return;
        RegisterInteraction(registry, holder, holder, relic);
        registry.Register(relic, PublicObjectKind.Relic);
    }

    public static bool TryGetRewardChoice(
        NCardRewardSelectionScreen screen,
        [NotNullWhen(true)] out NativeRewardChoice? choice) =>
        RewardChoices.TryGetValue(screen, out choice);

    public static bool TryGetCardChoice(
        NChooseACardSelectionScreen screen,
        [NotNullWhen(true)] out NativeCardChoice? choice) =>
        CardChoices.TryGetValue(screen, out choice);

    public static void RegisterCardChoice(NChooseACardSelectionScreen screen, IReadOnlyList<CardModel> cards, bool canSkip)
    {
        CardChoices.Remove(screen);
        CardChoices.Add(screen, new NativeCardChoice(cards, canSkip));
        if (ObservationHub.Identities?.Registry is { } registry)
        {
            foreach (var card in cards)
                registry.Register(card, PublicObjectKind.Card);
        }
        RegisterSurface(screen);
    }

    public static void RegisterCardRewardPresentation(NCardRewardSelectionScreen screen, IReadOnlyList<CardCreationResult> cards)
    {
        RewardChoices.Remove(screen);
        RewardChoices.Add(screen, new NativeRewardChoice(cards));
        RegisterCardRewards(cards);
        RegisterSurface(screen);
        if (ObservationHub.Identities?.Registry is not { } registry)
            return;
        var lifetime = new CardRewardBindingLifetime();
        RegisterAlternatives(screen, screen, lifetime, registry);
    }

    public static void RegisterCurrentSurfaces()
    {
        if (Engine.GetMainLoop() is not SceneTree tree)
            return;
        if (tree.Root.GetNodeOrNull<Node>("/root/Game/RootSceneContainer/Run") is { } run)
            RegisterSurface(run);
    }

    public static void RegisterSurface(Node? surface)
    {
        if (surface is null || surface.IsQueuedForDeletion() || ObservationHub.Identities?.Registry is not { } registry)
            return;
        RegisterNode(surface, registry);
        foreach (var child in surface.GetChildren())
            RegisterSurface(child);
    }

    public static void RegisterRewards(RewardsSet rewards)
    {
        if (ObservationHub.Identities?.Registry is not { } registry)
            return;
        foreach (var reward in rewards.Rewards)
            RegisterReward(reward, registry);
    }

    public static void RegisterMerchantEntry(MerchantEntry entry, NodeObjectRegistry registry)
    {
        registry.Register(entry, PublicObjectKind.Offer);
        if (!Merchants.TryGetValue(entry, out _))
        {
            Merchants.Add(entry, new MerchantRegistration());
            entry.EntryUpdated += () =>
            {
                if (ObservationHub.Identities?.Registry is not { } current)
                    return;
                RegisterMerchantEntry(entry, current);
                RegisterCurrentSurfaces();
            };
        }

        if (!entry.IsStocked)
            return;

        switch (entry)
        {
            case MerchantCardEntry card when card.CreationResult?.Card is { } model:
                registry.Register(model, PublicObjectKind.Card);
                break;
            case MerchantRelicEntry relic when relic.Model is { } model:
                registry.Register(model, PublicObjectKind.Relic);
                break;
            case MerchantPotionEntry potion when potion.Model is { } model:
                registry.Register(model, PublicObjectKind.Potion);
                break;
        }
    }

    public static void RegisterCardRewards(IReadOnlyList<CardCreationResult> cards)
    {
        if (ObservationHub.Identities?.Registry is not { } registry)
            return;
        foreach (var result in cards)
            registry.Register(result.Card, PublicObjectKind.Card);
    }

    public static void RegisterEvent(EventModel model)
    {
        if (ObservationHub.Identities?.Registry is not { } registry)
            return;
        registry.Register(model, PublicObjectKind.Event);
        foreach (var option in model.CurrentOptions)
            registry.Register(option, PublicObjectKind.Option);
    }

    private static void EnsureInteraction(NodeObjectRegistry registry, object control)
    {
        if (Interactions.TryGetValue(control, out var registration))
        {
            registry.Register(registration.Binding, PublicObjectKind.Interaction);
            registration.Registry = registry;
            registration.NodeId = registry.NodeId;
            return;
        }
        RegisterInteraction(registry, control, control, control);
    }

    private static void RegisterAlternatives(
        Node node,
        NCardRewardSelectionScreen screen,
        CardRewardBindingLifetime lifetime,
        NodeObjectRegistry registry)
    {
        if (node is NCardRewardAlternativeButton button)
            RegisterInteraction(registry, button, screen, lifetime);
        foreach (var child in node.GetChildren())
            RegisterAlternatives(child, screen, lifetime, registry);
    }

    private static void RegisterNode(Node node, NodeObjectRegistry registry)
    {
        if (node is NRestSiteButton && node.IsQueuedForDeletion())
            return;

        if (node is NClickableControl clickable && node is not NRestSiteButton { Option: not null })
            EnsureInteraction(registry, clickable);

        switch (node)
        {
            case NCardGridSelectionScreen grid when
                AccessTools.Field(typeof(NCardGridSelectionScreen), "_cards")?.GetValue(grid) is IReadOnlyList<CardModel> candidates:
                foreach (var card in candidates)
                    registry.Register(card, PublicObjectKind.Card);
                break;
            case NEventRoom:
                var events = RunManager.Instance.EventSynchronizer;
                if (events.Events.Count > 0)
                    RegisterEvent(events.GetLocalEvent());
                break;
            case NCardRewardSelectionScreen screen:
                registry.Register(screen, PublicObjectKind.Offer);
                break;
            case NCardBundle bundle when bundle.Bundle is { } cards:
                RegisterInteraction(registry, bundle, bundle, cards);
                registry.Register(cards, PublicObjectKind.Bundle);
                foreach (var card in cards)
                    registry.Register(card, PublicObjectKind.Card);
                if (bundle.Hitbox is { } bundleHitbox)
                    RegisterInteraction(registry, bundleHitbox, bundle, cards);
                break;
            case NRewardButton button when button.Reward is { } reward:
                RegisterReward(reward, registry);
                RegisterInteraction(registry, button, button, reward);
                break;
            case NLinkedRewardSet linked when linked.LinkedRewardSet is { } linkedSet:
                foreach (var reward in linkedSet.Rewards)
                    RegisterReward(reward, registry);
                break;
            case NMerchantInventory inventory when inventory.Inventory is { } model:
                foreach (var entry in model.AllEntries)
                    RegisterMerchantEntry(entry, registry);
                break;
            case NMerchantSlot slot when slot.Entry is { } entry:
                RegisterMerchantEntry(entry, registry);
                if (slot.Hitbox is { } hitbox)
                    RegisterInteraction(registry, hitbox, entry, MerchantSubject(entry));
                break;
            case NEventOptionButton button when button.Event is { } model && button.Option is { } option:
                registry.Register(model, PublicObjectKind.Event);
                registry.Register(option, PublicObjectKind.Option);
                RegisterInteraction(registry, button, model, option);
                break;
            case NRestSiteButton button when button.Option is { } option:
                registry.Register(option, PublicObjectKind.Option);
                RegisterRestSiteInteraction(registry, button, option);
                break;
            case NCardHolder holder when holder.CardModel is { } card:
                RegisterInteraction(registry, holder, holder, card);
                registry.Register(card, PublicObjectKind.Card);
                if (holder.Hitbox is { } cardHitbox)
                    RegisterInteraction(registry, cardHitbox, holder, card);
                break;
            case NTreasureRoomRelicHolder holder when GetInitializedTreasureRelic(holder) is { } relic:
                registry.Register(relic, PublicObjectKind.Relic);
                break;
            case NCrystalSphereCell cell when cell.Entity is { } entity:
                RegisterInteraction(registry, cell, cell, entity);
                break;
            case NMapPoint point:
                RegisterInteraction(registry, point, point, point.Point);
                break;
        }
    }

    private static void RegisterReward(Reward reward, NodeObjectRegistry registry)
    {
        if (reward is LinkedRewardSet linked)
        {
            foreach (var child in linked.Rewards)
                RegisterReward(child, registry);
            return;
        }

        registry.Register(reward, PublicObjectKind.Offer);
        if (!reward.IsPopulated || reward.SuccessfullySelected)
            return;

        switch (reward)
        {
            case SpecialCardReward special when GetSpecialRewardCard(special) is { } card:
                registry.Register(card, PublicObjectKind.Card);
                break;
            case CardReward cards:
                foreach (var card in cards.Cards)
                    registry.Register(card, PublicObjectKind.Card);
                break;
            case RelicReward relic when relic.Relic is { } model:
                registry.Register(model, PublicObjectKind.Relic);
                break;
            case PotionReward potion when potion.Potion is { } model:
                registry.Register(model, PublicObjectKind.Potion);
                break;
        }
    }

    internal static object MerchantSubject(MerchantEntry entry) => entry switch
    {
        MerchantCardEntry card when card.CreationResult is { } creation => creation,
        MerchantRelicEntry relic when relic.Model is { } model => model,
        MerchantPotionEntry potion when potion.Model is { } model => model,
        _ => entry,
    };
}

[HarmonyPatch(typeof(NRewardsScreen), nameof(NRewardsScreen.ShowScreen))]
internal static class RunRewardsRegistrationPatch
{
    private static void Postfix(RewardsSet __0, NRewardsScreen __result)
    {
        RunSurfaceLifecycle.RegisterRewards(__0);
        RunSurfaceLifecycle.RegisterSurface(__result);
    }
}

[HarmonyPatch(typeof(NCardRewardSelectionScreen), nameof(NCardRewardSelectionScreen.RefreshOptions))]
internal static class RunCardRewardRegistrationPatch
{
    private static void Postfix(NCardRewardSelectionScreen __instance, IReadOnlyList<CardCreationResult> __0) =>
        RunSurfaceLifecycle.RegisterCardRewardPresentation(__instance, __0);
}

[HarmonyPatch(typeof(CardReward), "OnRelicObtained")]
internal static class RunCardRewardRelicRegistrationPatch
{
    private static void Postfix(CardReward __instance)
    {
        if (ObservationHub.Identities?.Registry is not { } registry)
            return;
        foreach (var card in __instance.Cards)
            registry.Register(card, PublicObjectKind.Card);
    }
}

[HarmonyPatch(typeof(NChooseABundleSelectionScreen), nameof(NChooseABundleSelectionScreen.ShowScreen))]
internal static class RunBundleRegistrationPatch
{
    private static void Postfix(NChooseABundleSelectionScreen? __result) =>
        RunSurfaceLifecycle.RegisterSurface(__result);
}

[HarmonyPatch]
internal static class RunSurfaceReadyRegistrationPatch
{
    private static IEnumerable<MethodBase> TargetMethods()
    {
        Type[] types =
        [
            typeof(NRewardsScreen),
            typeof(NCardRewardSelectionScreen),
            typeof(NChooseACardSelectionScreen),
            typeof(NDeckCardSelectScreen),
            typeof(NSimpleCardSelectScreen),
            typeof(NDeckUpgradeSelectScreen),
            typeof(NDeckTransformSelectScreen),
            typeof(NDeckEnchantSelectScreen),
            typeof(NCombatPileCardSelectScreen),
            typeof(NEventOptionButton),
            typeof(NRestSiteButton),
            typeof(NRewardButton),
            typeof(NLinkedRewardSet),
            typeof(NMerchantInventory),
            typeof(NMerchantRoom),
            typeof(NCardBundle),
            typeof(NRestSiteRoom),
            typeof(NTreasureRoom),
            typeof(NEventRoom),
            typeof(NMapPoint),
            typeof(NCrystalSphereScreen),
            typeof(NCrystalSphereCell),
        ];
        return types.Select(type => AccessTools.DeclaredMethod(type, "_Ready")
            ?? throw new MissingMethodException(type.FullName, "_Ready"));
    }

    private static void Postfix(Node __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NMerchantInventory), nameof(NMerchantInventory.Open))]
internal static class RunMerchantOpenRegistrationPatch
{
    private static void Postfix(NMerchantInventory __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NMapScreen), nameof(NMapScreen.Open))]
internal static class RunMapOpenRegistrationPatch
{
    private static void Postfix(NMapScreen __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NTreasureRoomRelicHolder), nameof(NTreasureRoomRelicHolder.Initialize))]
internal static class RunTreasureRelicRegistrationPatch
{
    private static void Postfix(NTreasureRoomRelicHolder __instance, RelicModel __0) =>
        RunSurfaceLifecycle.RegisterTreasureRelic(__instance, __0);
}

[HarmonyPatch(typeof(NTreasureRoomRelicCollection), nameof(NTreasureRoomRelicCollection.InitializeRelics))]
internal static class RunTreasureRegistrationPatch
{
    private static void Postfix(NTreasureRoomRelicCollection __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NEventLayout), nameof(NEventLayout.SetEvent))]
internal static class RunEventRegistrationPatch
{
    private static void Postfix(EventModel __0) =>
        RunSurfaceLifecycle.RegisterEvent(__0);
}

[HarmonyPatch(typeof(MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer), nameof(MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer.BeginEvent))]
internal static class RunEventBeginRegistrationPatch
{
    private static void Postfix(MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer __instance)
    {
        if (__instance.Events.Count > 0)
            RunSurfaceLifecycle.RegisterEvent(__instance.GetLocalEvent());
    }
}

[HarmonyPatch(typeof(MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer), nameof(MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer.ResumeEvents))]
internal static class RunEventResumeRegistrationPatch
{
    private static void Postfix(MegaCrit.Sts2.Core.Multiplayer.Game.EventSynchronizer __instance)
    {
        if (__instance.Events.Count > 0)
            RunSurfaceLifecycle.RegisterEvent(__instance.GetLocalEvent());
    }
}

[HarmonyPatch(typeof(NEventLayout), nameof(NEventLayout.AddOptions))]
internal static class RunEventOptionsRegistrationPatch
{
    private static void Postfix(NEventLayout __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NCardHolder), nameof(NCardHolder.ReassignToCard))]
internal static class RunCardHolderRegistrationPatch
{
    private static void Postfix(NCardHolder __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NChooseACardSelectionScreen), nameof(NChooseACardSelectionScreen.ShowScreen))]
internal static class RunCardChoiceRegistrationPatch
{
    private static void Postfix(IReadOnlyList<CardModel> __0, bool __1, NChooseACardSelectionScreen? __result)
    {
        if (__result is not null)
            RunSurfaceLifecycle.RegisterCardChoice(__result, __0, __1);
    }
}

[HarmonyPatch(typeof(NMerchantInventory), nameof(NMerchantInventory.Initialize))]
internal static class RunMerchantInitializeRegistrationPatch
{
    private static void Postfix(NMerchantInventory __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NCardRewardSelectionScreen), nameof(NCardRewardSelectionScreen.ShowScreen))]
internal static class RunCardRewardOpeningRegistrationPatch
{
    private static void Postfix(IReadOnlyList<CardCreationResult> __0, NCardRewardSelectionScreen? __result)
    {
        if (__result is not null)
            RunSurfaceLifecycle.RegisterCardRewardPresentation(__result, __0);
    }
}

[HarmonyPatch(typeof(NMapScreen), nameof(NMapScreen.SetMap))]
internal static class RunMapSetRegistrationPatch
{
    private static void Postfix(NMapScreen __instance) =>
        RunSurfaceLifecycle.RegisterSurface(__instance);
}

[HarmonyPatch(typeof(NCrystalSphereScreen), nameof(NCrystalSphereScreen.ShowScreen))]
internal static class RunCrystalSphereRegistrationPatch
{
    private static void Postfix(NCrystalSphereScreen __result) =>
        RunSurfaceLifecycle.RegisterSurface(__result);
}

[HarmonyPatch(typeof(NCrystalSphereCell), nameof(NCrystalSphereCell.Create))]
internal static class RunCrystalSphereCellRegistrationPatch
{
    private static void Postfix(NCrystalSphereCell __result) =>
        RunSurfaceLifecycle.RegisterSurface(__result);
}
