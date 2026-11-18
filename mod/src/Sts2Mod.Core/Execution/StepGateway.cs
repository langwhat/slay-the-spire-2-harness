using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Execution;

/// <summary>
/// Owns admission, settlement, and receipt publication for a single native gameplay step or map presentation.
/// </summary>
public sealed class StepGateway(AgentControlState control, string gameInstanceId, object? inputAdmission = null)
{
    private readonly object _sync = inputAdmission ?? new();
    private StepRequest? _pending;
    private bool _dispatching;
    private Task? _nativeCompletion;
    private Func<bool>? _nativeInputBoundary;
    private Func<bool>? _nativeAcceptance;
    private bool _nativeAccepted;
    private long? _nativeCompletionObservedSequence;
    private StepReceipt? _receipt;
    private long? _dispatchedSequence;
    private long _latestObservationSequence = -1;
    private long _uncertainThroughSequence = -1;
    private bool _awaitingObservationAfterUncertain;
    private StepLocation _location = new(gameInstanceId, null, null, null);
    private NativeInputReadiness _readiness = NativeInputReadiness.Unavailable;
    private int? _ascension;
    private PresentationStatus? _presentation;
    private StepRoute? _presentationRoute;
    private Task? _presentationWorker;
    private Task? _presentationVerifier;
    private bool _presentationIdentityLost;
    private Func<bool>? _externalInputBusy;

    public StepLocation AbsentLocation => new(gameInstanceId, null, null, null);

    public PresentationStatus? Presentation
    {
        get
        {
            lock (_sync)
                return _presentation;
        }
    }

    public void SetExternalInputBusyCheck(Func<bool> externalInputBusy)
    {
        lock (_sync)
            _externalInputBusy = externalInputBusy;
    }

    public StepCause? BeginPresentation(string operationId, StepRoute route, Func<StepCause?> capture)
    {
        lock (_sync)
        {
            if (_receipt?.OperationId == operationId || _presentation?.OperationId == operationId)
                return new(StepErrorCode.DuplicateOperation, "This operation ID is already current.");
            if (IsInputOwnedLocked())
                return new(StepErrorCode.NativeBusy, "A native operation still owns input.");
            if (route.Owner != ObservationOwner.Run)
                return new(StepErrorCode.WrongOwner, "Map presentation requires the run owner.");
            if (_location != new StepLocation(route.GameInstanceId, route.RunId, route.NodeId, route.Owner))
                return new RouteChangedCause("The native route changed before presentation.", _location);
            if (!control.IsRunning)
                return new(StepErrorCode.ActionUnavailable, "Map presentation admission failed: agent control is paused.");
            if (_readiness is not (NativeInputReadiness.Ready or NativeInputReadiness.Selection))
                return new(StepErrorCode.ActionUnavailable, "Map presentation admission failed: native input is not ready.");
            if (capture() is { } captureError)
                return captureError;
            if (IsInputOwnedLocked())
                return new(StepErrorCode.NativeBusy, "A native operation still owns input.");
            if (_location != new StepLocation(route.GameInstanceId, route.RunId, route.NodeId, route.Owner))
                return new RouteChangedCause("The native route changed before presentation.", _location);

            _presentation = new(
                operationId,
                PresentationPhase.Running,
                PresentationDrawing.Pending,
                PresentationRestoration.Pending,
                PresentationGate.Locked);
            _presentationRoute = route;
            _presentationWorker = null;
            _presentationVerifier = null;
            _presentationIdentityLost = false;
            return null;
        }
    }

    public void TrackPresentationWorker(string operationId, Task worker)
    {
        lock (_sync)
        {
            if (_presentation is not { Phase: PresentationPhase.Running } current
                || current.OperationId != operationId
                || _presentationWorker is not null)
            {
                throw new InvalidOperationException("The presentation worker must own the current running presentation.");
            }
            _presentationWorker = worker;
        }
    }

    public bool SettlePresentation(string operationId, PresentationDrawing drawing, PresentationRestoration restoration)
    {
        lock (_sync)
        {
            if (_presentation is not { Phase: PresentationPhase.Running } current
                || current.OperationId != operationId
                || _presentationWorker is not { IsCompleted: true })
            {
                return false;
            }
            if (drawing == PresentationDrawing.Pending || restoration == PresentationRestoration.Pending)
                throw new ArgumentException("A settled presentation requires terminal outcomes.");
            if (!_presentationWorker.IsCompletedSuccessfully)
            {
                drawing = PresentationDrawing.Unknown;
                restoration = PresentationRestoration.Unknown;
            }
            if (!PresentationLocationMatches())
                restoration = PresentationRestoration.Unknown;
            _presentation = FinishedPresentation(current, drawing, restoration);
            return true;
        }
    }

    public StepCause? BeginPresentationVerification(string operationId, StepRoute route)
    {
        lock (_sync)
        {
            if (_presentation is { Phase: PresentationPhase.Running or PresentationPhase.Verifying }
                || _externalInputBusy?.Invoke() == true)
            {
                return new(StepErrorCode.NativeBusy, "A native operation still owns input.");
            }
            if (route.Owner != ObservationOwner.Run)
                return new(StepErrorCode.WrongOwner, "Map verification requires the run owner.");
            if (_presentation is not { Phase: PresentationPhase.SettledUnverified } current
                || current.OperationId != operationId
                || _presentationRoute != route)
            {
                return new(StepErrorCode.ActionUnavailable, "No matching presentation recovery is available.");
            }
            if (!PresentationLocationMatches())
                return new RouteChangedCause("The native route changed before verification.", _location);

            _presentation = current with
            {
                Phase = PresentationPhase.Verifying,
                Restoration = PresentationRestoration.Pending,
            };
            _presentationVerifier = null;
            return null;
        }
    }

    public void TrackPresentationVerifier(string operationId, Task verifier)
    {
        lock (_sync)
        {
            if (_presentation is not { Phase: PresentationPhase.Verifying } current
                || current.OperationId != operationId
                || _presentationVerifier is not null)
            {
                throw new InvalidOperationException("The verifier must own the current presentation verification.");
            }
            _presentationVerifier = verifier;
        }
    }

    public bool FinishPresentationVerification(string operationId, PresentationRestoration restoration)
    {
        lock (_sync)
        {
            if (_presentation is not { Phase: PresentationPhase.Verifying } current
                || current.OperationId != operationId
                || _presentationVerifier is not { IsCompleted: true })
            {
                return false;
            }
            if (restoration == PresentationRestoration.Pending)
                throw new ArgumentException("A settled verification requires a terminal outcome.");
            if (!_presentationVerifier.IsCompletedSuccessfully || !PresentationLocationMatches())
                restoration = PresentationRestoration.Unknown;
            _presentation = FinishedPresentation(current, current.Drawing, restoration);
            return true;
        }
    }

    public bool IsPresentationIdentityCurrent(string operationId)
    {
        lock (_sync)
            return _presentation?.OperationId == operationId && PresentationLocationMatches();
    }

    public HttpReply Submit(string body)
    {
        StepRequest request;
        try
        {
            request = Decode(body);
        }
        catch (JsonException)
        {
            return new HttpReply(400, InvalidRequestReply.Json(
                "The request does not match the single-step action contract.",
                "operation_id",
                "expected_route",
                "step"));
        }

        return new HttpReply(200, JsonContract.Serialize(SubmitReceipt(request)));
    }

    public NativeStepStatus Status()
    {
        lock (_sync)
        {
            var readiness = _pending is not null
                            || _awaitingObservationAfterUncertain
                            || _presentation is { Phase: PresentationPhase.Running or PresentationPhase.Verifying }
                            || _externalInputBusy?.Invoke() == true
                ? NativeInputReadiness.Busy
                : _presentation is { Phase: PresentationPhase.SettledUnverified }
                    ? NativeInputReadiness.Unavailable
                    : _readiness;

            var receipt = _awaitingObservationAfterUncertain && _receipt is { } uncertain
                ? new PendingStep(uncertain.OperationId)
                : _receipt;
            return new(_location, control.Mode, readiness, receipt)
            {
                Ascension = _ascension,
                Presentation = _pending is null ? _presentation : null,
            };
        }
    }

    public NativeReadPublication Project(NativeReadPublication publication)
    {
        lock (_sync)
        {
            if (publication.Reply is CompletedStateRead published
                && (_awaitingObservationAfterUncertain
                    || published.Observation.Stamp.ObservationSequence <= _uncertainThroughSequence))
            {
                return publication with
                {
                    Reply = new FailedStateRead(new ReadCause(
                        NativeReadError.ReadUnavailable,
                        "Waiting for a trusted native observation after an uncertain step.")),
                };
            }

            var externalBusy = _externalInputBusy?.Invoke() == true;
            if ((_pending is null && _presentation is not { Gate: PresentationGate.Locked } && !externalBusy)
                || publication.Reply is not CompletedStateRead completed)
            {
                return publication;
            }

            var observation = completed.Observation;
            if (observation.State is TerminalStateView)
            {
                return publication with
                {
                    Reply = new FailedStateRead(new ReadCause(
                        NativeReadError.ReadUnavailable,
                        "The native terminal transition is still settling.")),
                };
            }

            var busy = _pending is not null
                       || _presentation is { Phase: PresentationPhase.Running or PresentationPhase.Verifying }
                       || externalBusy;
            var input = busy ? NativeInputReadiness.Busy : NativeInputReadiness.Unavailable;
            var publicControl = busy ? PublicInputControl.Busy : PublicInputControl.Unavailable;
            IObservedState state = observation.State switch
            {
                RunStateView run => run with { Control = publicControl },
                CombatStateView combat => combat with { Control = publicControl },
                _ => throw new InvalidOperationException("Unsupported observed state."),
            };

            return publication with
            {
                Reply = new CompletedStateRead(observation with
                {
                    InputReadiness = input,
                    State = state,
                }),
            };
        }
    }

    public void Advance(
        StepLocation location,
        NativeInputReadiness readiness,
        long sequence,
        Func<StepRequest, StepReceipt?> dispatch,
        int? ascension = null)
    {
        lock (_sync)
        {
            _location = location;
            if (_presentation is { Gate: PresentationGate.Locked } && !PresentationLocationMatches())
                _presentationIdentityLost = true;
            if (_presentation is { Phase: PresentationPhase.Restored } && !PresentationLocationMatches())
                ClearPresentation();
            if (_presentation is { Phase: PresentationPhase.SettledUnverified }
                && _presentationWorker is { IsCompleted: true }
                && _presentationRoute is { } oldRoute
                && location.RunId is not null && location.RunId != oldRoute.RunId
                && sequence >= 0)
            {
                ClearPresentation();
            }
            _ascension = location.RunId is null ? null : ascension;
            _readiness = readiness;
            _latestObservationSequence = Math.Max(sequence, _latestObservationSequence);
            if (_awaitingObservationAfterUncertain
                && sequence > _uncertainThroughSequence
                && readiness is NativeInputReadiness.Ready or NativeInputReadiness.Selection or NativeInputReadiness.Terminal)
            {
                _awaitingObservationAfterUncertain = false;
            }

            if (_dispatching || _pending is not { } request)
                return;

            if (_dispatchedSequence is { } dispatched)
            {
                _nativeAccepted |= _nativeAcceptance?.Invoke() == true;
                if (_nativeCompletion is { } completion)
                {
                    if (completion.IsCanceled || completion.Exception is not null)
                    {
                        ReportUncertain();
                        return;
                    }

                    var inputBoundary = readiness == NativeInputReadiness.Ready
                                        && _nativeInputBoundary?.Invoke() == true;
                    if (inputBoundary)
                        _nativeAccepted = true;

                    if (readiness != NativeInputReadiness.Selection && !inputBoundary)
                    {
                        if (!completion.IsCompletedSuccessfully)
                        {
                            _readiness = NativeInputReadiness.Busy;
                            return;
                        }
                        _nativeAccepted = true;
                        if (readiness != NativeInputReadiness.Terminal)
                        {
                            _nativeCompletionObservedSequence ??= sequence;
                            if (sequence <= _nativeCompletionObservedSequence)
                                return;
                        }
                    }
                }

                if (sequence <= dispatched || readiness is NativeInputReadiness.Busy or NativeInputReadiness.Unavailable)
                    return;

                if (!_nativeAccepted && readiness != NativeInputReadiness.Selection)
                {
                    ReportUncertain();
                    return;
                }

                _receipt = new CompletedStep(request.OperationId, readiness switch
                {
                    NativeInputReadiness.Selection => StepBoundary.Selection,
                    NativeInputReadiness.Terminal => StepBoundary.Terminal,
                    _ => StepBoundary.Ready,
                });
                _pending = null;
                return;
            }

            var expected = request.ExpectedRoute;
            if (!control.IsRunning)
            {
                _receipt = Reject(request, StepErrorCode.ActionUnavailable, "Agent control is paused.");
            }
            else if (location != new StepLocation(expected.GameInstanceId, expected.RunId, expected.NodeId, expected.Owner))
            {
                _receipt = new RejectedStep(
                    request.OperationId,
                    new RouteChangedCause("The native route changed before dispatch.", location));
            }
            else if (_externalInputBusy?.Invoke() == true
                     || readiness is NativeInputReadiness.Busy or NativeInputReadiness.Unavailable)
            {
                _receipt = Reject(request, StepErrorCode.NativeBusy, "Native input is not ready.");
            }
            else if (readiness == NativeInputReadiness.Selection && request.Step.Kind != NativeStepKind.Select)
            {
                _receipt = Reject(request, StepErrorCode.SelectionRequired, "Complete the current native selection first.");
            }
            else
            {
                StepReceipt? result;
                _dispatching = true;
                try
                {
                    result = dispatch(request);
                }
                finally
                {
                    _dispatching = false;
                }

                if (result is null)
                {
                    _presentation = null;
                    _dispatchedSequence = sequence;
                    return;
                }

                if (result is not RejectedStep)
                    _presentation = null;
                _receipt = result;
                if (result is UnknownStep)
                    WaitForObservationAfterUncertain();
            }

            _pending = null;
        }
    }

    public void TrackNativeTask(StepRequest request, Task completion, Func<bool>? inputBoundary = null)
    {
        lock (_sync)
        {
            if (!_dispatching || !ReferenceEquals(_pending, request))
                throw new InvalidOperationException("A native task must belong to the currently dispatching operation.");
            _nativeCompletion = completion;
            _nativeInputBoundary = inputBoundary;
        }
    }

    public void TrackNativeAcceptance(StepRequest request, Func<bool> accepted)
    {
        lock (_sync)
        {
            if (!_dispatching || !ReferenceEquals(_pending, request))
                throw new InvalidOperationException("Native acceptance must belong to the currently dispatching operation.");
            _nativeAcceptance = accepted;
        }
    }

    public void RejectPendingBeforeTouch(string operationId, StepErrorCode code, string message)
    {
        lock (_sync)
        {
            if (_pending is not { } request || request.OperationId != operationId)
                return;
            _receipt = Reject(request, code, message);
            _pending = null;
        }
    }

    public void ReportUncertain(string? operationId = null)
    {
        lock (_sync)
        {
            if (_pending is not { } request || (operationId is not null && request.OperationId != operationId))
                return;
            WaitForObservationAfterUncertain();
            _receipt = new UnknownStep(
                request.OperationId,
                new StepCause(
                    StepErrorCode.ExecutionUncertain,
                    "The native step may have affected the game. Read the current state before making a new decision."));
            _pending = null;
        }
    }

    public void ClearReceiptOnRunReset()
    {
        lock (_sync)
        {
            if (_pending is not null)
                return;
            _receipt = null;
            _awaitingObservationAfterUncertain = false;
            if (_presentation is not { Gate: PresentationGate.Locked })
                ClearPresentation();
        }
    }

    private void ClearPresentation()
    {
        _presentation = null;
        _presentationRoute = null;
        _presentationWorker = null;
        _presentationVerifier = null;
        _presentationIdentityLost = false;
    }

    private void WaitForObservationAfterUncertain()
    {
        _uncertainThroughSequence = _latestObservationSequence;
        _awaitingObservationAfterUncertain = true;
    }

    public static RejectedStep Reject(StepRequest request, StepErrorCode code, string message) =>
        new(request.OperationId, new StepCause(code, message));

    private bool IsInputOwnedLocked() =>
        _pending is not null
        || _receipt is UnknownStep
        || _presentation is { Gate: PresentationGate.Locked }
        || _externalInputBusy?.Invoke() == true;

    private StepReceipt SubmitReceipt(StepRequest request)
    {
        lock (_sync)
        {
            if (_receipt?.OperationId == request.OperationId || _presentation?.OperationId == request.OperationId)
                return Reject(request, StepErrorCode.DuplicateOperation, "This operation ID is already current.");
            if (_pending is not null || _awaitingObservationAfterUncertain || _externalInputBusy?.Invoke() == true)
                return Reject(request, StepErrorCode.NativeBusy, "A native operation still owns input.");
            if (_presentation is { Gate: PresentationGate.Locked })
                return Reject(request, StepErrorCode.NativeBusy, "A map presentation still owns input.");
            if (!control.IsRunning)
                return Reject(request, StepErrorCode.ActionUnavailable, "Agent control is paused.");

            _pending = request;
            _dispatchedSequence = null;
            _nativeCompletion = null;
            _nativeInputBoundary = null;
            _nativeAcceptance = null;
            _nativeAccepted = request.Step is not InteractStep;
            _nativeCompletionObservedSequence = null;
            return _receipt = new PendingStep(request.OperationId);
        }
    }

    private bool PresentationLocationMatches() =>
        !_presentationIdentityLost
        && _presentationRoute is { } route
        && _location == new StepLocation(route.GameInstanceId, route.RunId, route.NodeId, route.Owner);

    private static PresentationStatus FinishedPresentation(
        PresentationStatus current,
        PresentationDrawing drawing,
        PresentationRestoration restoration) =>
        restoration is PresentationRestoration.Restored or PresentationRestoration.NotNeeded
            ? current with
            {
                Phase = PresentationPhase.Restored,
                Drawing = drawing,
                Restoration = restoration,
                Gate = PresentationGate.Open,
            }
            : current with
            {
                Phase = PresentationPhase.SettledUnverified,
                Drawing = drawing,
                Restoration = restoration,
                Gate = PresentationGate.Locked,
            };

    private static StepRequest Decode(string body)
    {
        JsonContract.EnsureNoDuplicateProperties(body);
        using var document = JsonDocument.Parse(body);
        var root = document.RootElement;
        JsonContract.RequireKeys(root, ["operation_id", "expected_route", "step"]);
        var expectedRoute = StepRoute.ParseJson(root.GetProperty("expected_route"));

        var raw = root.GetProperty("step");
        var kind = JsonContract.RequireText(raw, "kind") switch
        {
            "interact" => NativeStepKind.Interact,
            "play_card" => NativeStepKind.PlayCard,
            "use_potion" => NativeStepKind.UsePotion,
            "discard_potion" => NativeStepKind.DiscardPotion,
            "end_turn" => NativeStepKind.EndTurn,
            "select" => NativeStepKind.Select,
            _ => throw new JsonException(),
        };

        var subject = kind switch
        {
            NativeStepKind.Interact => "action",
            NativeStepKind.PlayCard => "card",
            NativeStepKind.UsePotion or NativeStepKind.DiscardPotion => "potion",
            _ => null,
        };

        string[] required = subject is not null
            ? ["kind", subject]
            : kind == NativeStepKind.Select
                ? ["kind", "refs"]
                : ["kind"];
        string[] optional = kind is NativeStepKind.PlayCard or NativeStepKind.UsePotion ? ["target"] : [];
        JsonContract.RequireKeys(raw, required, optional);

        var refs = ImmutableArray<string>.Empty;
        if (kind == NativeStepKind.Select)
        {
            var items = raw.GetProperty("refs");
            if (items.ValueKind != JsonValueKind.Array)
                throw new JsonException();
            refs = items.EnumerateArray().Select(JsonContract.RequireNonempty).ToImmutableArray();
            if (refs.Distinct(StringComparer.Ordinal).Count() != refs.Length)
                throw new JsonException();
        }

        var subjectRef = subject is null ? null : JsonContract.RequireText(raw, subject);
        var targetRef = raw.TryGetProperty("target", out var target) ? JsonContract.RequireNonempty(target) : null;

        NativeStep step = kind switch
        {
            NativeStepKind.Interact => new InteractStep(subjectRef!),
            NativeStepKind.PlayCard => new PlayCardStep(subjectRef!, targetRef),
            NativeStepKind.UsePotion => new UsePotionStep(subjectRef!, targetRef),
            NativeStepKind.DiscardPotion => new DiscardPotionStep(subjectRef!),
            NativeStepKind.EndTurn => new EndTurnStep(),
            NativeStepKind.Select => new SelectStep(refs),
            _ => throw new JsonException(),
        };

        return new(
            JsonContract.RequireText(root, "operation_id"),
            expectedRoute,
            step);
    }
}
