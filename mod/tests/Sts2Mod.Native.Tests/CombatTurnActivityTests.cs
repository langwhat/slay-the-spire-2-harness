using System.Collections.Immutable;
using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Combat.History;
using MegaCrit.Sts2.Core.Combat.History.Entries;
using MegaCrit.Sts2.Core.Entities.Cards;
using Sts2Mod.Core.Observation;
using Sts2Mod.Observation;
using Xunit;

namespace Sts2Mod.Native.Tests;

public sealed class CombatTurnActivityTests
{
    [Fact]
    public void NativeCombatHistoryAndLifecycleContract_MembersExistWithExpectedShapes()
    {
        var historyProp = typeof(CombatManager).GetProperty(
            nameof(CombatManager.History),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(historyProp);
        Assert.Equal(typeof(CombatHistory), historyProp.PropertyType);
        Assert.Null(historyProp.SetMethod);

        var combatSetUpEvent = typeof(CombatManager).GetEvent(
            nameof(CombatManager.CombatSetUp),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(combatSetUpEvent);

        var combatEndedEvent = typeof(CombatManager).GetEvent(
            nameof(CombatManager.CombatEnded),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(combatEndedEvent);

        var changedEvent = typeof(CombatHistory).GetEvent(
            nameof(CombatHistory.Changed),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(changedEvent);
        Assert.Equal(typeof(Action), changedEvent.EventHandlerType);

        var entriesProp = typeof(CombatHistory).GetProperty(
            nameof(CombatHistory.Entries),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(entriesProp);

        var cardPlaysFinishedProp = typeof(CombatHistory).GetProperty(
            nameof(CombatHistory.CardPlaysFinished),
            BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(cardPlaysFinishedProp);

        var clearMethod = typeof(CombatHistory).GetMethod(
            nameof(CombatHistory.Clear),
            BindingFlags.Instance | BindingFlags.Public,
            Type.EmptyTypes);
        Assert.NotNull(clearMethod);

        var happenedThisTurn = typeof(CombatHistoryEntry).GetMethod(
            nameof(CombatHistoryEntry.HappenedThisTurn),
            BindingFlags.Instance | BindingFlags.Public,
            [typeof(ICombatState)]);
        Assert.NotNull(happenedThisTurn);
        Assert.Equal(typeof(bool), happenedThisTurn.ReturnType);

        Assert.True(typeof(CombatHistoryEntry).IsAssignableFrom(typeof(CardPlayFinishedEntry)));
        Assert.True(typeof(CombatHistoryEntry).IsAssignableFrom(typeof(CardExhaustedEntry)));
        Assert.True(typeof(CombatHistoryEntry).IsAssignableFrom(typeof(CardDiscardedEntry)));

        Assert.NotNull(typeof(CardPlayFinishedEntry).GetProperty(nameof(CardPlayFinishedEntry.CardPlay)));
        Assert.NotNull(typeof(CardExhaustedEntry).GetProperty(nameof(CardExhaustedEntry.Card)));
        Assert.NotNull(typeof(CardDiscardedEntry).GetProperty(nameof(CardDiscardedEntry.Card)));
    }

    [Fact]
    public void CountTurnActivity_AggregatesCardTypesExhaustedAndDiscardedCounts()
    {
        var activity = GameObserver.CountTurnActivity(
            [
                CardType.Attack,
                CardType.Skill,
                CardType.Attack,
                CardType.Power,
                CardType.Status,
                CardType.Curse,
            ],
            cardsExhaustedThisTurn: 2,
            cardsDiscardedThisTurn: 3);

        Assert.Equal(6, activity.CardsPlayed);
        Assert.Equal(2, activity.AttacksPlayed);
        Assert.Equal(1, activity.SkillsPlayed);
        Assert.Equal(1, activity.PowersPlayed);
        Assert.Equal(2, activity.CardsExhausted);
        Assert.Equal(3, activity.CardsDiscarded);

        var empty = GameObserver.CountTurnActivity([], cardsExhaustedThisTurn: 0, cardsDiscardedThisTurn: null);
        Assert.Equal(0, empty.CardsPlayed);
        Assert.Equal(0, empty.AttacksPlayed);
        Assert.Equal(0, empty.SkillsPlayed);
        Assert.Equal(0, empty.PowersPlayed);
        Assert.Equal(0, empty.CardsExhausted);
        Assert.Null(empty.CardsDiscarded);
    }

    [Fact]
    public void BattleHistoryTracker_PreservesCompletedBattleOnlyForMatchingRunAndNode()
    {
        var tracker = new BattleHistoryTracker();

        // Without any captured combat on this node, post-combat RunStateView omits battle_cards_played.
        Assert.Null(tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-2", battleTurns: 1));

        // Combat begins with zero cards played; a zero-card victory (e.g. potion/relic) yields an empty array.
        tracker.BeginCombat(runInstanceId: 1, nodeId: "node-2");
        var zeroCardWin = tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-2", battleTurns: 1);
        Assert.NotNull(zeroCardWin);
        Assert.Empty(zeroCardWin.Value);

        // Recording played cards (including a Turn-1 lethal play) persists for the matching completed battle.
        ImmutableArray<NamedDefinition> played =
        [
            new("Bash", "CARD.BASH"),
            new("Strike", "CARD.STRIKE_RED"),
        ];
        tracker.Record(runInstanceId: 1, nodeId: "node-2", played);

        Assert.Null(tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-2", battleTurns: null));
        Assert.Null(tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-2", battleTurns: 0));

        var resolved = tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-2", battleTurns: 1);
        Assert.NotNull(resolved);
        Assert.Equal(played, resolved.Value);

        // Entering a different node or starting a different run must never leak stale cards.
        Assert.Null(tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-3", battleTurns: 1));
        Assert.Null(tracker.ReadForCompletedBattle(runInstanceId: 2, nodeId: "node-2", battleTurns: 1));

        // Resetting on run boundary clears latched history.
        tracker.Reset();
        Assert.Null(tracker.ReadForCompletedBattle(runInstanceId: 1, nodeId: "node-2", battleTurns: 1));
    }
}
