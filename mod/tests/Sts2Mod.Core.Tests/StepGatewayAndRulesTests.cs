using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class StepGatewayAndRulesTests
{
    private static readonly StepRoute CombatRoute = new("game-1", "run-1", "node-1", ObservationOwner.Combat);
    private static readonly StepLocation CombatLocation = new("game-1", "run-1", "node-1", ObservationOwner.Combat);
    private static readonly StepRoute RunRoute = new("game-1", "run-1", "node-1", ObservationOwner.Run);
    private static readonly StepLocation RunLocation = new("game-1", "run-1", "node-1", ObservationOwner.Run);

    private static string ActionJson(string operationId, StepRoute route, string stepJson) =>
        $$"""
          {
            "operation_id": "{{operationId}}",
            "expected_route": {
              "game_instance_id": "{{route.GameInstanceId}}",
              "run_id": "{{route.RunId}}",
              "node_id": "{{route.NodeId}}",
              "owner": "{{JsonContract.Wire(route.Owner)}}"
            },
            "step": {{stepJson}}
          }
          """;

    [Fact]
    public void StepRules_BindTargetAndValidateSelection()
    {
        Assert.Equal(new StepTargetBinding(null, null), StepRules.BindTarget("enemy:1:1", required: false, ["enemy:1:1"]));
        Assert.Equal(new StepTargetBinding("enemy:1:1", null), StepRules.BindTarget(null, required: true, ["enemy:1:1"]));
        Assert.Equal(new StepTargetBinding("enemy:1:2", null), StepRules.BindTarget("enemy:1:2", required: true, ["enemy:1:1", "enemy:1:2"]));

        var ambiguous = StepRules.BindTarget(null, required: true, ["enemy:1:1", "enemy:1:2"]);
        Assert.NotNull(ambiguous.Error);
        Assert.Equal(2, ambiguous.Error!.LegalTargets.Length);

        var invalid = StepRules.BindTarget("enemy:1:9", required: true, ["enemy:1:1"]);
        Assert.Equal("enemy:1:9", invalid.Error!.Target);

        var selection = new SelectionView("Choose 1-2", null, 1, 2, ["card:1:1", "card:1:2"], []);
        Assert.Null(StepRules.ValidateSelection(["card:1:1"], selection));
        Assert.IsType<InvalidStepRefs>(StepRules.ValidateSelection(["card:9:9"], selection));
        Assert.IsType<InvalidStepSelection>(StepRules.ValidateSelection([], selection));
        Assert.IsType<InvalidStepSelection>(StepRules.ValidateSelection(
            ["card:1:1", "card:1:2", "card:1:3"],
            new SelectionView("Choose 1-2", null, 1, 2, ["card:1:1", "card:1:2", "card:1:3"], [])));
    }

    [Fact]
    public void StepGateway_EnforcesAdmissionDispatchAcceptanceAndNativeTaskSettlement()
    {
        var control = new AgentControlState();
        var gateway = new StepGateway(control, "game-1");
        gateway.Advance(CombatLocation, NativeInputReadiness.Ready, sequence: 1, _ => null, ascension: 4);

        // Paused control rejects submission immediately with 200 RejectedStep.
        var (pausedCode, pausedJson) = gateway.Submit(ActionJson("op-paused", CombatRoute, "{\"kind\":\"end_turn\"}"));
        Assert.Equal(200, pausedCode);
        using (var pausedDoc = JsonDocument.Parse(pausedJson))
        {
            Assert.Equal("rejected", pausedDoc.RootElement.GetProperty("status").GetString());
            Assert.Equal("action_unavailable", pausedDoc.RootElement.GetProperty("error").GetProperty("code").GetString());
        }

        control.SetMode(AgentControlMode.Running);

        // Malformed payload returns HTTP 400.
        var (badCode, _) = gateway.Submit("{\"operation_id\":\"bad\"}");
        Assert.Equal(400, badCode);

        // Submit play_card and track an asynchronous native task.
        var (submitCode, submitJson) = gateway.Submit(
            ActionJson("op-play", CombatRoute, "{\"kind\":\"play_card\",\"card\":\"card:1:1\",\"target\":\"enemy:1:1\"}"));
        Assert.Equal(200, submitCode);
        using (var submitDoc = JsonDocument.Parse(submitJson))
        {
            Assert.Equal("pending", submitDoc.RootElement.GetProperty("status").GetString());
            Assert.Equal("op-play", submitDoc.RootElement.GetProperty("operation_id").GetString());
        }

        // Duplicate operation ID and concurrent step while pending are rejected.
        var (_, dupJson) = gateway.Submit(ActionJson("op-play", CombatRoute, "{\"kind\":\"end_turn\"}"));
        using (var dupDoc = JsonDocument.Parse(dupJson))
        {
            Assert.Equal("duplicate_operation", dupDoc.RootElement.GetProperty("error").GetProperty("code").GetString());
        }

        var (_, busyJson) = gateway.Submit(ActionJson("op-other", CombatRoute, "{\"kind\":\"end_turn\"}"));
        using (var busyDoc = JsonDocument.Parse(busyJson))
        {
            Assert.Equal("native_busy", busyDoc.RootElement.GetProperty("error").GetProperty("code").GetString());
        }

        var taskSource = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        gateway.Advance(CombatLocation, NativeInputReadiness.Ready, sequence: 2, request =>
        {
            gateway.TrackNativeTask(request, taskSource.Task);
            return null;
        });
        Assert.Equal(NativeInputReadiness.Busy, gateway.Status().InputReadiness);

        // While task is still running, status stays Busy.
        gateway.Advance(CombatLocation, NativeInputReadiness.Ready, sequence: 3, _ => null);
        Assert.IsType<PendingStep>(gateway.Status().Receipt);

        // Completing task records completion sequence (4) and requires one later observed sequence (5) to settle Ready.
        taskSource.SetResult();
        gateway.Advance(CombatLocation, NativeInputReadiness.Ready, sequence: 4, _ => null);
        Assert.IsType<PendingStep>(gateway.Status().Receipt);

        gateway.Advance(CombatLocation, NativeInputReadiness.Ready, sequence: 5, _ => null);
        var completed = Assert.IsType<CompletedStep>(gateway.Status().Receipt);
        Assert.Equal("op-play", completed.OperationId);
        Assert.Equal(StepBoundary.Ready, completed.Boundary);

        // InteractStep without acceptance reports UnknownStep rather than false CompletedStep.
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 6, _ => null);
        gateway.Submit(ActionJson("op-interact", RunRoute, "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}"));
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 6, request =>
        {
            gateway.TrackNativeAcceptance(request, static () => false);
            return null;
        });
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 7, _ => null);
        Assert.IsType<UnknownStep>(gateway.Status().Receipt);
    }

    [Fact]
    public void StepGateway_HandlesInputBoundarySelectionRequiredProjectionAndPresentationLifecycle()
    {
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1");
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, _ => null);

        // Orrery-style inputBoundary settles a pending native task as soon as the reward screen yields Ready input.
        var pendingReward = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        gateway.Submit(ActionJson("op-orrery", RunRoute, "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}"));
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, request =>
        {
            gateway.TrackNativeTask(request, pendingReward.Task, inputBoundary: static () => true);
            return null;
        });
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 2, _ => null);
        Assert.Equal("op-orrery", Assert.IsType<CompletedStep>(gateway.Status().Receipt).OperationId);

        // Selection readiness rejects non-select steps with SelectionRequired.
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 3, _ => null);
        gateway.Submit(ActionJson("op-wrong", CombatRoute, "{\"kind\":\"end_turn\"}"));
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 3, _ => null);
        Assert.Equal(StepErrorCode.SelectionRequired, Assert.IsType<RejectedStep>(gateway.Status().Receipt).Error.Code);

        // Map presentation lifecycle: Running -> SettledUnverified (Locked) -> Verifying -> Restored (Open) -> Cleared on route move.
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 4, _ => null);
        Assert.Null(gateway.BeginPresentation("pres-1", RunRoute, static () => null));
        gateway.TrackPresentationWorker("pres-1", Task.CompletedTask);
        Assert.Equal(NativeInputReadiness.Busy, gateway.Status().InputReadiness);

        Assert.True(gateway.SettlePresentation("pres-1", PresentationDrawing.Shown, PresentationRestoration.Unknown));
        Assert.Equal(PresentationPhase.SettledUnverified, gateway.Presentation!.Phase);
        Assert.Equal(PresentationGate.Locked, gateway.Presentation.Gate);
        Assert.Equal(NativeInputReadiness.Unavailable, gateway.Status().InputReadiness);

        var runView = new RunStateView(
            PublicInputControl.Ready,
            new PublicProgress(1, 3, 1, "map:0:0", "monster", "Underdocks", "ACT.UNDERDOCKS"),
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
        var publication = new NativeReadPublication(new CompletedStateRead(new NativeObservation(
            new ObservationStamp("game-1", 4),
            new ObservationLocation("game-1", "run-1", "node-1", ObservationOwner.Run),
            "eng",
            NativeInputReadiness.Ready,
            runView,
            ImmutableDictionary<string, ObservedObject>.Empty,
            ImmutableDictionary<string, string?>.Empty,
            ImmutableDictionary<string, CardModifierDescriptions>.Empty,
            ImmutableDictionary<string, UpgradePreview>.Empty,
            ImmutableDictionary<string, ImmutableArray<GeneratedCard>>.Empty,
            [],
            new RunRules(0, []))));

        var projected = Assert.IsType<CompletedStateRead>(gateway.Project(publication).Reply);
        Assert.Equal(NativeInputReadiness.Unavailable, projected.Observation.InputReadiness);
        Assert.Equal(PublicInputControl.Unavailable, ((RunStateView)projected.Observation.State).Control);

        Assert.Null(gateway.BeginPresentationVerification("pres-1", RunRoute));
        gateway.TrackPresentationVerifier("pres-1", Task.CompletedTask);
        Assert.True(gateway.FinishPresentationVerification("pres-1", PresentationRestoration.Restored));
        Assert.Equal(PresentationPhase.Restored, gateway.Presentation!.Phase);
        Assert.Equal(PresentationGate.Open, gateway.Presentation.Gate);

        gateway.Advance(new StepLocation("game-1", "run-1", "node-2", ObservationOwner.Run), NativeInputReadiness.Ready, sequence: 5, _ => null);
        Assert.Null(gateway.Presentation);
    }

    [Fact]
    public async Task MapPresentationTransportAndBarrier_ValidateEdgesAndBoundAnimationFrames()
    {
        PresentationRequest? capturedPresent = null;
        PresentationVerificationRequest? capturedVerify = null;
        var transport = new MapPresentationTransport(
            request =>
            {
                capturedPresent = request;
                return Task.FromResult(new HttpReply(200, "{\"ok\":true}"));
            },
            request =>
            {
                capturedVerify = request;
                return Task.FromResult(new HttpReply(200, "{\"verified\":true}"));
            });

        var validPresent = """
            {
              "operation_id": "map-1",
              "expected_route": {
                "game_instance_id": "game-1",
                "run_id": "run-1",
                "node_id": "node-1",
                "owner": "run"
              },
              "edges": [
                { "from": "map:0:0", "to": "map:1:1" },
                { "from": "map:1:1", "to": "map:-1:2" }
              ]
            }
            """;
        var presentReply = await transport.Present(validPresent);
        Assert.Equal(200, presentReply.StatusCode);
        Assert.NotNull(capturedPresent);
        Assert.Equal(2, capturedPresent!.Edges.Count);

        var badEdgesReply = await transport.Present(validPresent.Replace("map:1:1", "invalid:coord"));
        Assert.Equal(400, badEdgesReply.StatusCode);

        var verifyReply = await transport.Verify("""
            {
              "operation_id": "map-1",
              "expected_route": {
                "game_instance_id": "game-1",
                "run_id": "run-1",
                "node_id": "node-1",
                "owner": "run"
              }
            }
            """);
        Assert.Equal(200, verifyReply.StatusCode);
        Assert.Equal("map-1", capturedVerify!.OperationId);

        var unknownBarrier = new MapOpeningBarrier(ownershipKnown: false, maxFrames: 2);
        Assert.False(unknownBarrier.Advance(animationIsLive: false));

        var barrier = new MapOpeningBarrier(ownershipKnown: true, maxFrames: 2);
        Assert.False(barrier.Advance(animationIsLive: true));
        Assert.False(barrier.Advance(animationIsLive: true));
        Assert.False(barrier.TimedOut);
        Assert.True(barrier.Advance(animationIsLive: true));
        Assert.True(barrier.TimedOut);
    }
}
