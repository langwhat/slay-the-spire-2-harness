using System.Collections.Immutable;
using System.IO;
using System.Text.Json;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class DeveloperContractTests
{
    [Fact]
    public void HeadlessRunConfigurationMenuReadinessAndStartReceipt_ValidateStrictly()
    {
        var settled = new MainMenuReadinessObservation(
            GamePresent: true,
            MainMenuPresent: true,
            MainMenuValid: true,
            MainMenuInsideTree: true,
            MainMenuVisibleInTree: true,
            SubmenuStackPresent: true,
            SubmenusOpen: false,
            TransitionInProgress: false);
        Assert.True(MainMenuReadinessRules.IsRunStartReady(settled));
        Assert.True(MainMenuReadinessRules.IsMenuSettled(settled));
        Assert.False(MainMenuReadinessRules.IsMenuSettled(settled with { TransitionInProgress = true }));
        Assert.False(MainMenuReadinessRules.IsRunStartReady(settled with { SubmenusOpen = true }));

        var parsed = HeadlessRunConfiguration.Parse("{\"character\":\"SILENT\",\"ascension\":7,\"seed\":\"SEED123\"}");
        Assert.Equal(StandardCharacter.Silent, parsed.Character);
        Assert.Equal(7, parsed.Ascension);
        Assert.Equal("SEED123", parsed.Seed);

        var fool = HeadlessRunConfiguration.Parse("{\"character\":\"FOOL\",\"ascension\":10}");
        Assert.Equal(StandardCharacter.Fool, fool.Character);
        Assert.Equal(10, fool.Ascension);
        Assert.Equal("\"FOOL\"", JsonSerializer.Serialize(fool.Character));

        Assert.Throws<ArgumentException>(() => HeadlessRunConfiguration.Parse("{\"character\":\"WATCHER\"}"));
        Assert.Throws<ArgumentException>(() => HeadlessRunConfiguration.Parse("{\"character\":\"DEFECT\",\"ascension\":11}"));
        Assert.Throws<ArgumentException>(() => HeadlessRunConfiguration.Parse("{\"character\":\"DEFECT\",\"seed\":\"\"}"));

        var receiptJson = JsonContract.Serialize(HeadlessStartReceipt.Completed("CANONICAL99"));
        using var doc = JsonDocument.Parse(receiptJson);
        Assert.Equal("completed", doc.RootElement.GetProperty("status").GetString());
        Assert.True(doc.RootElement.GetProperty("effect_verified").GetBoolean());
        Assert.Equal(JsonValueKind.Null, doc.RootElement.GetProperty("reason_code").ValueKind);
        Assert.Equal("CANONICAL99", doc.RootElement.GetProperty("seed").GetString());
    }

    [Fact]
    public void CheckpointContractFilesStoreAndIdentityReader_EnforceInvariants()
    {
        Assert.True(CheckpointContract.IsValidCommandId("cmd_01:alpha-beta.1"));
        Assert.False(CheckpointContract.IsValidCommandId("invalid space"));

        var tempRoot = Path.Combine(Path.GetTempPath(), "sts2mod-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tempRoot);
        try
        {
            var sourceSave = Path.Combine(tempRoot, "source.save");
            File.WriteAllText(sourceSave, "{\"save\":true}");
            var captureDir = Path.Combine(tempRoot, "captured");

            var proof = CheckpointFiles.Capture(sourceSave, captureDir);
            Assert.Equal(Path.Combine(captureDir, "current_run.save"), proof.Path);
            Assert.Equal(13, proof.Bytes);
            Assert.Equal(64, proof.Sha256.Length);
            Assert.Throws<IOException>(() => CheckpointFiles.Capture(sourceSave, captureDir));

            var captureRequest = Assert.IsType<CaptureCheckpointRequest>(CheckpointContract.Decode($$"""
                {
                  "command_id": "cap-1",
                  "kind": "capture",
                  "expected_route": {
                    "game_instance_id": "game-1",
                    "run_id": "run-1",
                    "node_id": "node-1",
                    "owner": "run"
                  },
                  "output_directory": {{JsonSerializer.Serialize(Path.Combine(tempRoot, "next"))}}
                }
                """));
            Assert.Equal("cap-1", captureRequest.CommandId);

            var loadRequest = Assert.IsType<LoadCheckpointRequest>(CheckpointContract.Decode($$"""
                {
                  "command_id": "load-1",
                  "kind": "load",
                  "checkpoint_path": {{JsonSerializer.Serialize(proof.Path)}},
                  "checkpoint_sha256": "{{proof.Sha256}}",
                  "target": {
                    "canonical_seed": "SEED1",
                    "game_mode": "standard",
                    "character": "CHARACTER.SILENT",
                    "ascension": 2,
                    "act": 1,
                    "floor": 4
                  }
                }
                """));
            Assert.Equal(proof.Sha256, loadRequest.CheckpointSha256);
            Assert.Equal(64, CheckpointContract.Fingerprint(loadRequest).Length);

            var store = new CheckpointCommandStore(capacity: 2);
            var (first, added) = store.ReservePending("c1", CheckpointKind.Capture, "fp1");
            Assert.True(added);
            Assert.False(store.ReservePending("c1", CheckpointKind.Capture, "fp1").Added);
            Assert.True(store.HasNonTerminalOtherThan("c2"));

            store.MarkDispatched("c1");
            Assert.True(Assert.IsType<PendingCheckpoint>(store.Get("c1")!.Reply).NativeDispatched);

            store.ReservePending("c2", CheckpointKind.Load, "fp2");
            store.Settle("c1", new RejectedCheckpoint("c1", new CheckpointCause(CheckpointErrorCode.Conflict, "Done.")));
            store.ReservePending("c3", CheckpointKind.Status, "fp3");
            Assert.Null(store.Get("c1"));
            Assert.NotNull(store.Get("c2"));
            Assert.NotNull(store.Get("c3"));
        }
        finally
        {
            if (Directory.Exists(tempRoot))
                Directory.Delete(tempRoot, recursive: true);
        }

        var noRunPub = new NativeReadPublication(new FailedStateRead(new ReadCause(NativeReadError.NoRun, "None.")));
        Assert.IsType<FailedCheckpointIdentityRead>(CheckpointIdentityReader.Read(noRunPub));

        var formattedRun = new RunStateView(
            PublicInputControl.Ready,
            new PublicProgress(1, 3, 1, "map:0:0", "monster", "[gold]Underdocks[/gold]", "ACT.UNDERDOCKS"),
            new RunPlayer("Ironclad", "CHARACTER.IRONCLAD", 80, 80, 99, 3, 3, 5, 3, 0),
            [],
            [],
            [],
            null,
            ObservationPhase.Run,
            RunScreen.Map,
            [],
            [])
        {
            CardRarityOffset = -0.05f,
        };
        var identityPub = new NativeReadPublication(new CompletedStateRead(new NativeObservation(
            new ObservationStamp("game-1", 1),
            new ObservationLocation("game-1", "run-1", "node-1", ObservationOwner.Run),
            "eng",
            NativeInputReadiness.Ready,
            formattedRun,
            ImmutableDictionary<string, ObservedObject>.Empty,
            ImmutableDictionary<string, string?>.Empty.Add("card:1:1", "Gain [img]res://energy_red.png[/img]."),
            ImmutableDictionary<string, CardModifierDescriptions>.Empty,
            ImmutableDictionary<string, UpgradePreview>.Empty,
            ImmutableDictionary<string, ImmutableArray<GeneratedCard>>.Empty,
            [],
            new RunRules(0, []))))
        {
            CheckpointIdentities = new CheckpointIdentityFacts([], [], [], [], []),
        };
        using var identityDoc = JsonDocument.Parse(CheckpointIdentityReader.ReadJson(identityPub));
        Assert.Equal(
            "Underdocks",
            identityDoc.RootElement.GetProperty("observation").GetProperty("state").GetProperty("progress").GetProperty("name").GetString());
        Assert.Equal(
            "Gain 1 energy.",
            identityDoc.RootElement.GetProperty("observation").GetProperty("descriptions").GetProperty("card:1:1").GetString());
    }

    [Fact]
    public void CombatEvalScenarioAndReplaySettings_ProduceVerifiedCommandsAndGuards()
    {
        var scenario = new CombatEvalScenario(
            Deck: ["CARD.STRIKE_RED", "CARD.STRIKE_RED", "CARD.BASH"],
            Relics: ["RELIC.BURNING_BLOOD"],
            Potions: ["POTION.FIRE_POTION"],
            Encounter: "ENCOUNTER.JAW_WORM");

        var initialState = new LaboratoryPlayerState(
            PlayerCount: 1,
            Hp: 60,
            MaxHp: 80,
            PotionSlotCount: 3,
            IsMapSelectionReady: true,
            EncounterId: null,
            Phase: ObservationPhase.Run,
            Deck: [new LaboratoryCardState("CARD.DEFEND_RED", false, null)],
            Relics: ["RELIC.OLD_COIN"],
            Potions: []);

        var steps = scenario.Prepare(initialState);
        Assert.Equal(
            new[]
            {
                "remove_card DEFEND_RED Deck",
                "relic remove OLD_COIN",
                "card STRIKE_RED Deck",
                "card STRIKE_RED Deck",
                "card BASH Deck",
                "relic add BURNING_BLOOD",
                "potion FIRE_POTION",
            },
            steps.Select(step => step.Command).ToArray());

        var preparedState = initialState with
        {
            Hp = 80,
            Deck =
            [
                new LaboratoryCardState("CARD.STRIKE_RED", false, null),
                new LaboratoryCardState("CARD.STRIKE_RED", false, null),
                new LaboratoryCardState("CARD.BASH", false, null),
            ],
            Relics = ["RELIC.BURNING_BLOOD"],
            Potions = ["POTION.FIRE_POTION"],
        };
        Assert.True(scenario.MatchesResources(preparedState));
        Assert.True(scenario.FillHealth(initialState).IsComplete(preparedState));
        Assert.True(scenario.Fight().IsComplete(preparedState with
        {
            Phase = ObservationPhase.Combat,
            EncounterId = "ENCOUNTER.JAW_WORM",
        }));

        Assert.False(ReplayPresentation.Parse(null, headless: false, replay: false));
        Assert.True(ReplayPresentation.Parse("instant", headless: true, replay: true));
        Assert.Throws<InvalidOperationException>(() => ReplayPresentation.Parse("instant", headless: false, replay: true));

        var activated = 0;
        var lifecycle = new ReplayPresentationLifecycle(enabled: true);
        Assert.True(lifecycle.Activate(() => activated++));
        Assert.False(lifecycle.Activate(() => activated++));
        Assert.True(lifecycle.Active);
        Assert.Equal(1, activated);

        var scale = ReplayTimeScale.Parse("2.5", headless: true, replay: true)!;
        Assert.Equal(2.5, scale.BaseScale);
        Assert.Equal(0.5f, scale.ApplyHitStop(0.2f), precision: 4);
        Assert.Throws<ArgumentOutOfRangeException>(() => ReplayTimeScale.Parse("5.0", headless: true, replay: true));
    }
}
