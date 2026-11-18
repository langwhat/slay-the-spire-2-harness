using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Identity;

namespace Sts2Mod.Observation;

internal sealed class IdentityLifecycle
{
    private CombatState? _creationCombat;
    private readonly List<CardModel> _createdCards = [];
    private readonly HashSet<CardModel> _knownCreatedCards = new(ReferenceEqualityComparer.Instance);
    private readonly HashSet<Player> _players = new(ReferenceEqualityComparer.Instance);
    private readonly HashSet<CardPile> _piles = new(ReferenceEqualityComparer.Instance);

    public NodeObjectRegistry Registry { get; } = new();

    public void ResetRun()
    {
        foreach (var player in _players)
        {
            player.PotionProcured -= RegisterPotion;
            player.PotionDiscarded -= RetirePotion;
            player.UsedPotionRemoved -= RetirePotion;
            player.RelicObtained -= RegisterRelic;
        }
        foreach (var pile in _piles)
            pile.CardAdded -= RegisterCard;

        _creationCombat = null;
        _createdCards.Clear();
        _knownCreatedCards.Clear();
        _players.Clear();
        _piles.Clear();
        EnterNode();
    }

    public void EnterNode()
    {
        Registry.EnterNode(new object());
        RegisterCurrent();
        RunSurfaceLifecycle.RegisterCurrentSurfaces();
    }

    public void RegisterCurrent()
    {
        var run = RunManager.Instance.DebugOnlyGetState();
        if (run is null)
            return;

        foreach (var player in run.Players)
        {
            if (_players.Add(player))
            {
                player.PotionProcured += RegisterPotion;
                player.PotionDiscarded += RetirePotion;
                player.UsedPotionRemoved += RetirePotion;
                player.RelicObtained += RegisterRelic;
            }
            if (_piles.Add(player.Deck))
                player.Deck.CardAdded += RegisterCard;
            foreach (var pile in player.Piles)
            {
                if (_piles.Add(pile))
                    pile.CardAdded += RegisterCard;
            }
            foreach (var card in player.Deck.Cards)
                RegisterCard(card);
            foreach (var potion in player.Potions)
                Registry.Register(potion, PublicObjectKind.Potion);
            foreach (var relic in player.Relics)
                Registry.Register(relic, PublicObjectKind.Relic);
        }

        var combat = CombatManager.Instance.DebugOnlyGetState();
        if (combat is null)
            return;

        RegisterCreatedCards(combat, combat.ContainsCard);
        foreach (var creature in combat.Creatures)
            RegisterCreature(creature);
    }

    public void RegisterCreature(Creature creature) =>
        Registry.Register(creature, creature.IsEnemy ? PublicObjectKind.Enemy : PublicObjectKind.Ally);

    public void RegisterCard(CardModel card) =>
        Registry.Register(card, PublicObjectKind.Card);

    public void RegisterCreatedCard(CombatState combat, CardModel card)
    {
        if (!ReferenceEquals(_creationCombat, combat))
        {
            _creationCombat = combat;
            _createdCards.Clear();
            _knownCreatedCards.Clear();
        }
        if (_knownCreatedCards.Add(card))
            _createdCards.Add(card);
        RegisterCard(card);
    }

    public void RegisterCreatedCards(CombatState combat, Func<CardModel, bool> belongsToCombat)
    {
        if (!ReferenceEquals(_creationCombat, combat))
            return;
        foreach (var card in _createdCards)
        {
            if (belongsToCombat(card))
                RegisterCard(card);
        }
    }

    private void RegisterPotion(PotionModel potion) =>
        Registry.Register(potion, PublicObjectKind.Potion);

    private void RegisterRelic(RelicModel relic) =>
        Registry.Register(relic, PublicObjectKind.Relic);

    private void RetirePotion(PotionModel potion) =>
        Registry.Retire(potion);
}

[HarmonyPatch(typeof(CombatState), nameof(CombatState.AddCard), [typeof(CardModel), typeof(Player)])]
internal static class CombatCardRegistrationPatch
{
    private static void Postfix(CombatState __instance, CardModel __0) =>
        ObservationHub.Identities?.RegisterCreatedCard(__instance, __0);
}

[HarmonyPatch(typeof(CombatState), nameof(CombatState.AddCreature), [typeof(Creature)])]
internal static class CombatCreatureRegistrationPatch
{
    private static void Postfix(Creature __0) =>
        ObservationHub.Identities?.RegisterCreature(__0);
}

[HarmonyPatch(typeof(CombatState), nameof(CombatState.CloneCard), [typeof(CardModel)])]
internal static class CombatCloneRegistrationPatch
{
    private static void Postfix(CombatState __instance, CardModel __result) =>
        ObservationHub.Identities?.RegisterCreatedCard(__instance, __result);
}
