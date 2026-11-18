using System.Collections.Concurrent;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.Assets;
using MegaCrit.Sts2.Core.Multiplayer;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.Audio;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Saves.Managers;
using MegaCrit.Sts2.Core.Saves.Runs;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Execution;
using Sts2Mod.Observation;

namespace Sts2Mod.Developer;

/// <summary>
/// Owns <c>/operator/checkpoint</c> and <c>/operator/checkpoint/identity</c> in headless developer sessions.
/// </summary>
internal sealed class CheckpointService(
    bool enabled,
    AgentControlState control,
    Func<NativeStepStatus> readStepStatus,
    object admissionLock)
{
    private static readonly FieldInfo SaveStoreField =
        typeof(SaveManager).GetField("_saveStore", BindingFlags.Instance | BindingFlags.NonPublic)
        ?? throw new MissingFieldException(typeof(SaveManager).FullName, "_saveStore");

    private sealed class ActiveOperation(
        CheckpointRequest request,
        Task worker,
        CheckpointFileProof? validatedFile = null)
    {
        public CheckpointRequest Request { get; } = request;
        public Task Worker { get; } = worker;
        public CheckpointFileProof? ValidatedFile { get; } = validatedFile;
        public DateTimeOffset StartedAt { get; } = DateTimeOffset.UtcNow;
    }

    private readonly CheckpointCommandStore _commands = new();
    private readonly ConcurrentQueue<CheckpointRequest> _pendingQueue = new();
    private ActiveOperation? _active;
    private volatile bool _ownsInput;

    public bool Enabled => enabled;
    public bool OwnsInput => _ownsInput;

    public HttpReply ReadIdentity()
    {
        if (!enabled)
        {
            return new HttpReply(400, InvalidRequestReply.Json(
                "Development checkpoints are disabled for this launch.",
                "checkpoint"));
        }
        return new HttpReply(200, ObservationHub.ReadCheckpointIdentityJson());
    }

    public HttpReply Submit(string body, Func<bool> otherDeveloperWorkBusy)
    {
        if (!enabled)
        {
            return Reply(
                200,
                new RejectedCheckpoint(
                    null,
                    new CheckpointCause(CheckpointErrorCode.Disabled, "Development checkpoints are disabled for this launch.")));
        }

        CheckpointRequest request;
        try
        {
            request = CheckpointContract.Decode(body);
        }
        catch (Exception error) when (error is JsonException or ArgumentException or InvalidOperationException)
        {
            return Reply(
                400,
                new RejectedCheckpoint(
                    null,
                    new CheckpointCause(CheckpointErrorCode.InvalidRequest, "The request does not match the checkpoint contract.")));
        }

        lock (admissionLock)
        {
            if (request is CheckpointStatusRequest)
                return StatusReply(request.CommandId);

            var fingerprint = CheckpointContract.Fingerprint(request);
            if (_commands.Get(request.CommandId) is { } existing)
            {
                return existing.Fingerprint == fingerprint && existing.Kind == request.Kind
                    ? StatusReply(request.CommandId)
                    : Reply(
                        200,
                        new RejectedCheckpoint(
                            request.CommandId,
                            new CheckpointCause(CheckpointErrorCode.Conflict, "This command ID belongs to a different request.")));
            }

            var (_, added) = _commands.ReservePending(request.CommandId, request.Kind, fingerprint);
            if (!added)
            {
                return Reply(
                    200,
                    new RejectedCheckpoint(
                        request.CommandId,
                        new CheckpointCause(CheckpointErrorCode.Conflict, "This command ID is already reserved.")));
            }

            var status = readStepStatus();
            if (_ownsInput
                || otherDeveloperWorkBusy()
                || _commands.HasNonTerminalOtherThan(request.CommandId)
                || control.Mode != AgentControlMode.Paused
                || status.Receipt is PendingStep or UnknownStep
                || status.Presentation is { Gate: PresentationGate.Locked })
            {
                return RejectBeforeDispatch(request.CommandId, CheckpointErrorCode.Busy, "checkpoint_busy");
            }

            if (request is CaptureCheckpointRequest capture)
            {
                var expected = capture.ExpectedRoute;
                if (status.InputReadiness != NativeInputReadiness.Ready
                    || status.Location != new StepLocation(expected.GameInstanceId, expected.RunId, expected.NodeId, expected.Owner)
                    || ObservationHub.Current.Reply is not CompletedStateRead completed
                    || completed.Observation.State is not RunStateView { Screen: RunScreen.Map }
                    || Directory.Exists(capture.OutputDirectory)
                    || File.Exists(capture.OutputDirectory))
                {
                    return RejectBeforeDispatch(
                        request.CommandId,
                        CheckpointErrorCode.NativeRejected,
                        "checkpoint_requires_expected_map_and_fresh_directory");
                }
            }
            else if (status.Location.RunId is not null || status.InputReadiness == NativeInputReadiness.Busy)
            {
                return RejectBeforeDispatch(
                    request.CommandId,
                    CheckpointErrorCode.NativeRejected,
                    "checkpoint_load_requires_fresh_menu");
            }

            _ownsInput = true;
            _pendingQueue.Enqueue(request);
            return StatusReply(request.CommandId);
        }
    }

    public void Advance()
    {
        if (_active is null && _pendingQueue.TryDequeue(out var next))
            BeginOnGameThread(next);

        if (_active is not { } active)
            return;

        if (DateTimeOffset.UtcNow - active.StartedAt > TimeSpan.FromSeconds(45))
        {
            SettleUncertain(active.Request.CommandId, dispatched: true, "checkpoint_timeout");
            _active = null;
            return;
        }

        if (!active.Worker.IsCompleted)
            return;

        if (active.Worker.IsFaulted || active.Worker.IsCanceled)
        {
            GD.PrintErr($"[sts2agent] Checkpoint task faulted: {active.Worker.Exception}");
            SettleUncertain(active.Request.CommandId, dispatched: true, "checkpoint_task_failed");
            _active = null;
            return;
        }

        if (!TryBuildSettledProof(active, out var proof))
            return;

        _commands.Settle(active.Request.CommandId, new CompletedCheckpoint(active.Request.CommandId, proof!));
        _active = null;
        _ownsInput = false;
    }

    private void BeginOnGameThread(CheckpointRequest request)
    {
        try
        {
            if (SaveManager.Instance.CurrentRunSaveTask is { IsCompleted: false })
            {
                SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_native_busy");
                return;
            }

            if (request is CaptureCheckpointRequest capture)
            {
                var state = RunManager.Instance.DebugOnlyGetState();
                if (!SaveManager.Instance.IsProfileInitialized
                    || state is null
                    || !RunManager.Instance.ActionQueueSet.IsEmpty
                    || !MatchesMapBoundary(capture.ExpectedRoute))
                {
                    SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_expected_map_changed");
                    return;
                }

                var currentRoom = state.CurrentRoom;
                var restorableShell = currentRoom is { IsPreFinished: true }
                                      && currentRoom.RoomType is RoomType.Monster or RoomType.Elite or RoomType.Boss or RoomType.Event;
                _commands.MarkDispatched(request.CommandId);
                var saveTask = SaveManager.Instance.SaveRun(restorableShell ? currentRoom : null);
                _active = new ActiveOperation(request, saveTask);
                return;
            }

            var load = (LoadCheckpointRequest)request;
            if (RunManager.Instance.IsInProgress || NGame.Instance is not { } game)
            {
                SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_load_requires_fresh_menu");
                return;
            }

            var bytes = File.ReadAllBytes(load.CheckpointPath);
            var actualSha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
            if (!string.Equals(actualSha256, load.CheckpointSha256, StringComparison.Ordinal))
            {
                SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_hash_mismatch");
                return;
            }

            var read = JsonSerializationUtility.FromJson<SerializableRun>(Encoding.UTF8.GetString(bytes));
            if (!read.Success || read.SaveData is not { } save)
            {
                SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_invalid");
                return;
            }

            if (save.PreFinishedRoom is { } room
                && (!room.IsPreFinished
                    || room.RoomType is not (RoomType.Monster or RoomType.Elite or RoomType.Boss or RoomType.Event)))
            {
                SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_not_map_frontier");
                return;
            }

            var runState = RunState.FromSerializable(save);
            if (!MatchesReloadTarget(runState, load.Target))
            {
                SettleRejectedBeforeDispatch(request.CommandId, "run_reload_identity_drift");
                return;
            }

            var fileProof = new CheckpointFileProof(load.CheckpointPath, actualSha256, bytes.LongLength);
            control.ArmRunLoadBoundary();
            _commands.MarkDispatched(request.CommandId);
            var restoreTask = RestoreCheckpointAsync(game, runState, save);
            _active = new ActiveOperation(request, restoreTask, fileProof);
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Checkpoint preflight failed: {error}");
            SettleRejectedBeforeDispatch(request.CommandId, "checkpoint_invalid");
        }
    }

    private bool TryBuildSettledProof(ActiveOperation active, out CheckpointProof? proof)
    {
        proof = null;
        if (ObservationHub.Current.Reply is not CompletedStateRead completed
            || completed.Observation.InputReadiness != NativeInputReadiness.Ready
            || completed.Observation.State is not RunStateView { Screen: RunScreen.Map }
            || completed.Observation.Location is not { RunId: not null, NodeId: not null, Owner: { } owner } location
            || NMapScreen.Instance is not { IsOpen: true, IsTravelEnabled: true, IsTraveling: false }
            || RunManager.Instance.DebugOnlyGetState() is not { } run
            || run.Players.Count == 0)
        {
            return false;
        }

        try
        {
            var route = new StepRoute(location.GameInstanceId, location.RunId, location.NodeId, owner);
            var player = run.Players[0];
            var target = new ReloadTarget(
                run.Rng.StringSeed,
                run.GameMode.ToString().ToLowerInvariant(),
                player.Character.Id.Entry,
                run.AscensionLevel,
                run.CurrentActIndex + 1,
                run.TotalFloor);
            var stateProof = new CheckpointStateProof(
                route,
                ObservationHub.CurrentRunInstanceId,
                completed.Observation.Stamp.ObservationSequence,
                target);

            if (active.Request is LoadCheckpointRequest)
            {
                proof = new LoadedCheckpointProof(
                    stateProof,
                    active.ValidatedFile ?? throw new InvalidOperationException("Missing loaded checkpoint file proof."));
                return true;
            }

            var capture = (CaptureCheckpointRequest)active.Request;
            if (route != capture.ExpectedRoute)
                throw new InvalidOperationException("The map route changed while saving the checkpoint.");

            var sourcePath = GetCurrentRunSavePath(SaveManager.Instance);
            proof = new CapturedCheckpointProof(
                stateProof,
                CheckpointFiles.Capture(sourcePath, capture.OutputDirectory));
            return true;
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Checkpoint proof verification failed: {error}");
            SettleUncertain(active.Request.CommandId, dispatched: true, "checkpoint_proof_failed");
            _active = null;
            return false;
        }
    }

    private static async Task RestoreCheckpointAsync(NGame game, RunState state, SerializableRun save)
    {
        if (SaveManager.Instance.CurrentRunSaveTask is { } currentSave)
            await currentSave;

        NAudioManager.Instance?.StopMusic();
        await game.Transition.FadeOut();
        CardSelectionInterceptor.ResetForRunBoundary();
        RunManager.Instance.CleanUp();

        try
        {
            await RunManager.Instance.SetUpSavedSingleplayer(state, save);
            game.ReactionContainer.InitializeNetworking(new NetSingleplayerGameService());

            await PreloadManager.LoadRunAssets(state.Players.Select(player => player.Character));
            await PreloadManager.LoadActAssets(state.Act);
            RunManager.Instance.Launch();
            var runNode = NRun.Create(state);
            game.RootSceneContainer.SetCurrentScene(runNode);
            await RunManager.Instance.GenerateMap();

            var mapScreen = runNode.GlobalUi.MapScreen;
            if (RunManager.Instance.MapDrawingsToLoad is { } drawings)
            {
                mapScreen.Drawings.LoadDrawings(drawings);
                RunManager.Instance.MapDrawingsToLoad = null;
            }
            mapScreen.IsTraveling = false;
            mapScreen.SetTravelEnabled(enabled: true);
            mapScreen.Open();
            // This map frontier bypasses room entry, which normally unpauses the action executor.
            RunManager.Instance.ActionExecutor.Unpause();

            await game.Transition.FadeIn();

            var stableFrames = 0;
            for (var frame = 0; frame < 180; frame++)
            {
                if (mapScreen.IsOpen && mapScreen.IsTravelEnabled)
                {
                    if (++stableFrames >= 10)
                        return;
                }
                else
                {
                    stableFrames = 0;
                }
                await game.ToSignal(game.GetTree(), SceneTree.SignalName.ProcessFrame);
            }
            throw new InvalidOperationException("Restored map frontier did not stabilize.");
        }
        catch
        {
            RunManager.Instance.CleanUp();
            throw;
        }
    }

    private static bool MatchesMapBoundary(StepRoute expected) =>
        ObservationHub.Current.Reply is CompletedStateRead completed
        && completed.Observation.InputReadiness == NativeInputReadiness.Ready
        && completed.Observation.Location is { RunId: not null, NodeId: not null, Owner: { } owner } route
        && expected == new StepRoute(route.GameInstanceId, route.RunId, route.NodeId, owner)
        && NMapScreen.Instance is { IsOpen: true, IsTravelEnabled: true, IsTraveling: false };

    private static bool MatchesReloadTarget(RunState state, ReloadTarget target) =>
        state.Players.FirstOrDefault() is { } player
        && string.Equals(state.Rng.StringSeed, target.CanonicalSeed, StringComparison.Ordinal)
        && string.Equals(state.GameMode.ToString().ToLowerInvariant(), target.GameMode, StringComparison.Ordinal)
        && string.Equals(player.Character.Id.Entry, target.Character, StringComparison.Ordinal)
        && state.AscensionLevel == target.Ascension
        && state.CurrentActIndex + 1 == target.Act
        && state.TotalFloor == target.Floor;

    private static string GetCurrentRunSavePath(SaveManager saveManager)
    {
        var store = (ISaveStore)SaveStoreField.GetValue(saveManager)!;
        var key = RunSaveManager.GetRunSavePath(saveManager.CurrentProfileId, RunSaveManager.runSaveFileName);
        return ProjectSettings.GlobalizePath(store.GetFullPath(key));
    }

    private HttpReply RejectBeforeDispatch(
        string commandId,
        CheckpointErrorCode code,
        string nativeReason)
    {
        _commands.Settle(
            commandId,
            new RejectedCheckpoint(
                commandId,
                new CheckpointCause(code, "Checkpoint admission was rejected before native dispatch.", nativeReason)));
        return StatusReply(commandId);
    }

    private void SettleRejectedBeforeDispatch(string commandId, string nativeReason)
    {
        control.CancelRunLoadBoundary();
        _commands.Settle(
            commandId,
            new RejectedCheckpoint(
                commandId,
                new CheckpointCause(
                    CheckpointErrorCode.NativeRejected,
                    "Checkpoint admission was rejected before native dispatch.",
                    nativeReason)));
        _ownsInput = false;
    }

    private void SettleUncertain(string commandId, bool dispatched, string nativeReason)
    {
        _commands.Settle(
            commandId,
            new UnknownCheckpoint(
                commandId,
                dispatched,
                new CheckpointCause(
                    CheckpointErrorCode.ExecutionUncertain,
                    "The native checkpoint operation may have taken effect. Keep paused and inspect state.",
                    nativeReason)));
        // Leave _ownsInput = true so subsequent mutations do not race an uncertain native state.
    }

    private HttpReply StatusReply(string commandId)
    {
        if (_commands.Get(commandId) is { } entry)
            return Reply(200, entry.Reply);
        return Reply(
            200,
            new RejectedCheckpoint(
                commandId,
                new CheckpointCause(CheckpointErrorCode.NotFound, "No checkpoint command has this ID in this process.")));
    }

    private static HttpReply Reply(int statusCode, CheckpointReply reply) =>
        new(statusCode, JsonContract.Serialize(reply));
}
