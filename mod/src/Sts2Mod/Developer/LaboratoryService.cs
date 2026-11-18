using System.Collections.Concurrent;
using System.Collections.Immutable;
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.DevConsole;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Catalog;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Observation;

namespace Sts2Mod.Developer;

/// <summary>
/// Owns <c>/operator/eval/scenario</c> one-shot combat laboratory construction in headless developer sessions.
/// </summary>
internal sealed class LaboratoryService(
    bool enabled,
    AgentControlState control,
    GameCatalogProvider catalog,
    Func<NativeStepStatus> readStepStatus)
{
    private sealed record MainThreadWork(Action Run, TaskCompletionSource Completion);

    private readonly object _admissionLock = new();
    private readonly ConcurrentQueue<MainThreadWork> _workQueue = new();
    private DevConsole? _console;
    private volatile bool _active;
    private volatile bool _ownsInput;

    public bool OwnsInput => _ownsInput;

    public void Advance()
    {
        while (_workQueue.TryDequeue(out var item))
        {
            try
            {
                item.Run();
                item.Completion.TrySetResult();
            }
            catch (Exception error)
            {
                item.Completion.TrySetException(error);
            }
        }
    }

    public async Task<(int StatusCode, string Json)> PrepareScenarioAsync(string body, bool checkpointBusy)
    {
        CombatEvalScenario recipe;
        try
        {
            recipe = JsonContract.DeserializeUnique<CombatEvalScenario>(body) ?? throw new JsonException();
        }
        catch (JsonException)
        {
            return Reply(
                200,
                LaboratoryOutcome.Rejected,
                LaboratoryErrorCode.InvalidRecipe,
                "The request must contain exactly deck, relics, potions, and encounter.");
        }

        if (!enabled)
        {
            return Reply(
                200,
                LaboratoryOutcome.Rejected,
                LaboratoryErrorCode.DevelopmentDisabled,
                "Laboratory setup is disabled for this launch.");
        }

        try
        {
            recipe.Validate();
        }
        catch (ArgumentException error)
        {
            return Reply(200, LaboratoryOutcome.Rejected, LaboratoryErrorCode.InvalidRecipe, error.Message);
        }

        long expectedRunInstanceId;
        IReadOnlyList<CombatEvalStep> steps;
        lock (_admissionLock)
        {
            var status = readStepStatus();
            var current = ObservationHub.Current.Reply as CompletedStateRead;
            if (_active
                || _ownsInput
                || checkpointBusy
                || control.Mode != AgentControlMode.Paused
                || status.InputReadiness != NativeInputReadiness.Ready
                || status.Receipt is PendingStep or UnknownStep
                || status.Presentation is { Gate: PresentationGate.Locked }
                || status.Location.Owner != ObservationOwner.Run
                || status.Location.RunId != $"run-{ObservationHub.CurrentRunInstanceId}"
                || current?.Observation.State is not RunStateView { Screen: RunScreen.Map }
                || current.Observation.InputReadiness != NativeInputReadiness.Ready)
            {
                return Reply(
                    200,
                    LaboratoryOutcome.Rejected,
                    LaboratoryErrorCode.NativeBusy,
                    "Laboratory setup requires a ready, paused single-player map without pending input.");
            }

            LaboratoryPlayerState? initialState = null;
            try
            {
                initialState = CapturePlayerStateOnGameThreadAsync().GetAwaiter().GetResult();
                if (initialState is null)
                {
                    return Reply(
                        200,
                        LaboratoryOutcome.Rejected,
                        LaboratoryErrorCode.NativeBusy,
                        "Native run state is unavailable for laboratory setup.");
                }
                steps = recipe.Prepare(initialState);
            }
            catch (ArgumentException error)
            {
                return Reply(200, LaboratoryOutcome.Rejected, LaboratoryErrorCode.InvalidRecipe, error.Message);
            }

            expectedRunInstanceId = ObservationHub.CurrentRunInstanceId;
            _active = true;
            _ownsInput = true;
        }

        var mutated = false;
        NativeObservation? prepared = null;
        try
        {
            var valid = await catalog.ValidateScenarioAsync(recipe)
                .WaitAsync(TimeSpan.FromSeconds(30))
                .ConfigureAwait(false);
            if (!valid)
            {
                return Reply(
                    200,
                    LaboratoryOutcome.Rejected,
                    LaboratoryErrorCode.InvalidRecipe,
                    "A recipe definition ID is missing from its native object category; no native step was started.");
            }

            foreach (var step in steps)
            {
                mutated = true;
                if (!await ExecuteStepAsync(step, expectedRunInstanceId, waitReady: true).ConfigureAwait(false))
                    return Unknown(LaboratoryErrorCode.NativeIncomplete, prepared);
            }

            var midState = await CapturePlayerStateOnGameThreadAsync().ConfigureAwait(false);
            if (midState is null
                || ObservationHub.CurrentRunInstanceId != expectedRunInstanceId
                || !recipe.MatchesResources(midState))
            {
                return Unknown(LaboratoryErrorCode.NativeConflict, prepared);
            }

            var fill = recipe.FillHealth(midState);
            if (!await ExecuteStepAsync(fill, expectedRunInstanceId, waitReady: true).ConfigureAwait(false))
                return Unknown(LaboratoryErrorCode.NativeIncomplete, prepared);

            prepared = await WaitSettledObservationAsync(expectedRunInstanceId, ObservationOwner.Run).ConfigureAwait(false);
            var afterFill = await CapturePlayerStateOnGameThreadAsync().ConfigureAwait(false);
            if (prepared is null || afterFill is null || !recipe.MatchesResources(afterFill))
                return Unknown(LaboratoryErrorCode.NativeConflict, prepared);

            if (!await ExecuteStepAsync(recipe.Fight(), expectedRunInstanceId, waitReady: false).ConfigureAwait(false))
                return Unknown(LaboratoryErrorCode.NativeIncomplete, prepared);

            var combatObservation = await WaitSettledObservationAsync(expectedRunInstanceId, ObservationOwner.Combat).ConfigureAwait(false);
            var finalState = await CapturePlayerStateOnGameThreadAsync().ConfigureAwait(false);
            if (combatObservation is null
                || finalState is null
                || ObservationHub.CurrentRunInstanceId != expectedRunInstanceId
                || !recipe.MatchesResources(finalState)
                || finalState.EncounterId != recipe.Encounter)
            {
                return Unknown(LaboratoryErrorCode.NativeConflict, prepared);
            }

            _ownsInput = false;
            return (200, JsonContract.Serialize(new LaboratoryReply(
                LaboratoryOutcome.Completed,
                Error: null,
                PreparedObservation: prepared,
                Observation: combatObservation)));
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Laboratory scenario setup failed: {error}");
            if (!mutated)
            {
                _ownsInput = false;
                _active = false;
                return Reply(
                    200,
                    LaboratoryOutcome.Rejected,
                    LaboratoryErrorCode.InvalidRecipe,
                    "Laboratory validation failed before native mutation.");
            }
            return Unknown(LaboratoryErrorCode.NativeIncomplete, prepared);
        }
        finally
        {
            if (!mutated)
                _ownsInput = false;
        }
    }

    private async Task<bool> ExecuteStepAsync(CombatEvalStep step, long expectedRunInstanceId, bool waitReady)
    {
        Task? commandTask = null;
        string? failure = null;
        await RunOnGameThreadAsync(() =>
        {
            _console ??= new DevConsole(shouldAllowDebugCommands: true);
            var result = _console.ProcessCommand(step.Command);
            if (!result.success)
                failure = result.msg;
            else
                commandTask = result.task;
        }).ConfigureAwait(false);

        if (failure is not null)
        {
            GD.PrintErr($"[sts2agent] Laboratory console command rejected '{step.Command}': {failure}");
            return false;
        }

        if (commandTask is not null)
            await commandTask.WaitAsync(TimeSpan.FromSeconds(30)).ConfigureAwait(false);

        var deadline = DateTimeOffset.UtcNow.AddSeconds(20);
        while (DateTimeOffset.UtcNow < deadline)
        {
            if (ObservationHub.CurrentRunInstanceId != expectedRunInstanceId)
                return false;

            var state = await CapturePlayerStateOnGameThreadAsync().ConfigureAwait(false);
            if (state is not null
                && step.IsComplete(state)
                && (!waitReady || (ObservationHub.Current.Reply is CompletedStateRead completed
                                   && completed.Observation.InputReadiness == NativeInputReadiness.Ready)))
            {
                return true;
            }
            await Task.Delay(25).ConfigureAwait(false);
        }
        return false;
    }

    private static async Task<NativeObservation?> WaitSettledObservationAsync(
        long expectedRunInstanceId,
        ObservationOwner expectedOwner)
    {
        var deadline = DateTimeOffset.UtcNow.AddSeconds(25);
        while (DateTimeOffset.UtcNow < deadline)
        {
            if (ObservationHub.CurrentRunInstanceId != expectedRunInstanceId)
                return null;

            if (ObservationHub.Current.Reply is CompletedStateRead completed
                && completed.Observation.Location.Owner == expectedOwner
                && completed.Observation.InputReadiness is NativeInputReadiness.Ready or NativeInputReadiness.Selection)
            {
                return completed.Observation;
            }
            await Task.Delay(25).ConfigureAwait(false);
        }
        return null;
    }

    private async Task<LaboratoryPlayerState?> CapturePlayerStateOnGameThreadAsync()
    {
        LaboratoryPlayerState? captured = null;
        await RunOnGameThreadAsync(() =>
        {
            var run = RunManager.Instance.DebugOnlyGetState();
            if (run is null || run.Players.Count == 0)
                return;

            var player = run.Players[0];
            var combat = CombatManager.Instance.IsInProgress ? CombatManager.Instance.DebugOnlyGetState() : null;
            var currentObs = ObservationHub.Current.Reply as CompletedStateRead;
            var isMapReady = currentObs?.Observation.State is RunStateView { Screen: RunScreen.Map } mapState
                             && mapState.Actions.Any(action => action.Enabled && action.Destination is not null);

            captured = new LaboratoryPlayerState(
                run.Players.Count,
                player.Creature.CurrentHp,
                player.Creature.MaxHp,
                player.PotionSlots.Count,
                isMapReady,
                combat?.Encounter?.Id.ToString(),
                combat is not null ? ObservationPhase.Combat : ObservationPhase.Run,
                player.Deck.Cards.Select(card => new LaboratoryCardState(
                    card.Id.ToString(),
                    card.IsUpgraded,
                    card.Enchantment?.Id.ToString())).ToImmutableArray(),
                player.Relics.Select(relic => relic.Id.ToString()).ToImmutableArray(),
                player.Potions.Select(potion => potion.Id.ToString()).ToImmutableArray());
        }).ConfigureAwait(false);
        return captured;
    }

    private Task RunOnGameThreadAsync(Action action)
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        _workQueue.Enqueue(new MainThreadWork(action, tcs));
        return tcs.Task;
    }

    private static (int StatusCode, string Json) Reply(
        int statusCode,
        LaboratoryOutcome outcome,
        LaboratoryErrorCode code,
        string message) =>
        (statusCode, JsonContract.Serialize(new LaboratoryReply(
            outcome,
            new LaboratoryError(code, message),
            PreparedObservation: null,
            Observation: null)));

    private static (int StatusCode, string Json) Unknown(
        LaboratoryErrorCode code,
        NativeObservation? prepared) =>
        (200, JsonContract.Serialize(new LaboratoryReply(
            LaboratoryOutcome.Unknown,
            new LaboratoryError(
                code,
                "Laboratory setup did not settle cleanly; keep the instance paused and discard it."),
            PreparedObservation: prepared,
            Observation: null)));
}
