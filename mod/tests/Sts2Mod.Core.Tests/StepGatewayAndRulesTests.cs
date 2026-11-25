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

    private sealed class ManualClock : TimeProvider
    {
        public long Seconds { get; set; }

        public override long TimestampFrequency => 1;

        public override long GetTimestamp() => Seconds;
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void StalledSelectionReleasesInputAfterFreshObservation(bool selectionAlreadyOpen)
    {
        var clock = new ManualClock();
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1", timeProvider: clock);
        var initialReadiness = selectionAlreadyOpen ? NativeInputReadiness.Selection : NativeInputReadiness.Ready;
        var selector = new object();
        var initialSelector = selectionAlreadyOpen ? selector : null;
        gateway.Advance(RunLocation, initialReadiness, sequence: 1, _ => null, selectionIdentity: initialSelector);
        var step = selectionAlreadyOpen
            ? "{\"kind\":\"select\",\"refs\":[\"card:1:1\"]}"
            : "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}";
        gateway.Submit(ActionJson("first", RunRoute, step));
        StepRequest? dispatched = null;
        gateway.Advance(RunLocation, initialReadiness, sequence: 1, request =>
        {
            dispatched = request;
            return null;
        }, selectionIdentity: initialSelector);
        var first = Assert.IsType<StepRequest>(dispatched);

        for (var second = 1; second < 8; second++)
        {
            clock.Seconds = second;
            gateway.Advance(RunLocation, NativeInputReadiness.Busy, sequence: second + 1, _ => null, selectionIdentity: selector);
            Assert.IsType<PendingStep>(gateway.Status().Receipt);
            Assert.True(gateway.OwnsPendingStep(first));
        }

        clock.Seconds = 9;
        gateway.Advance(RunLocation, NativeInputReadiness.Busy, sequence: 10, _ => null, selectionIdentity: selector);
        Assert.False(gateway.OwnsPendingStep(first));
        Assert.IsType<PendingStep>(gateway.Status().Receipt);

        gateway.Advance(RunLocation, NativeInputReadiness.Selection, sequence: 11, _ => null, selectionIdentity: selector);
        Assert.Equal("first", Assert.IsType<UnknownStep>(gateway.Status().Receipt).OperationId);
        gateway.Submit(ActionJson("new", RunRoute, "{\"kind\":\"select\",\"refs\":[\"card:1:1\"]}"));
        gateway.Advance(RunLocation, NativeInputReadiness.Selection, sequence: 11,
            request => new CompletedStep(request.OperationId, StepBoundary.Ready), selectionIdentity: selector);
        Assert.Equal("new", Assert.IsType<CompletedStep>(gateway.Status().Receipt).OperationId);
    }

    [Fact]
    public void ClosedSelectionWaitsForNativeResolutionAcrossPause()
    {
        var clock = new ManualClock();
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1", timeProvider: clock);
        var selector = new object();
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 1, _ => null, selectionIdentity: selector);
        gateway.Submit(ActionJson("select", CombatRoute, "{\"kind\":\"select\",\"refs\":[\"card:1:1\"]}"));

        var cue = new TaskCompletionSource();
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 1, request =>
        {
            gateway.TrackNativeTask(request, cue.Task);
            return null;
        }, selectionIdentity: selector);
        clock.Seconds = 1;
        cue.SetResult();
        gateway.Advance(CombatLocation, NativeInputReadiness.Busy, sequence: 2, _ => null);

        for (var second = 2; second < 20; second++)
        {
            clock.Seconds = second;
            gateway.Advance(CombatLocation, NativeInputReadiness.Busy, sequence: second + 1, _ => null);
            Assert.Equal("select", Assert.IsType<PendingStep>(gateway.Status().Receipt).OperationId);
        }

        control.SetMode(AgentControlMode.Paused);
        clock.Seconds = 20;
        gateway.Advance(CombatLocation, NativeInputReadiness.Ready, sequence: 21, _ => null);
        Assert.Equal(AgentControlMode.Paused, gateway.Status().ControlIntent);
        var receipt = Assert.IsType<CompletedStep>(gateway.Status().Receipt);
        Assert.Equal("select", receipt.OperationId);
        Assert.Equal(StepBoundary.Ready, receipt.Boundary);
    }

    [Fact]
    public void SuccessorSelectionKeepsTheOperationUntilItsOwnInputBoundary()
    {
        var clock = new ManualClock();
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1", timeProvider: clock);
        var firstSelector = new object();
        var nextSelector = new object();
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 1, _ => null, selectionIdentity: firstSelector);
        gateway.Submit(ActionJson("select", CombatRoute, "{\"kind\":\"select\",\"refs\":[\"card:1:1\"]}"));
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 1, _ => null, selectionIdentity: firstSelector);

        clock.Seconds = 6;
        gateway.Advance(CombatLocation, NativeInputReadiness.Busy, sequence: 2, _ => null, selectionIdentity: firstSelector);
        clock.Seconds = 7;
        gateway.Advance(CombatLocation, NativeInputReadiness.Busy, sequence: 3, _ => null, selectionIdentity: nextSelector);
        clock.Seconds = 9;
        gateway.Advance(CombatLocation, NativeInputReadiness.Selection, sequence: 4, _ => null, selectionIdentity: nextSelector);

        var receipt = Assert.IsType<CompletedStep>(gateway.Status().Receipt);
        Assert.Equal("select", receipt.OperationId);
        Assert.Equal(StepBoundary.Selection, receipt.Boundary);
    }

    [Fact]
    public void ExpiredQueuedStepDoesNotReachNativeDispatch()
    {
        var clock = new ManualClock();
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1", timeProvider: clock);
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, _ => null);
        gateway.Submit(ActionJson("expired", RunRoute, "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}"));

        clock.Seconds = 60;
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 2,
            _ => throw new InvalidOperationException("Expired input reached the game."));

        var receipt = Assert.IsType<RejectedStep>(gateway.Status().Receipt);
        Assert.Equal("expired", receipt.OperationId);
        Assert.Equal(StepErrorCode.NativeBusy, receipt.Error.Code);
    }

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
        Assert.IsType<PendingStep>(gateway.Status().Receipt);
        Assert.Equal(NativeInputReadiness.Busy, gateway.Status().InputReadiness);
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 7, _ => null);
        Assert.IsType<PendingStep>(gateway.Status().Receipt);
        gateway.Advance(RunLocation, NativeInputReadiness.Busy, sequence: 8, _ => null);
        Assert.IsType<PendingStep>(gateway.Status().Receipt);
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 9, _ => null);
        Assert.Equal("op-interact", Assert.IsType<UnknownStep>(gateway.Status().Receipt).OperationId);
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

        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 6, _ => null);
        gateway.Submit(ActionJson("op-uncertain", RunRoute, "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}"));
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 6, request =>
            new UnknownStep(request.OperationId, new StepCause(StepErrorCode.ExecutionUncertain, "Read the fresh observation.")));
        Assert.IsType<PendingStep>(gateway.Status().Receipt);
        Assert.IsType<FailedStateRead>(gateway.Project(publication).Reply);
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 7, _ => null);
        Assert.IsType<UnknownStep>(gateway.Status().Receipt);
        Assert.IsType<FailedStateRead>(gateway.Project(publication).Reply);
        var original = Assert.IsType<CompletedStateRead>(publication.Reply).Observation;
        var newer = publication with
        {
            Reply = new CompletedStateRead(original with { Stamp = new ObservationStamp("game-1", 7) }),
        };
        var fresh = Assert.IsType<CompletedStateRead>(gateway.Project(newer).Reply);
        Assert.Equal(NativeInputReadiness.Ready, fresh.Observation.InputReadiness);
    }

    [Fact]
    public void MapPresentation_RetiresOldLockedGateOnlyAfterWorkerStopsAndNewRunIsObserved()
    {
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1");
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, _ => null);
        Assert.Null(gateway.BeginPresentation("map-old", RunRoute, static () => null));
        var worker = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        gateway.TrackPresentationWorker("map-old", worker.Task);

        gateway.ClearReceiptOnRunReset();
        var newRun = RunLocation with { RunId = "run-2" };
        gateway.Advance(newRun, NativeInputReadiness.Unavailable, sequence: -1, _ => null);
        Assert.Equal(PresentationPhase.Running, gateway.Presentation!.Phase);
        gateway.Advance(newRun, NativeInputReadiness.Ready, sequence: 2, _ => null);
        Assert.Equal(PresentationPhase.Running, gateway.Presentation!.Phase);

        worker.SetResult();
        Assert.True(gateway.SettlePresentation("map-old", PresentationDrawing.Failed, PresentationRestoration.Unknown));
        gateway.Advance(newRun, NativeInputReadiness.Unavailable, sequence: -1, _ => null);
        Assert.Equal(PresentationPhase.SettledUnverified, gateway.Presentation!.Phase);
        gateway.Advance(newRun, NativeInputReadiness.Ready, sequence: 3, _ => null);
        Assert.Null(gateway.Presentation);
        Assert.Null(gateway.BeginPresentation("map-new", RunRoute with { RunId = "run-2" }, static () => null));
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

    [Fact]
    public void StepGateway_PreTouchRejectionAndScopedUncertainGuardOperationIdentity()
    {
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var gateway = new StepGateway(control, "game-1");
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, _ => null);

        gateway.Submit(ActionJson("op-highlight", RunRoute, "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}"));
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, _ => null);
        Assert.IsType<PendingStep>(gateway.Status().Receipt);

        // Stale operation IDs do not disturb the active pending operation.
        gateway.RejectPendingBeforeTouch("stale-op", StepErrorCode.ActionUnavailable, "Ignored.");
        gateway.ReportUncertain("stale-op");
        Assert.IsType<PendingStep>(gateway.Status().Receipt);

        // Pausing before native input is touched rejects cleanly without ExecutionUncertain.
        gateway.RejectPendingBeforeTouch("op-highlight", StepErrorCode.ActionUnavailable, "Paused before dispatch.");
        var rejected = Assert.IsType<RejectedStep>(gateway.Status().Receipt);
        Assert.Equal("op-highlight", rejected.OperationId);
        Assert.Equal(StepErrorCode.ActionUnavailable, rejected.Error.Code);

        var externalOwner = false;
        gateway.SetExternalInputBusyCheck(() => externalOwner);
        gateway.Submit(ActionJson("op-external", RunRoute, "{\"kind\":\"interact\",\"action\":\"interaction:1:1\"}"));
        Assert.IsType<PendingStep>(gateway.Status().Receipt);
        externalOwner = true;
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 2, _ =>
            throw new InvalidOperationException("The external input owner must prevent dispatch."));
        Assert.Equal(StepErrorCode.NativeBusy, Assert.IsType<RejectedStep>(gateway.Status().Receipt).Error.Code);
    }

    [Fact]
    public async Task RewardOpenTransport_ValidatesRouteOfferAndDuplicateKeys()
    {
        RewardOpenRequest? captured = null;
        RewardOpenRequest? capturedClose = null;
        var transport = new RewardOpenTransport(
            request =>
            {
                captured = request;
                return Task.FromResult(new HttpReply(200, JsonContract.Serialize(new RewardOpenReply(RewardOpenStatus.Opened))));
            },
            request =>
            {
                capturedClose = request;
                return Task.FromResult(new HttpReply(200, JsonContract.Serialize(new RewardCloseReply(RewardCloseStatus.Closed))));
            });

        var valid = """
            {
              "expected_route": {
                "game_instance_id": "game-1",
                "run_id": "run-1",
                "node_id": "node-1",
                "owner": "run"
              },
              "offer": "offer:1:2"
            }
            """;
        var ok = await transport.Open(valid);
        Assert.Equal(200, ok.StatusCode);
        Assert.Equal(RunRoute, captured!.ExpectedRoute);
        Assert.Equal("offer:1:2", captured.Offer);
        using (var doc = JsonDocument.Parse(ok.Json))
        {
            Assert.Equal("opened", doc.RootElement.GetProperty("status").GetString());
        }

        var closed = await transport.Close(valid);
        Assert.Equal(200, closed.StatusCode);
        Assert.Equal(RunRoute, capturedClose!.ExpectedRoute);
        Assert.Equal("offer:1:2", capturedClose.Offer);
        using (var doc = JsonDocument.Parse(closed.Json))
        {
            Assert.Equal("closed", doc.RootElement.GetProperty("status").GetString());
        }

        foreach (var bad in new[]
                 {
                     "{}",
                     """{"expected_route":{"game_instance_id":"game-1","run_id":"run-1","node_id":"node-1","owner":"run"}}""",
                     """{"expected_route":{"game_instance_id":"game-1","run_id":"run-1","node_id":"node-1","owner":"run"},"offer":""}""",
                     """{"expected_route":{"game_instance_id":"game-1","run_id":"run-1","node_id":"node-1","owner":"run"},"offer":"offer:1:2","extra":1}""",
                     """{"expected_route":{"game_instance_id":"game-1","run_id":"run-1","node_id":"node-1","owner":"run"},"offer":"offer:1:2","offer":"offer:1:2"}""",
                 })
        {
            var rejected = await transport.Open(bad);
            Assert.Equal(400, rejected.StatusCode);
        }
    }

    [Fact]
    public async Task RewardOpenTracker_GatesStepGatewayUntilSelectionSettlesAndHandlesAbort()
    {
        var clock = new ManualClock();
        var control = new AgentControlState();
        control.SetMode(AgentControlMode.Running);
        var tracker = new RewardOpenTracker(clock, TimeSpan.FromSeconds(3));
        var gateway = new StepGateway(control, "game-1", timeProvider: clock);
        gateway.SetExternalInputBusyCheck(() => tracker.IsBusy);
        gateway.Advance(RunLocation, NativeInputReadiness.Ready, sequence: 1, _ => null);

        var request = new RewardOpenRequest(RunRoute, "offer:1:2");
        Assert.True(tracker.TryBegin(request, out var opening));
        Assert.True(tracker.IsBusy);
        Assert.Equal(NativeInputReadiness.Busy, gateway.Status().InputReadiness);

        // Concurrent open and concurrent gameplay step are both rejected while opening settles.
        Assert.False(tracker.TryBegin(new RewardOpenRequest(RunRoute, "offer:1:3"), out var duplicate));
        Assert.Equal(RewardOpenStatus.Skipped, await duplicate);
        var (_, busyJson) = gateway.Submit(ActionJson("op-race", RunRoute, "{\"kind\":\"select\",\"refs\":[\"card:1:1\"]}"));
        using (var busyDoc = JsonDocument.Parse(busyJson))
        {
            Assert.Equal("native_busy", busyDoc.RootElement.GetProperty("error").GetProperty("code").GetString());
        }

        // Another group's selection does not settle this opening.
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Selection, isRunning: true, activeSelectionOfferRef: "offer:1:9");
        Assert.True(tracker.IsBusy);
        Assert.False(opening.IsCompleted);

        // Matching group with Selection readiness settles Opened and releases StepGateway.
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Selection, isRunning: true, activeSelectionOfferRef: "offer:1:2");
        Assert.Equal(RewardOpenStatus.Opened, await opening);
        Assert.False(tracker.IsBusy);
        gateway.Advance(RunLocation, NativeInputReadiness.Selection, sequence: 2, _ => null);
        Assert.Equal(NativeInputReadiness.Selection, gateway.Status().InputReadiness);

        // Pause aborts cleanly.
        Assert.True(tracker.TryBegin(request, out var pausedOpen));
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Busy, isRunning: false, activeSelectionOfferRef: null);
        Assert.Equal(RewardOpenStatus.Skipped, await pausedOpen);
        Assert.False(tracker.IsBusy);

        // Timeout aborts cleanly.
        Assert.True(tracker.TryBegin(request, out var timedOut));
        clock.Seconds = 2;
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Busy, isRunning: true, activeSelectionOfferRef: null);
        Assert.True(tracker.IsBusy);
        clock.Seconds = 3;
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Busy, isRunning: true, activeSelectionOfferRef: null);
        Assert.Equal(RewardOpenStatus.Skipped, await timedOut);
        Assert.False(tracker.IsBusy);

        // Route change aborts cleanly.
        Assert.True(tracker.TryBegin(request, out var routeChanged));
        tracker.ObserveFrame(RunLocation with { NodeId = "node-2" }, NativeInputReadiness.Ready, isRunning: true, activeSelectionOfferRef: null);
        Assert.Equal(RewardOpenStatus.Skipped, await routeChanged);
        Assert.False(tracker.IsBusy);

        // Close transition gates StepGateway until Ready with no selection settles.
        Assert.True(tracker.TryBeginClose(request, out var closing));
        Assert.True(tracker.IsBusy);
        Assert.Equal(NativeInputReadiness.Busy, gateway.Status().InputReadiness);
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Selection, isRunning: true, activeSelectionOfferRef: "offer:1:2", hasSelection: true);
        Assert.True(tracker.IsBusy);
        tracker.ObserveFrame(RunLocation, NativeInputReadiness.Ready, isRunning: true, activeSelectionOfferRef: null, hasSelection: false);
        Assert.Equal(RewardCloseStatus.Closed, await closing);
        Assert.False(tracker.IsBusy);
    }
}
