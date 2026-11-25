using System.Text.Json;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Execution;

public enum RewardOpenStatus
{
    Opened,
    AlreadyOpen,
    Skipped,
}

public enum RewardCloseStatus
{
    Closed,
    AlreadyClosed,
    Skipped,
}

public sealed record RewardOpenRequest(StepRoute ExpectedRoute, string Offer);

public sealed record RewardOpenReply(RewardOpenStatus Status);

public sealed record RewardCloseReply(RewardCloseStatus Status);

public sealed class RewardOpenTransport(
    Func<RewardOpenRequest, Task<HttpReply>> open,
    Func<RewardOpenRequest, Task<HttpReply>> close)
{
    public Task<HttpReply> Open(string body) => Dispatch(body, open);

    public Task<HttpReply> Close(string body) => Dispatch(body, close);

    private static Task<HttpReply> Dispatch(string body, Func<RewardOpenRequest, Task<HttpReply>> handler)
    {
        try
        {
            JsonContract.EnsureNoDuplicateProperties(body);
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            JsonContract.RequireKeys(root, ["expected_route", "offer"]);
            var route = StepRoute.ParseJson(root.GetProperty("expected_route"));
            var offer = JsonContract.RequireText(root, "offer");
            return handler(new(route, offer));
        }
        catch (JsonException)
        {
            return Task.FromResult(new HttpReply(400, InvalidRequestReply.Json(
                "The request does not match the card reward presentation contract.",
                "expected_route",
                "offer")));
        }
    }
}

/// <summary>
/// Tracks a single viewer-only card reward open or close transition until native readiness settles.
/// </summary>
public sealed class RewardOpenTracker(TimeProvider? timeProvider = null, TimeSpan? settleLimit = null)
{
    private static readonly TimeSpan DefaultSettleLimit = TimeSpan.FromSeconds(3);

    private sealed record ActiveOpen(
        RewardOpenRequest Request,
        long StartedAt,
        TaskCompletionSource<RewardOpenStatus> Completion);

    private sealed record ActiveClose(
        RewardOpenRequest Request,
        long StartedAt,
        TaskCompletionSource<RewardCloseStatus> Completion);

    private readonly object _sync = new();
    private readonly TimeProvider _clock = timeProvider ?? TimeProvider.System;
    private readonly TimeSpan _settleLimit = settleLimit ?? DefaultSettleLimit;
    private ActiveOpen? _activeOpen;
    private ActiveClose? _activeClose;

    public bool IsBusy
    {
        get
        {
            lock (_sync)
                return _activeOpen is not null || _activeClose is not null;
        }
    }

    public bool TryBegin(RewardOpenRequest request, out Task<RewardOpenStatus> completion)
    {
        lock (_sync)
        {
            if (_activeOpen is not null || _activeClose is not null)
            {
                completion = Task.FromResult(RewardOpenStatus.Skipped);
                return false;
            }

            var source = new TaskCompletionSource<RewardOpenStatus>(TaskCreationOptions.RunContinuationsAsynchronously);
            _activeOpen = new(request, _clock.GetTimestamp(), source);
            completion = source.Task;
            return true;
        }
    }

    public bool TryBeginClose(RewardOpenRequest request, out Task<RewardCloseStatus> completion)
    {
        lock (_sync)
        {
            if (_activeOpen is not null || _activeClose is not null)
            {
                completion = Task.FromResult(RewardCloseStatus.Skipped);
                return false;
            }

            var source = new TaskCompletionSource<RewardCloseStatus>(TaskCreationOptions.RunContinuationsAsynchronously);
            _activeClose = new(request, _clock.GetTimestamp(), source);
            completion = source.Task;
            return true;
        }
    }

    public void ObserveFrame(
        StepLocation location,
        NativeInputReadiness readiness,
        bool isRunning,
        string? activeSelectionOfferRef,
        bool hasSelection = false,
        bool stillValid = true)
    {
        ActiveOpen? finishedOpen = null;
        var openOutcome = RewardOpenStatus.Skipped;
        ActiveClose? finishedClose = null;
        var closeOutcome = RewardCloseStatus.Skipped;

        lock (_sync)
        {
            if (_activeOpen is { } opening)
            {
                var expected = opening.Request.ExpectedRoute;
                var expectedLocation = new StepLocation(
                    expected.GameInstanceId,
                    expected.RunId,
                    expected.NodeId,
                    expected.Owner);

                if (location == expectedLocation
                    && readiness == NativeInputReadiness.Selection
                    && string.Equals(activeSelectionOfferRef, opening.Request.Offer, StringComparison.Ordinal))
                {
                    finishedOpen = opening;
                    openOutcome = RewardOpenStatus.Opened;
                    _activeOpen = null;
                }
                else if (location != expectedLocation
                         || !isRunning
                         || !stillValid
                         || _clock.GetElapsedTime(opening.StartedAt) >= _settleLimit)
                {
                    finishedOpen = opening;
                    openOutcome = RewardOpenStatus.Skipped;
                    _activeOpen = null;
                }
            }
            else if (_activeClose is { } closing)
            {
                var expected = closing.Request.ExpectedRoute;
                var expectedLocation = new StepLocation(
                    expected.GameInstanceId,
                    expected.RunId,
                    expected.NodeId,
                    expected.Owner);

                if (location == expectedLocation
                    && readiness == NativeInputReadiness.Ready
                    && !hasSelection)
                {
                    finishedClose = closing;
                    closeOutcome = RewardCloseStatus.Closed;
                    _activeClose = null;
                }
                else if (location != expectedLocation
                         || !isRunning
                         || !stillValid
                         || _clock.GetElapsedTime(closing.StartedAt) >= _settleLimit)
                {
                    finishedClose = closing;
                    closeOutcome = RewardCloseStatus.Skipped;
                    _activeClose = null;
                }
            }
        }

        finishedOpen?.Completion.TrySetResult(openOutcome);
        finishedClose?.Completion.TrySetResult(closeOutcome);
    }

    public void Complete(RewardOpenStatus status)
    {
        ActiveOpen? finished;
        lock (_sync)
        {
            finished = _activeOpen;
            _activeOpen = null;
        }
        finished?.Completion.TrySetResult(status);
    }

    public void CompleteClose(RewardCloseStatus status)
    {
        ActiveClose? finished;
        lock (_sync)
        {
            finished = _activeClose;
            _activeClose = null;
        }
        finished?.Completion.TrySetResult(status);
    }

    public void Reset()
    {
        Complete(RewardOpenStatus.Skipped);
        CompleteClose(RewardCloseStatus.Skipped);
    }
}
