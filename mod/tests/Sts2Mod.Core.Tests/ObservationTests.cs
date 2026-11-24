using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Observation;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class ObservationTests
{
    private static CardEntry SampleCard(string id, string name, int cost = 1, bool upgradable = true) =>
        new(
            id,
            name,
            upgrades: 0,
            cardType: "attack",
            cardPool: "CARD_POOL.IRONCLAD",
            rarity: "starter",
            isUpgradable: upgradable,
            isRemovable: true,
            keywords: [],
            shouldRetainThisTurn: false,
            exhaustOnNextPlay: false,
            hasTurnEndInHandEffect: false,
            enchantment: null,
            affliction: null,
            cost: new ResourceCosts(ResourceCost.FromNative(cost, x: false), ResourceCost.FromNative(-1, x: false)));

    private static NativeReadPublication SampleRunPublication(RunScreen screen = RunScreen.Rewards)
    {
        var card = SampleCard("CARD.STRIKE_RED", "[gold]Strike[/gold]");
        var upgraded = new CardDefinitionPreview(SampleCard("CARD.STRIKE_RED", "Strike+", upgradable: false), "Deal [blue]9[/blue] damage.");
        var objects = ImmutableDictionary<string, ObservedObject>.Empty
            .Add("card:1:1", card)
            .Add("card:1:2", SampleCard("CARD.POMMEL_STRIKE", "Pommel Strike"))
            .Add("bundle:1:1", new BundleEntry("Starter Pack", ["card:1:2"]))
            .Add("offer:1:1", new OfferEntry("Card Reward"))
            .Add("relic:1:1", new RelicEntry("RELIC.BURNING_BLOOD", "Burning Blood", "normal", false, false, 1))
            .Add("option:1:1", new OptionEntry("[green]Rest[/green]") { DefinitionId = "REST.HEAL" });

        var descriptions = ImmutableDictionary<string, string?>.Empty
            .Add("card:1:1", "Gain [img]res://images/energy_red.png[/img][img]res://images/energy_red.png[/img].")
            .Add("card:1:2", "Deal 9 damage.")
            .Add("relic:1:1", "At the end of combat, heal 6 HP.")
            .Add("option:1:1", "Heal 30% HP.");

        var state = new RunStateView(
            PublicInputControl.Ready,
            new PublicProgress(1, 3, 2, "map:0:1", "monster", "Underdocks", "ACT.UNDERDOCKS") { BossesLeft = 1 },
            new RunPlayer("Ironclad", "CHARACTER.IRONCLAD", 72, 80, 120, 3, 2, 5, 3, 0),
            Relics: ["relic:1:1"],
            Potions: [],
            Actions:
            [
                new PublicInteraction("interaction:1:1", "[b]Claim[/b]", true) { Subject = "offer:1:1" },
                new PublicInteraction("interaction:1:2", "Rest", true) { Subject = "option:1:1" },
            ],
            Selection: null,
            ObservationPhase.Run,
            screen,
            Deck: ["card:1:1"],
            Offers:
            [
                new OfferView("offer:1:1", OfferKind.Bundle, ["bundle:1:1"], ["interaction:1:1"]),
            ])
        {
            CardRarityOffset = -0.05f,
            BattleTurns = 3,
        };

        var map = new PublicMap(
            [new NamedDefinition("The Kin", "ENCOUNTER.THE_KIN")],
            [new PublicMapNode("map:0:1", "monster", ["map:0:2"])])
        {
            LegalDestinations = ["map:0:2"],
            ElitePool = [new NamedDefinition("Bygone Effigy", "ENCOUNTER.BYGONE_EFFIGY_ELITE")],
        };

        var observation = new NativeObservation(
            new ObservationStamp("game-1", 7),
            new ObservationLocation("game-1", "run-1", "node-1", ObservationOwner.Run),
            "eng",
            NativeInputReadiness.Ready,
            state,
            objects,
            descriptions,
            ImmutableDictionary<string, CardModifierDescriptions>.Empty
                .Add("card:1:1", new CardModifierDescriptions("[b]Sharp[/b]", null)),
            ImmutableDictionary<string, UpgradePreview>.Empty,
            ImmutableDictionary<string, ImmutableArray<GeneratedCard>>.Empty,
            [new MechanismView(MechanismKind.Relic, "RELIC.BURNING_BLOOD", "Burning Blood", "Heal [gold]6[/gold] HP.", "relic:1:1")],
            new RunRules(0, []))
        {
            Map = map,
        };

        return new NativeReadPublication(
            new CompletedStateRead(observation),
            History: null,
            Deck: ["card:1:1"])
        {
            InspectionUpgrades = ImmutableDictionary<string, UpgradePreview>.Empty
                .Add("card:1:1", new AvailableUpgradePreview(upgraded))
                .Add("card:1:2", new NotUpgradablePreview()),
            CheckpointIdentities = new CheckpointIdentityFacts([], [], [], [], []),
        };
    }

    private static NativeReadPublication SampleCombatPublication()
    {
        var strike = SampleCard("CARD.STRIKE_RED", "Strike");
        var objects = ImmutableDictionary<string, ObservedObject>.Empty
            .Add("card:2:1", strike)
            .Add("enemy:2:1", new EnemyEntry(
                "Jaw Worm",
                "MONSTER.JAW_WORM",
                40,
                42,
                0,
                HpDisplay.Numeric,
                [new PublicPower("Strength", "POWER.STRENGTH", 3m, 3, "buff")],
                [new PublicIntent("attack") { DamagePerHit = 11, Hits = 1 }]));

        var state = new CombatStateView(
            PublicInputControl.Ready,
            new PublicProgress(1, 3, 3, "map:0:2", "monster", "Underdocks", "ACT.UNDERDOCKS"),
            new CombatPlayer(
                "Ironclad",
                "CHARACTER.IRONCLAD",
                65,
                80,
                120,
                3,
                3,
                Block: 5,
                new ResourceBalances(3, 0),
                [new PublicPower("Dexterity", "POWER.DEXTERITY", 1m, 1, "buff")]),
            Relics: [],
            Potions: [],
            Actions: [],
            Selection: new SelectionView("Choose 1 card", null, 1, 1, ["card:2:1"], []),
            ObservationPhase.Combat,
            CombatScreen.CardSelection,
            Turn: 2,
            Deck: ["card:1:1"],
            new PileMembership(["card:2:1"], [], [], [], []),
            [new EnemyView("enemy:2:1", "Jaw Worm", "MONSTER.JAW_WORM", 40, 42, 0, HpDisplay.Numeric, [], [])],
            []);

        var observation = new NativeObservation(
            new ObservationStamp("game-1", 12),
            new ObservationLocation("game-1", "run-1", "node-2", ObservationOwner.Combat),
            "eng",
            NativeInputReadiness.Selection,
            state,
            objects,
            ImmutableDictionary<string, string?>.Empty.Add("card:2:1", "Deal 6 damage."),
            ImmutableDictionary<string, CardModifierDescriptions>.Empty,
            ImmutableDictionary<string, UpgradePreview>.Empty,
            ImmutableDictionary<string, ImmutableArray<GeneratedCard>>.Empty,
            [
                new MechanismView(MechanismKind.Power, "POWER.DEXTERITY", "Dexterity", "Increases Block.", "player"),
                new MechanismView(MechanismKind.Power, "POWER.STRENGTH", "Strength", "Increases Attack.", "enemy:2:1"),
            ],
            new RunRules(0, []))
        {
            Encounter = new NamedDefinition("Jaw Worm", "ENCOUNTER.JAW_WORM"),
        };

        var history = new HistoryInspectionData(
            [new NamedDefinition("Strike", "CARD.STRIKE_RED")],
            [new NamedDefinition("Strike", "CARD.STRIKE_RED")],
            []);

        return new NativeReadPublication(
            new CompletedStateRead(observation),
            History: history,
            Deck: ["card:1:1"]);
    }

    [Fact]
    public void ReadAndInspect_StripFormattingExpandOffersAndServeUpgrades()
    {
        var publication = SampleRunPublication();
        var stateJson = ObservationReader.Read(publication);
        using var stateDoc = JsonDocument.Parse(stateJson);
        Assert.Equal("completed", stateDoc.RootElement.GetProperty("status").GetString());
        Assert.Equal("Strike", stateDoc.RootElement.GetProperty("observation").GetProperty("objects").GetProperty("card:1:1").GetProperty("name").GetString());
        Assert.Equal("Gain 2 energy.", stateDoc.RootElement.GetProperty("observation").GetProperty("descriptions").GetProperty("card:1:1").GetString());

        var (mapCode, mapJson) = ObservationReader.Inspect(publication, "{\"query\":{\"area\":\"map\"}}");
        Assert.Equal(200, mapCode);
        using var mapDoc = JsonDocument.Parse(mapJson);
        Assert.Equal("ENCOUNTER.THE_KIN", mapDoc.RootElement.GetProperty("data").GetProperty("map").GetProperty("bosses")[0].GetProperty("definition_id").GetString());
        Assert.Equal("ENCOUNTER.BYGONE_EFFIGY_ELITE", mapDoc.RootElement.GetProperty("data").GetProperty("map").GetProperty("elite_pool")[0].GetProperty("definition_id").GetString());

        var (rewardCode, rewardJson) = ObservationReader.Inspect(
            publication,
            "{\"query\":{\"area\":\"rewards\",\"refs\":[\"offer:1:1\"],\"upgrade\":true}}");
        Assert.Equal(200, rewardCode);
        using var rewardDoc = JsonDocument.Parse(rewardJson);
        var objects = rewardDoc.RootElement.GetProperty("data").GetProperty("objects");
        Assert.True(objects.TryGetProperty("offer:1:1", out _));
        Assert.True(objects.TryGetProperty("bundle:1:1", out _));
        Assert.True(objects.TryGetProperty("card:1:2", out _));
        Assert.Equal(
            "not_upgradable",
            rewardDoc.RootElement.GetProperty("data").GetProperty("upgrades").GetProperty("card:1:2").GetProperty("status").GetString());

        var (deckCode, deckJson) = ObservationReader.Inspect(
            publication,
            "{\"query\":{\"area\":\"deck\",\"upgrade\":true}}");
        Assert.Equal(200, deckCode);
        using var deckDoc = JsonDocument.Parse(deckJson);
        var upgrade = deckDoc.RootElement.GetProperty("data").GetProperty("upgrades").GetProperty("card:1:1");
        Assert.Equal("available", upgrade.GetProperty("status").GetString());
        Assert.Equal("Deal 9 damage.", upgrade.GetProperty("card").GetProperty("text").GetString());
    }

    [Fact]
    public void Inspect_HandlesCombatAreasEventOptionsAndStructuredFailures()
    {
        var combat = SampleCombatPublication();

        var (historyCode, historyJson) = ObservationReader.Inspect(combat, "{\"query\":{\"area\":\"combat_history\"}}");
        Assert.Equal(200, historyCode);
        using var historyDoc = JsonDocument.Parse(historyJson);
        Assert.Single(historyDoc.RootElement.GetProperty("data").GetProperty("cards_played_this_turn").EnumerateArray());

        var (powersCode, powersJson) = ObservationReader.Inspect(combat, "{\"query\":{\"area\":\"powers\"}}");
        Assert.Equal(200, powersCode);
        using var powersDoc = JsonDocument.Parse(powersJson);
        Assert.Equal(2, powersDoc.RootElement.GetProperty("data").GetProperty("mechanisms").GetArrayLength());

        var (missingUpgradeCode, missingUpgradeJson) = ObservationReader.Inspect(
            combat,
            "{\"query\":{\"area\":\"options\",\"upgrade\":true}}");
        Assert.Equal(200, missingUpgradeCode);
        using var missingUpgradeDoc = JsonDocument.Parse(missingUpgradeJson);
        Assert.Equal("failed", missingUpgradeDoc.RootElement.GetProperty("status").GetString());
        Assert.Equal("read_unavailable", missingUpgradeDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var (invalidRefsCode, invalidRefsJson) = ObservationReader.Inspect(
            combat,
            "{\"query\":{\"area\":\"hand\",\"refs\":[\"card:9:9\"]}}");
        Assert.Equal(200, invalidRefsCode);
        using var invalidRefsDoc = JsonDocument.Parse(invalidRefsJson);
        Assert.Equal("invalid_refs", invalidRefsDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var restRun = SampleRunPublication(RunScreen.Rest);
        var (optionsCode, optionsJson) = ObservationReader.Inspect(restRun, "{\"query\":{\"area\":\"options\"}}");
        Assert.Equal(200, optionsCode);
        using var optionsDoc = JsonDocument.Parse(optionsJson);
        Assert.Equal("Rest", optionsDoc.RootElement.GetProperty("data").GetProperty("objects").GetProperty("option:1:1").GetProperty("name").GetString());

        var (areaCode, areaJson) = ObservationReader.Inspect(restRun, "{\"query\":{\"area\":\"hand\"}}");
        Assert.Equal(200, areaCode);
        using var areaDoc = JsonDocument.Parse(areaJson);
        Assert.Equal("area_unavailable", areaDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var noRun = new NativeReadPublication(new FailedStateRead(new ReadCause(NativeReadError.NoRun, "No run.")));
        var (noRunCode, noRunJson) = ObservationReader.Inspect(noRun, "{\"query\":{\"area\":\"map\"}}");
        Assert.Equal(200, noRunCode);
        using var noRunDoc = JsonDocument.Parse(noRunJson);
        Assert.Equal("read_unavailable", noRunDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var (badQueryCode, _) = ObservationReader.Inspect(combat, "{\"query\":{\"area\":\"powers\",\"upgrade\":true}}");
        Assert.Equal(400, badQueryCode);
    }

    [Fact]
    public void TerminalObservation_ConvertsRunAndCombatPublicationsToTerminalView()
    {
        var combat = SampleCombatPublication();
        var terminalCombat = TerminalObservation.Create(combat, RunResult.Defeat, new ObservationStamp("game-1", 13));
        var completed = Assert.IsType<CompletedStateRead>(terminalCombat.Reply);
        Assert.Null(terminalCombat.CheckpointIdentities);
        Assert.Null(completed.Observation.Location.Owner);
        Assert.Null(completed.Observation.Encounter);
        Assert.Equal(NativeInputReadiness.Terminal, completed.Observation.InputReadiness);

        var terminalView = Assert.IsType<TerminalStateView>(completed.Observation.State);
        Assert.Equal(RunResult.Defeat, terminalView.Result);
        Assert.Equal(2, terminalView.LastObservedTurn);
        Assert.Single(terminalView.Enemies!.Value);
        Assert.Equal(new[] { "card:1:1" }, terminalView.Deck.ToArray());

        Assert.Throws<InvalidOperationException>(() =>
            TerminalObservation.Create(terminalCombat, RunResult.Defeat, new ObservationStamp("game-1", 14)));
    }

    [Fact]
    public void SemanticTextLabelsOddsAndUpgradePreviewReader_BehaveDeterministically()
    {
        Assert.Equal(
            "Gain 2 \u80fd\u91cf, 3\u8f89\u661f, [lb]ok[rb], [icon:mystery].".Replace("[lb]", "[").Replace("[rb]", "]"),
            SemanticText.Plain(
                "Gain [img]res://energy_colorless.png[/img][img]res://energy_colorless.png[/img], 3[img]res://star_icon.png[/img], [lb]ok[rb], [img]res://mystery.png[/img].",
                "zhs"));
        Assert.Equal(
            "Gain 3 star.",
            SemanticText.Plain("Gain 3[img]res://star_icon.png[/img].", "eng"));

        Assert.Equal("Open shop", NativeInteractionLabels.Operation("eng", InteractionOperation.OpenShop));
        Assert.Equal("\u6253\u5f00\u5546\u5e97", NativeInteractionLabels.Operation("zhs", InteractionOperation.OpenShop));
        Assert.Equal("open_shop", NativeInteractionLabels.Operation("", InteractionOperation.OpenShop));
        Assert.Equal("sold_out", NativeInteractionLabels.SoldOut(""));
        Assert.Equal("card_removal_service", NativeInteractionLabels.CardRemovalService(""));
        Assert.Equal("map:1:2", NativeInteractionLabels.Travel("unknown", "map:1:2"));

        var odds = UnknownRoomOddsMath.Compute(0.10m, 0.05m, 0.02m, 0.03m, ["event", "monster", "shop"]);
        Assert.Equal(new[] { "monster", "shop", "event" }, odds.AllowedRoomTypes.ToArray());
        Assert.Equal(0.10m, odds.Monster);
        Assert.Equal(0m, odds.Elite);
        Assert.Equal(0.03m, odds.Shop);
        Assert.Equal(0.87m, odds.Event);

        var failures = 0;
        var reader = new UpgradePreviewReader(_ => failures++);
        Assert.IsType<NotUpgradablePreview>(reader.Read(isUpgradable: false, () => throw new InvalidOperationException()));
        Assert.IsType<UnavailableUpgradePreview>(reader.Read(isUpgradable: true, () => throw new InvalidOperationException()));
        Assert.IsType<UnavailableUpgradePreview>(reader.Read(isUpgradable: true, () => throw new InvalidOperationException()));
        Assert.Equal(1, failures);

        var preview = new CardDefinitionPreview(SampleCard("CARD.STRIKE_RED", "Strike+"), "Deal 9 damage.");
        Assert.IsType<AvailableUpgradePreview>(reader.Read(isUpgradable: true, () => preview));
        Assert.IsType<UnavailableUpgradePreview>(reader.Read(isUpgradable: true, () => throw new InvalidOperationException()));
        Assert.Equal(2, failures);
    }
}
