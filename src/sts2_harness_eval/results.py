"""Closed evidence returned by offline software replay."""

from dataclasses import dataclass
from enum import StrEnum

from sts2_harness.game import OperationKind
from sts2_harness.journal import DependencyKind


@dataclass(frozen=True, slots=True)
class ModelAnchor:
    recording_id: str
    model_sequence: int


class RejectReason(StrEnum):
    UNSUPPORTED_HISTORY = "unsupported_history"
    TARGET_NOT_FOUND = "target_not_found"
    INPUT_MISMATCH = "input_mismatch"
    ORDER_MISMATCH = "order_mismatch"


class UnsupportedCause(StrEnum):
    MISSING_INITIAL_NOTES = "missing_initial_notes"
    PREFIX_GAP = "prefix_gap"
    INCOMPLETE_DEPENDENCY = "incomplete_dependency"
    UNKNOWN_WRITE = "unknown_write"
    UNEXPECTED_END = "unexpected_end"
    START_MODE = "start_mode"


class InputComponent(StrEnum):
    REQUEST = "request"
    MODEL_INPUT = "model_input"
    TOOLS = "tools"
    ROLE_STREAM = "role_stream"
    POSITION = "position"
    CURSOR = "cursor"
    SEMANTIC_DIGEST = "semantic_digest"
    OPERATION_ID = "operation_id"


@dataclass(frozen=True, slots=True)
class ArchiveUnreadable:
    message: str


@dataclass(frozen=True, slots=True)
class MissingTarget:
    anchor: ModelAnchor


@dataclass(frozen=True, slots=True)
class Unsupported:
    cause: UnsupportedCause


@dataclass(frozen=True, slots=True)
class InputDifference:
    component: InputComponent


@dataclass(frozen=True, slots=True)
class OrderDifference:
    recorded: DependencyKind | OperationKind | None
    requested: DependencyKind | OperationKind


type ReplayRejectionDetail = (
    ArchiveUnreadable | MissingTarget | Unsupported | InputDifference | OrderDifference
)


@dataclass(frozen=True, slots=True)
class Reached:
    anchor: ModelAnchor
    confirmed_operation_cursor: int
    semantic_digest: str


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: RejectReason
    sequence: int | None
    detail: ReplayRejectionDetail


type ReplayResult = Reached | Rejected
