"""Explicit wire projections of the closed historical execution result."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, assert_never

from pydantic import Field

from sts2_harness.client.models import WireModel
from sts2_harness.config import Protocol
from sts2_harness.game import OperationKind
from sts2_harness.journal import DependencyKind
from sts2_harness_eval import results as replay
from sts2_harness_eval.cases import IncompleteCause, Mismatch, Stage, Unsupported
from sts2_harness_eval.checkpoints import CheckpointCause
from sts2_harness_eval.sample import (
    AfterDecisions,
    BattleEnd,
    Completed,
    Incomplete,
    OperatorStopped,
    StopCause,
)
from sts2_harness_eval.takeover import (
    NativeUncertain,
    ProtocolMismatch,
    Refusal,
    TakeoverCompleted,
    TakeoverIncomplete,
    TakeoverResult,
    TakeoverUnsupported,
)


class ExecutionKind(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"


class ReasonKind(StrEnum):
    SAMPLE = "sample"
    NATIVE = "native"
    OPERATOR = "operator"
    REPLAY = "replay"
    ADMISSION = "admission"
    MISMATCH = "mismatch"
    CHECKPOINT = "checkpoint"
    PROTOCOL = "protocol"


class DetailKind(StrEnum):
    ARCHIVE = "archive"
    TARGET = "target"
    UNSUPPORTED = "unsupported"
    INPUT = "input"
    ORDER = "order"
    MATERIAL = "material"


class AnchorRecord(WireModel):
    recording_id: str
    model_sequence: int


class ReachedRecord(WireModel):
    anchor: AnchorRecord
    confirmed_operation_cursor: int
    semantic_digest: str


class ArchiveDetail(WireModel):
    kind: Literal[DetailKind.ARCHIVE] = DetailKind.ARCHIVE
    message: str


class TargetDetail(WireModel):
    kind: Literal[DetailKind.TARGET] = DetailKind.TARGET
    anchor: AnchorRecord


class UnsupportedDetail(WireModel):
    kind: Literal[DetailKind.UNSUPPORTED] = DetailKind.UNSUPPORTED
    cause: replay.UnsupportedCause


class InputDetail(WireModel):
    kind: Literal[DetailKind.INPUT] = DetailKind.INPUT
    component: replay.InputComponent


class OrderDetail(WireModel):
    kind: Literal[DetailKind.ORDER] = DetailKind.ORDER
    recorded: DependencyKind | OperationKind | None
    requested: DependencyKind | OperationKind


class MaterialDetail(WireModel):
    kind: Literal[DetailKind.MATERIAL] = DetailKind.MATERIAL
    message: str


type Detail = Annotated[
    ArchiveDetail | TargetDetail | UnsupportedDetail | InputDetail | OrderDetail | MaterialDetail,
    Field(discriminator="kind"),
]


class ReplayReason(WireModel):
    kind: Literal[ReasonKind.REPLAY] = ReasonKind.REPLAY
    reason: replay.RejectReason
    sequence: int | None
    detail: Detail


class AdmissionReason(WireModel):
    kind: Literal[ReasonKind.ADMISSION] = ReasonKind.ADMISSION
    evidence: Unsupported


class MismatchReason(WireModel):
    kind: Literal[ReasonKind.MISMATCH] = ReasonKind.MISMATCH
    evidence: Mismatch


class CheckpointReason(WireModel):
    kind: Literal[ReasonKind.CHECKPOINT] = ReasonKind.CHECKPOINT
    evidence: CheckpointCause


class ProtocolReason(WireModel):
    kind: Literal[ReasonKind.PROTOCOL] = ReasonKind.PROTOCOL
    recorded: Protocol
    configured: Protocol


class SampleStop(WireModel):
    kind: Literal[ReasonKind.SAMPLE] = ReasonKind.SAMPLE
    cause: StopCause
    decisions: int
    requests: int


class NativeStop(WireModel):
    kind: Literal[ReasonKind.NATIVE] = ReasonKind.NATIVE
    stage: Stage
    cause: IncompleteCause
    message: str


class OperatorStop(WireModel):
    kind: Literal[ReasonKind.OPERATOR] = ReasonKind.OPERATOR


type Stop = Annotated[SampleStop | NativeStop | OperatorStop, Field(discriminator="kind")]
type RefusalRecord = Annotated[
    ReplayReason | AdmissionReason | MismatchReason | CheckpointReason | ProtocolReason,
    Field(discriminator="kind"),
]


class EndpointKind(StrEnum):
    DECISIONS = "after_decisions"
    BATTLE = "battle_end"
    RUN = "run_end"


class DecisionEndpoint(WireModel):
    kind: Literal[EndpointKind.DECISIONS] = EndpointKind.DECISIONS
    count: int


class BattleEndpoint(WireModel):
    kind: Literal[EndpointKind.BATTLE] = EndpointKind.BATTLE
    node_id: str


class RunEndpoint(WireModel):
    kind: Literal[EndpointKind.RUN] = EndpointKind.RUN


class CompletedExecution(WireModel):
    kind: Literal[ExecutionKind.COMPLETED] = ExecutionKind.COMPLETED
    branch: Path
    recording_id: str
    anchor: ReachedRecord
    endpoint: Annotated[
        DecisionEndpoint | BattleEndpoint | RunEndpoint, Field(discriminator="kind")
    ]
    decisions: int
    requests: int


class IncompleteExecution(WireModel):
    kind: Literal[ExecutionKind.INCOMPLETE] = ExecutionKind.INCOMPLETE
    branch: Path
    recording_id: str | None
    anchor: ReachedRecord | None
    reason: Stop


class UnsupportedExecution(WireModel):
    kind: Literal[ExecutionKind.UNSUPPORTED] = ExecutionKind.UNSUPPORTED
    branch: Path
    recording_id: str | None
    reason: RefusalRecord


type ExecutionRecord = Annotated[
    CompletedExecution | IncompleteExecution | UnsupportedExecution, Field(discriminator="kind")
]


def anchor_record(value: replay.Reached) -> ReachedRecord:
    return ReachedRecord(
        anchor=AnchorRecord(
            recording_id=value.anchor.recording_id, model_sequence=value.anchor.model_sequence
        ),
        confirmed_operation_cursor=value.confirmed_operation_cursor,
        semantic_digest=value.semantic_digest,
    )


def _detail(value: replay.ReplayRejectionDetail) -> Detail:
    match value:
        case replay.ArchiveUnreadable(message):
            return ArchiveDetail(message=message)
        case replay.MissingTarget(anchor):
            return TargetDetail(
                anchor=AnchorRecord(
                    recording_id=anchor.recording_id, model_sequence=anchor.model_sequence
                )
            )
        case replay.Unsupported(cause):
            return UnsupportedDetail(cause=cause)
        case replay.InputDifference(component):
            return InputDetail(component=component)
        case replay.OrderDifference(recorded, requested):
            return OrderDetail(recorded=recorded, requested=requested)
        case replay.MaterialDifference(message):
            return MaterialDetail(message=message)
        case _:
            assert_never(value)


def _refusal(value: Refusal) -> RefusalRecord:
    match value:
        case replay.Rejected(reason, sequence, detail):
            return ReplayReason(reason=reason, sequence=sequence, detail=_detail(detail))
        case Unsupported():
            return AdmissionReason(evidence=value)
        case Mismatch():
            return MismatchReason(evidence=value)
        case CheckpointCause():
            return CheckpointReason(evidence=value)
        case ProtocolMismatch(recorded, configured):
            return ProtocolReason(recorded=recorded, configured=configured)
        case _:
            assert_never(value)


def execution_record(value: TakeoverResult) -> ExecutionRecord:
    match value:
        case TakeoverCompleted(
            branch, anchor, Completed(endpoint, decisions, requests), recording_id
        ):
            finished: DecisionEndpoint | BattleEndpoint | RunEndpoint
            if isinstance(endpoint, AfterDecisions):
                finished = DecisionEndpoint(count=endpoint.count)
            elif isinstance(endpoint, BattleEnd):
                finished = BattleEndpoint(node_id=endpoint.node_id)
            else:
                finished = RunEndpoint()
            return CompletedExecution(
                branch=branch.resolve(),
                recording_id=recording_id,
                anchor=anchor_record(anchor),
                endpoint=finished,
                decisions=decisions,
                requests=requests,
            )
        case TakeoverIncomplete(branch, anchor, evidence, recording_id):
            match evidence:
                case Incomplete(cause, decisions, requests):
                    reason = SampleStop(cause=cause, decisions=decisions, requests=requests)
                case NativeUncertain(stage, cause, message):
                    reason = NativeStop(stage=stage, cause=cause, message=message)
                case OperatorStopped():
                    reason = OperatorStop()
                case _:
                    assert_never(evidence)
            return IncompleteExecution(
                branch=branch.resolve(),
                recording_id=recording_id,
                anchor=anchor_record(anchor) if anchor else None,
                reason=reason,
            )
        case TakeoverUnsupported(branch, evidence, recording_id):
            return UnsupportedExecution(
                branch=branch.resolve(), recording_id=recording_id, reason=_refusal(evidence)
            )
        case _:
            assert_never(value)
