using System.Collections.Concurrent;
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

public readonly record struct CardRewardAlternativeDescriptor(
    string OptionId,
    bool EndsSelectionWithoutCompletingReward,
    bool HasCustomOnSelect);

/// <summary>
/// Pure reversibility policy for automatic viewer card-reward previews.
/// </summary>
public static class RewardPreviewPolicy
{
    public static bool IsNonClaimingCloseAlternative(CardRewardAlternativeDescriptor alternative) =>
        string.Equals(alternative.OptionId, "SKIP", StringComparison.OrdinalIgnoreCase)
        && alternative.EndsSelectionWithoutCompletingReward
        && !alternative.HasCustomOnSelect;

    public static bool CanAutoPreview(
        int cardOfferCount,
        bool isStandardCardReward,
        bool canSkip,
        IReadOnlyList<CardRewardAlternativeDescriptor>? preflightAlternatives)
    {
        if (!isStandardCardReward)
            return false;
        if (cardOfferCount <= 1)
            return true;
        if (!canSkip || preflightAlternatives is null)
            return false;
        for (var i = 0; i < preflightAlternatives.Count; i++)
        {
            if (IsNonClaimingCloseAlternative(preflightAlternatives[i]))
                return true;
        }
        return false;
    }
}

public sealed record PendingRewardRequest(
    RewardOpenRequest Request,
    bool Close,
    long EnqueuedAt,
    TaskCompletionSource<HttpReply> Reply);

/// <summary>
/// Deadline-bounded queue for viewer card reward open/close requests so stale requests never mutate UI.
/// </summary>
public sealed class RewardOpenQueue(TimeProvider? timeProvider = null, TimeSpan? queueLimit = null)
{
    private static readonly TimeSpan DefaultQueueLimit = TimeSpan.FromSeconds(2);

    private readonly TimeProvider _clock = timeProvider ?? TimeProvider.System;
    private readonly TimeSpan _queueLimit = queueLimit ?? DefaultQueueLimit;
    private readonly ConcurrentQueue<PendingRewardRequest> _items = new();

    public Task<HttpReply> Enqueue(RewardOpenRequest request, bool close)
    {
        var reply = new TaskCompletionSource<HttpReply>(TaskCreationOptions.RunContinuationsAsynchronously);
        _items.Enqueue(new(request, close, _clock.GetTimestamp(), reply));
        return reply.Task;
    }

    public bool TryDequeueValid(out PendingRewardRequest item)
    {
        while (_items.TryDequeue(out var candidate))
        {
            if (_clock.GetElapsedTime(candidate.EnqueuedAt) >= _queueLimit)
            {
                candidate.Reply.TrySetResult(candidate.Close
                    ? CloseReplyJson(RewardCloseStatus.Skipped)
                    : OpenReplyJson(RewardOpenStatus.Skipped));
                continue;
            }
            item = candidate;
            return true;
        }
        item = null!;
        return false;
    }

    public void DrainAll()
    {
        while (_items.TryDequeue(out var pending))
        {
            pending.Reply.TrySetResult(pending.Close
                ? CloseReplyJson(RewardCloseStatus.Skipped)
                : OpenReplyJson(RewardOpenStatus.Skipped));
        }
    }

    public static HttpReply OpenReplyJson(RewardOpenStatus status) =>
        new(200, JsonContract.Serialize(new RewardOpenReply(status)));

    public static HttpReply CloseReplyJson(RewardCloseStatus status) =>
        new(200, JsonContract.Serialize(new RewardCloseReply(status)));
}

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

    public bool TryBegin(RewardOpenRequest request, out Task<RewardOpenStatus> completion, long? startedAt = null)
    {
        lock (_sync)
        {
            var origin = startedAt ?? _clock.GetTimestamp();
            if (_activeOpen is not null || _activeClose is not null || _clock.GetElapsedTime(origin) >= _settleLimit)
            {
                completion = Task.FromResult(RewardOpenStatus.Skipped);
                return false;
            }

            var source = new TaskCompletionSource<RewardOpenStatus>(TaskCreationOptions.RunContinuationsAsynchronously);
            _activeOpen = new(request, origin, source);
            completion = source.Task;
            return true;
        }
    }

    public bool TryBeginClose(RewardOpenRequest request, out Task<RewardCloseStatus> completion, long? startedAt = null)
    {
        lock (_sync)
        {
            var origin = startedAt ?? _clock.GetTimestamp();
            if (_activeOpen is not null || _activeClose is not null || _clock.GetElapsedTime(origin) >= _settleLimit)
            {
                completion = Task.FromResult(RewardCloseStatus.Skipped);
                return false;
            }

            var source = new TaskCompletionSource<RewardCloseStatus>(TaskCreationOptions.RunContinuationsAsynchronously);
            _activeClose = new(request, origin, source);
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
