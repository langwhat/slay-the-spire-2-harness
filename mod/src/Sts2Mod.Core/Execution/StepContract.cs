using System.Collections.Immutable;
using System.Text.Json;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Execution;

public enum NativeStepKind
{
    Interact,
    PlayCard,
    UsePotion,
    DiscardPotion,
    EndTurn,
    Select,
    RerollCardReward,
}

public enum StepOutcome
{
    Pending,
    Completed,
    Rejected,
    Failed,
    Unknown,
}

public enum StepBoundary
{
    Ready,
    Selection,
    Terminal,
}

public enum StepErrorCode
{
    InvalidRequest,
    RouteChanged,
    NativeBusy,
    WrongOwner,
    ActionUnavailable,
    SelectionRequired,
    SelectionUnavailable,
    InvalidRefs,
    InvalidTarget,
    InvalidSelection,
    DuplicateOperation,
    DispatchFailed,
    IdentityUnregistered,
    ExecutionUncertain,
}

public sealed record StepRoute(
    string GameInstanceId,
    string RunId,
    string NodeId,
    ObservationOwner Owner)
{
    public static StepRoute ParseJson(JsonElement route)
    {
        JsonContract.RequireKeys(route, ["game_instance_id", "run_id", "node_id", "owner"]);
        var owner = JsonContract.RequireText(route, "owner") switch
        {
            "run" => ObservationOwner.Run,
            "combat" => ObservationOwner.Combat,
            _ => throw new JsonException("Invalid route owner."),
        };
        return new(
            JsonContract.RequireText(route, "game_instance_id"),
            JsonContract.RequireText(route, "run_id"),
            JsonContract.RequireText(route, "node_id"),
            owner);
    }
}

public sealed record StepLocation(
    string GameInstanceId,
    string? RunId,
    string? NodeId,
    ObservationOwner? Owner);

public abstract record NativeStep(NativeStepKind Kind);

public sealed record InteractStep(string Action) : NativeStep(NativeStepKind.Interact);

public sealed record PlayCardStep(string Card, string? Target) : NativeStep(NativeStepKind.PlayCard);

public sealed record UsePotionStep(string Potion, string? Target) : NativeStep(NativeStepKind.UsePotion);

public sealed record DiscardPotionStep(string Potion) : NativeStep(NativeStepKind.DiscardPotion);

public sealed record EndTurnStep() : NativeStep(NativeStepKind.EndTurn);

public sealed record SelectStep(ImmutableArray<string> Refs) : NativeStep(NativeStepKind.Select);

public sealed record RerollCardRewardStep() : NativeStep(NativeStepKind.RerollCardReward);

public sealed record StepRequest(string OperationId, StepRoute ExpectedRoute, NativeStep Step);

[JsonDerivedType(typeof(RouteChangedCause))]
[JsonDerivedType(typeof(InvalidStepRefs))]
[JsonDerivedType(typeof(InvalidStepTarget))]
[JsonDerivedType(typeof(InvalidStepSelection))]
public record StepCause(StepErrorCode Code, string Message);

public sealed record RouteChangedCause(string Message, StepLocation ActualLocation)
    : StepCause(StepErrorCode.RouteChanged, Message);

public sealed record InvalidStepRefs(string Message, ImmutableArray<string> InvalidRefs)
    : StepCause(StepErrorCode.InvalidRefs, Message);

public sealed record InvalidStepTarget(string Message, string? Target, ImmutableArray<string> LegalTargets)
    : StepCause(StepErrorCode.InvalidTarget, Message);

public sealed record InvalidStepSelection(string Message, int Min, int Max)
    : StepCause(StepErrorCode.InvalidSelection, Message);

[JsonDerivedType(typeof(PendingStep))]
[JsonDerivedType(typeof(CompletedStep))]
[JsonDerivedType(typeof(RejectedStep))]
[JsonDerivedType(typeof(FailedStep))]
[JsonDerivedType(typeof(UnknownStep))]
public abstract record StepReceipt(string OperationId, StepOutcome Status);

public sealed record PendingStep(string OperationId) : StepReceipt(OperationId, StepOutcome.Pending);

public sealed record CompletedStep(string OperationId, StepBoundary Boundary)
    : StepReceipt(OperationId, StepOutcome.Completed);

public sealed record RejectedStep(string OperationId, StepCause Error)
    : StepReceipt(OperationId, StepOutcome.Rejected);

public sealed record FailedStep(string OperationId, StepCause Error)
    : StepReceipt(OperationId, StepOutcome.Failed);

public sealed record UnknownStep(string OperationId, StepCause Error)
    : StepReceipt(OperationId, StepOutcome.Unknown);

public enum PresentationPhase
{
    Running,
    SettledUnverified,
    Verifying,
    Restored,
}

public enum PresentationDrawing
{
    Pending,
    Shown,
    Failed,
    Unknown,
}

public enum PresentationRestoration
{
    Pending,
    Restored,
    NotNeeded,
    Unknown,
    Failed,
}

public enum PresentationGate
{
    Locked,
    Open,
}

public sealed record PresentationStatus(
    string OperationId,
    PresentationPhase Phase,
    PresentationDrawing Drawing,
    PresentationRestoration Restoration,
    PresentationGate Gate);

public sealed record NativeStepStatus(
    StepLocation Location,
    AgentControlMode ControlIntent,
    NativeInputReadiness InputReadiness,
    StepReceipt? Receipt)
{
    public int? Ascension { get; init; }
    public PresentationStatus? Presentation { get; init; }
}
