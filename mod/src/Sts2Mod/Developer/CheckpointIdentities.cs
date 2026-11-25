using System.Collections.Immutable;
using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Multiplayer;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Relics;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Rewards;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Identity;
using Sts2Mod.Core.Observation;
using Sts2Mod.Observation;

namespace Sts2Mod.Developer;

/// <summary>
/// Captures native card, creature, relic, map, and reward bindings for <c>/operator/checkpoint/identity</c>.
/// </summary>
internal static class CheckpointIdentities
{
    private static readonly Dictionary<Type, (int? Threshold, int? Reset)> RelicCounterBounds = new();

    public static CheckpointIdentityFacts Capture(NativeObservation observation, NodeObjectRegistry registry)
    {
        var cards = ImmutableArray.CreateBuilder<CheckpointCardIdentity>();
        var creatures = ImmutableArray.CreateBuilder<CheckpointCreatureIdentity>();
        var map = ImmutableArray.CreateBuilder<CheckpointMapIdentity>();
        var relics = ImmutableArray.CreateBuilder<CheckpointRelicIdentity>();

        foreach (var (reference, entry) in observation.Objects)
        {
            switch (entry)
            {
                case CardEntry:
                    var card = Resolve<CardModel>(registry, reference);
                    cards.Add(new(reference, CombatCardId(card), DeckCard(card)));
                    break;
                case EnemyEntry or AllyEntry:
                    creatures.Add(new(reference, Resolve<Creature>(registry, reference).CombatId));
                    break;
            }
        }

        foreach (var reference in observation.State.Relics)
            relics.Add(RelicSlot(reference, Resolve<RelicModel>(registry, reference)));

        if (observation.State is RunStateView { Screen: RunScreen.Map } run)
        {
            foreach (var action in run.Actions)
            {
                var binding = Resolve<RunSurfaceLifecycle.NativeInteractionBinding>(registry, action.Ref);
                if (binding.Control is not NMapPoint point || !ReferenceEquals(binding.Subject, point.Point))
                    throw new InvalidOperationException("A published map action has no matching native map point.");
                map.Add(new(action.Ref, run.Progress.Act, point.Point.coord.row, point.Point.coord.col));
            }
        }

        var anchoredObjects = cards
            .Where(card => card.CombatCardId is not null || card.DeckCard is not null)
            .Select(card => card.Ref)
            .Concat(observation.State.Relics)
            .Concat(observation.State.Potions)
            .ToHashSet(StringComparer.Ordinal);

        return new(
            cards.ToImmutable(),
            creatures.ToImmutable(),
            map.ToImmutable(),
            relics.ToImmutable(),
            RewardCheckpointIdentities.Capture(observation, registry, anchoredObjects));
    }

    private static uint? CombatCardId(CardModel card)
    {
        if (!card.IsMutable || !NetCombatCardDb.Instance.TryGetCardId(card, out var id))
            return null;
        if (!NetCombatCardDb.Instance.TryGetCard(id, out var resolved) || !ReferenceEquals(card, resolved))
            throw new InvalidOperationException("A native combat card ID does not resolve to its published card.");
        return id;
    }

    private static CheckpointDeckCardIdentity? DeckCard(CardModel card)
    {
        if (!card.IsMutable || card.Owner is not { } owner)
            return null;
        var members = owner.Deck.Cards.Count(member => ReferenceEquals(member, card));
        if (members == 0)
            return null;
        if (members != 1)
            throw new InvalidOperationException("A published deck card is not a unique member of its native owner's deck.");

        var token = NetDeckCard.FromModel(card);
        if (!ReferenceEquals(token.ToCardModel(owner), card))
            throw new InvalidOperationException("A native deck card token does not resolve to its published card.");
        return new(owner.NetId, token.DeckIndex);
    }

    private static CheckpointRelicIdentity RelicSlot(string reference, RelicModel relic)
    {
        var owner = relic.Owner ?? throw new InvalidOperationException("A published relic has no native owner.");
        var slot = -1;
        for (var index = 0; index < owner.Relics.Count; index++)
        {
            if (!ReferenceEquals(owner.Relics[index], relic))
                continue;
            if (slot >= 0)
                throw new InvalidOperationException("A published relic is not a unique member of its native owner's relics.");
            slot = index;
        }
        if (slot < 0)
            throw new InvalidOperationException("A published relic is absent from its native owner's relics.");
        var (threshold, reset) = CounterBounds(relic);
        return new(reference, owner.NetId, slot, threshold, reset);
    }

    /// <summary>
    /// Only relics whose activation clears the counter have a fixed threshold and reset pair.
    /// Other displays can keep a remainder or change their threshold during the run.
    /// </summary>
    private static (int? Threshold, int? Reset) CounterBounds(RelicModel relic)
    {
        if (relic.IsMock || relic is not (HappyFlower or OrnamentalFan or LastingCandy or LetterOpener))
            return (null, null);

        var type = relic.GetType();
        if (RelicCounterBounds.TryGetValue(type, out var bounds))
            return bounds;
        // Checkpoint identities are published on the game thread every frame.
        bounds = ReadCounterBounds(relic);
        RelicCounterBounds.Add(type, bounds);
        return bounds;
    }

    private static (int? Threshold, int? Reset) ReadCounterBounds(RelicModel relic)
    {
        var activating = relic.GetType().GetProperty(
            "IsActivating", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        if (activating is not { PropertyType: var propertyType, SetMethod: not null } || propertyType != typeof(bool))
            return (null, null);
        try
        {
            var canonical = ModelDb.AllRelics.Single(model => model.Id == relic.Id && !model.IsMock);
            var probe = canonical.ToMutable();
            activating.SetValue(probe, false);
            var reset = probe.DisplayAmount;
            activating.SetValue(probe, true);
            var threshold = probe.DisplayAmount;
            return threshold == reset ? (null, null) : (threshold, reset);
        }
        catch (Exception error) when (error is InvalidOperationException or TargetInvocationException or NullReferenceException)
        {
            return (null, null);
        }
    }

    private static T Resolve<T>(NodeObjectRegistry registry, string reference) where T : class =>
        registry.TryResolve<T>(reference, out var instance)
            ? instance!
            : throw new InvalidOperationException($"Published reference {reference} has no native {typeof(T).Name} binding.");
}

internal static class RewardCheckpointIdentities
{
    private const BindingFlags PrivateInstance = BindingFlags.Instance | BindingFlags.NonPublic;

    private static readonly FieldInfo RewardsField =
        typeof(NRewardsScreen).GetField("_rewardsSet", PrivateInstance)
        ?? throw new MissingFieldException(typeof(NRewardsScreen).FullName, "_rewardsSet");

    private static readonly FieldInfo CardsField =
        typeof(CardReward).GetField("_cards", PrivateInstance)
        ?? throw new MissingFieldException(typeof(CardReward).FullName, "_cards");

    public static ImmutableArray<CheckpointRewardIdentity> Capture(
        NativeObservation observation,
        NodeObjectRegistry registry,
        IReadOnlySet<string> anchoredObjects)
    {
        if (observation.State is not RunStateView { Screen: RunScreen.Rewards } state
            || NOverlayStack.Instance is not { } overlays)
        {
            return [];
        }

        var screens = overlays.GetChildren().OfType<NRewardsScreen>().ToArray();
        if (screens.Length != 1)
            return [];

        var screen = screens[0];
        var choice = overlays.Peek() as NCardRewardSelectionScreen;
        if (choice is null && !ReferenceEquals(overlays.Peek(), screen))
            return [];

        var set = RewardsField.GetValue(screen) as RewardsSet
            ?? throw new InvalidOperationException("The published reward surface has no native reward set.");
        if (set.Rewards.Any(reward => !ReferenceEquals(reward.Player, set.Player))
            || set.Rewards.Distinct(ReferenceEqualityComparer.Instance).Count() != set.Rewards.Count)
        {
            throw new InvalidOperationException("The native reward set has inconsistent ownership or indices.");
        }

        var cards = set.Rewards
            .OfType<CardReward>()
            .Where(reward => reward.IsPopulated)
            .SelectMany(reward => ((IReadOnlyList<CardCreationResult>)(CardsField.GetValue(reward)
                ?? throw new InvalidOperationException("A populated native reward has no creation cards.")))
                .Select((creation, slot) => (Reward: reward, Creation: creation, Slot: slot)))
            .ToArray();

        var singleContents = set.Rewards.Select(reward => (Reward: reward, Content: reward switch
        {
            RelicReward relic => (object?)relic.Relic,
            PotionReward potion => potion.Potion,
            SpecialCardReward special => RunSurfaceLifecycle.GetSpecialRewardCard(special),
            _ => null,
        })).Where(item => item.Content is not null).ToArray();

        CardReward? parent = null;
        NCardHolder[] holders = [];
        NCardRewardAlternativeButton[] alternatives = [];
        if (choice is not null)
        {
            var parents = set.Rewards
                .OfType<CardReward>()
                .Where(reward => ReferenceEquals(RunSurfaceLifecycle.GetCardRewardScreen(reward), choice))
                .ToArray();
            if (parents.Length != 1)
                return [];

            parent = parents[0];
            if (!RunSurfaceLifecycle.TryGetRewardChoice(choice, out var presentation))
                return [];

            var original = cards.Where(item => ReferenceEquals(item.Reward, parent)).ToArray();
            if (original.Length != presentation.Cards.Count
                || original.Where((item, index) => !ReferenceEquals(item.Creation, presentation.Cards[index])).Any())
            {
                return [];
            }

            holders = choice.GetNode<Control>("UI/CardRow").GetChildren()
                .OfType<NCardHolder>()
                .Where(holder => !holder.IsQueuedForDeletion() && holder.CardModel is not null)
                .ToArray();
            if (holders.Length != original.Length
                || original.Any(item => holders.Count(holder => ReferenceEquals(holder.CardModel, item.Creation.Card)) != 1))
            {
                return [];
            }

            alternatives = choice.GetNode<Control>("UI/RewardAlternatives").GetChildren()
                .OfType<NCardRewardAlternativeButton>()
                .Where(button => !button.IsQueuedForDeletion())
                .ToArray();
        }

        var identities = ImmutableArray.CreateBuilder<CheckpointRewardIdentity>();
        foreach (var reference in observation.Objects.Keys.Concat(state.Actions.Select(action => action.Ref)).Distinct())
        {
            if (anchoredObjects.Contains(reference))
                continue;
            if (!registry.TryResolve<object>(reference, out var subject))
                throw new InvalidOperationException("A published reward reference has no native binding.");

            CheckpointRewardAddress? address = null;
            switch (subject)
            {
                case Reward reward when set.Rewards.Any(member => ReferenceEquals(member, reward)):
                    address = Address(set, reward, CheckpointRewardBindingKind.RewardObject);
                    break;
                case NCardRewardSelectionScreen selected when ReferenceEquals(selected, choice) && parent is not null:
                    address = Address(set, parent, CheckpointRewardBindingKind.ChoiceScreen);
                    break;
                case CardModel card:
                    var matches = cards.Where(item => ReferenceEquals(item.Creation.Card, card)).ToArray();
                    if (matches.Length == 1)
                        address = Address(set, matches[0].Reward, CheckpointRewardBindingKind.CreationCard, matches[0].Slot);
                    break;
                case RunSurfaceLifecycle.NativeInteractionBinding binding:
                    if (binding.Control is NRewardButton button
                        && button.Reward is { } claimed
                        && ReferenceEquals(binding.Subject, claimed)
                        && set.Rewards.Any(member => ReferenceEquals(member, claimed)))
                    {
                        address = Address(set, claimed, CheckpointRewardBindingKind.ClaimAction);
                    }
                    else if (binding.Control is NCardHolder holder
                             && parent is not null
                             && holders.Any(member => ReferenceEquals(member, holder))
                             && ReferenceEquals(binding.Subject, holder.CardModel))
                    {
                        var picked = cards.Where(item =>
                            ReferenceEquals(item.Reward, parent)
                            && ReferenceEquals(item.Creation.Card, holder.CardModel)).ToArray();
                        if (picked.Length == 1)
                            address = Address(set, parent, CheckpointRewardBindingKind.PickAction, picked[0].Slot);
                    }
                    else if (binding.Control is NCardRewardAlternativeButton alternative
                             && choice is not null
                             && parent is not null
                             && ReferenceEquals(binding.Owner, choice))
                    {
                        var options = RunSurfaceLifecycle.GetCardRewardAlternatives(choice);
                        var index = Array.IndexOf(alternatives, alternative);
                        if (index >= 0 && options.Count == alternatives.Length && !string.IsNullOrWhiteSpace(options[index].OptionId))
                            address = Address(set, parent, CheckpointRewardBindingKind.AlternativeAction, optionId: options[index].OptionId);
                    }
                    else if (choice is null
                             && binding.Control is NProceedButton proceed
                             && ReferenceEquals(screen.GetNodeOrNull<NProceedButton>("ProceedButton"), proceed)
                             && ReferenceEquals(binding.Owner, proceed)
                             && ReferenceEquals(binding.Subject, proceed)
                             && state.Actions.Any(action => action.Ref == reference && action.Operation == InteractionOperation.Proceed))
                    {
                        address = new(set.Player.NetId, null, CheckpointRewardBindingKind.ProceedAction, null, null);
                    }
                    break;
            }

            if (address is null)
            {
                var matches = singleContents.Where(item => ReferenceEquals(item.Content, subject)).ToArray();
                if (matches.Length == 1)
                    address = Address(set, matches[0].Reward, CheckpointRewardBindingKind.ContentObject);
            }
            if (address is not null)
                identities.Add(new(reference, address));
        }

        if (identities.Select(identity => identity.Binding).Distinct().Count() != identities.Count)
            throw new InvalidOperationException("Published reward addresses are not one-to-one.");
        return identities.ToImmutable();
    }

    private static CheckpointRewardAddress Address(
        RewardsSet set,
        Reward reward,
        CheckpointRewardBindingKind kind,
        int? slot = null,
        string? optionId = null) =>
        new(reward.Player.NetId, set.Rewards.IndexOf(reward), kind, slot, optionId);
}
