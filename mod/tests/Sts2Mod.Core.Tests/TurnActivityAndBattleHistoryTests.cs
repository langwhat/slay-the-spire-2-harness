using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class TurnActivityAndBattleHistoryTests
{
    private static CombatStateView BaseCombatState() =>
        new(
            PublicInputControl.Ready,
            new PublicProgress(1, 3, 2, "map:0:1", "monster", "Underdocks", "ACT.UNDERDOCKS"),
            new CombatPlayer("Ironclad", "CHARACTER.IRONCLAD", 70, 80, 99, 3, 3, 0, new ResourceBalances(3, 0), []),
            Relics: [],
            Potions: [],
            Actions: [],
            Selection: null,
            ObservationPhase.Combat,
            CombatScreen.Combat,
            Turn: 1,
            Deck: [],
            new PileMembership([], [], [], [], []),
            Enemies: [],
            InactiveEnemies: []);

    private static RunStateView BaseRunState(int? battleTurns = null) =>
        new(
            PublicInputControl.Ready,
            new PublicProgress(1, 3, 2, "map:0:1", "monster", "Underdocks", "ACT.UNDERDOCKS"),
            new RunPlayer("Ironclad", "CHARACTER.IRONCLAD", 70, 80, 99, 3, 3, 5, 3, 0),
            Relics: [],
            Potions: [],
            Actions: [],
            Selection: null,
            ObservationPhase.Run,
            RunScreen.Rewards,
            Deck: [],
            Offers: [])
        {
            CardRarityOffset = -0.05f,
            BattleTurns = battleTurns,
        };

    [Fact]
    public void TurnActivity_SerializesSnakeCaseAndOmitsNullDiscardedCount()
    {
        var withoutDiscarded = new TurnActivity(
            CardsPlayed: 4,
            AttacksPlayed: 2,
            SkillsPlayed: 1,
            PowersPlayed: 1,
            CardsExhausted: 1);

        using var withoutDoc = JsonDocument.Parse(JsonContract.Serialize(withoutDiscarded));
        var withoutRoot = withoutDoc.RootElement;
        Assert.Equal(4, withoutRoot.GetProperty("cards_played").GetInt32());
        Assert.Equal(2, withoutRoot.GetProperty("attacks_played").GetInt32());
        Assert.Equal(1, withoutRoot.GetProperty("skills_played").GetInt32());
        Assert.Equal(1, withoutRoot.GetProperty("powers_played").GetInt32());
        Assert.Equal(1, withoutRoot.GetProperty("cards_exhausted").GetInt32());
        Assert.False(withoutRoot.TryGetProperty("cards_discarded", out _));

        var withDiscarded = withoutDiscarded with { CardsDiscarded = 2 };
        using var withDoc = JsonDocument.Parse(JsonContract.Serialize(withDiscarded));
        Assert.Equal(2, withDoc.RootElement.GetProperty("cards_discarded").GetInt32());
    }

    [Fact]
    public void CombatStateView_SerializesTurnActivityOnlyWhenPresent()
    {
        var omittedState = BaseCombatState();
        using var omittedDoc = JsonDocument.Parse(JsonContract.Serialize(omittedState));
        Assert.False(omittedDoc.RootElement.TryGetProperty("turn_activity", out _));

        var presentState = omittedState with
        {
            TurnActivity = new TurnActivity(3, 1, 2, 0, 0) { CardsDiscarded = 1 },
        };
        using var presentDoc = JsonDocument.Parse(JsonContract.Serialize(presentState));
        var activity = presentDoc.RootElement.GetProperty("turn_activity");
        Assert.Equal(3, activity.GetProperty("cards_played").GetInt32());
        Assert.Equal(1, activity.GetProperty("attacks_played").GetInt32());
        Assert.Equal(2, activity.GetProperty("skills_played").GetInt32());
        Assert.Equal(0, activity.GetProperty("powers_played").GetInt32());
        Assert.Equal(0, activity.GetProperty("cards_exhausted").GetInt32());
        Assert.Equal(1, activity.GetProperty("cards_discarded").GetInt32());
    }

    [Fact]
    public void RunStateView_SerializesBattleCardsPlayedOnlyWhenPresentAndStripsFormattingOnRead()
    {
        var omittedState = BaseRunState(battleTurns: null);
        using var omittedDoc = JsonDocument.Parse(JsonContract.Serialize(omittedState));
        Assert.False(omittedDoc.RootElement.TryGetProperty("battle_cards_played", out _));

        var emptyPlayedState = BaseRunState(battleTurns: 1) with
        {
            BattleCardsPlayed = ImmutableArray<NamedDefinition>.Empty,
        };
        using var emptyDoc = JsonDocument.Parse(JsonContract.Serialize(emptyPlayedState));
        Assert.Equal(JsonValueKind.Array, emptyDoc.RootElement.GetProperty("battle_cards_played").ValueKind);
        Assert.Empty(emptyDoc.RootElement.GetProperty("battle_cards_played").EnumerateArray());

        var populatedState = BaseRunState(battleTurns: 1) with
        {
            BattleCardsPlayed =
            [
                new NamedDefinition("[gold]Strike[/gold]", "CARD.STRIKE_RED"),
                new NamedDefinition("Bash", "CARD.BASH"),
            ],
        };
        var observation = new NativeObservation(
            new ObservationStamp("game-1", 4),
            new ObservationLocation("game-1", "run-1", "node-2", ObservationOwner.Run),
            "eng",
            NativeInputReadiness.Ready,
            populatedState,
            ImmutableDictionary<string, ObservedObject>.Empty,
            ImmutableDictionary<string, string?>.Empty,
            ImmutableDictionary<string, CardModifierDescriptions>.Empty,
            ImmutableDictionary<string, UpgradePreview>.Empty,
            ImmutableDictionary<string, ImmutableArray<GeneratedCard>>.Empty,
            [],
            new RunRules(0, []));

        var json = ObservationReader.Read(new NativeReadPublication(new CompletedStateRead(observation)));
        using var readDoc = JsonDocument.Parse(json);
        var played = readDoc.RootElement
            .GetProperty("observation")
            .GetProperty("state")
            .GetProperty("battle_cards_played");
        Assert.Equal(2, played.GetArrayLength());
        Assert.Equal("Strike", played[0].GetProperty("name").GetString());
        Assert.Equal("CARD.STRIKE_RED", played[0].GetProperty("definition_id").GetString());
        Assert.Equal("Bash", played[1].GetProperty("name").GetString());
        Assert.Equal("CARD.BASH", played[1].GetProperty("definition_id").GetString());
    }
}
