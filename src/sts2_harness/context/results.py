"""Semantic tool results enter Context after IO and execution have finished."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import MISSING, Field

from sts2_harness.client import models as native


class ContextValue(native.WireModel):
    def to_json(self) -> str:
        return self.model_dump_json()


class OutcomeStatus(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReasonCode(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    WRONG_ROLE = "wrong_role"
    ILLEGAL_ACTION = "illegal_action"
    SELECTION_REQUIRED = "selection_required"
    ROLE_CHANGED = "role_changed"
    CONTROL_UNAVAILABLE = "control_unavailable"
    REFERENCE_EXPIRED = "reference_expired"
    QUERY_FAILED = "query_failed"
    NOTES_CONFLICT = "notes_conflict"
    EXECUTION_UNCONFIRMED = "execution_unconfirmed"
    CONTENT_UNAVAILABLE = "content_unavailable"


class Reason(ContextValue):
    code: ReasonCode
    message: str


class Suggestion(ContextValue):
    message: str


type Suggestions = Annotated[tuple[Suggestion, ...], Field(min_length=1)]
type StepCount = Annotated[int, Field(ge=0)]
type StepPosition = Annotated[int, Field(ge=1)]


class ReadCompleted(ContextValue):
    status: Literal[OutcomeStatus.COMPLETED] = OutcomeStatus.COMPLETED


class ActionCompleted(ContextValue):
    status: Literal[OutcomeStatus.COMPLETED] = OutcomeStatus.COMPLETED
    completed_steps: StepCount


class ActionStopped(ContextValue):
    status: Literal[OutcomeStatus.STOPPED] = OutcomeStatus.STOPPED
    completed_steps: StepCount
    next_step: StepPosition
    reason: Reason
    suggested_actions: Suggestions


class Rejected(ContextValue):
    status: Literal[OutcomeStatus.REJECTED] = OutcomeStatus.REJECTED
    reason: Reason
    suggested_actions: Suggestions


class Failed(ContextValue):
    status: Literal[OutcomeStatus.FAILED] = OutcomeStatus.FAILED
    reason: Reason
    suggested_actions: Suggestions


class ActionUnknown(ContextValue):
    status: Literal[OutcomeStatus.UNKNOWN] = OutcomeStatus.UNKNOWN
    confirmed_steps: StepCount
    uncertain_step: StepPosition
    reason: Reason
    suggested_actions: Suggestions


class WriteUnknown(ContextValue):
    status: Literal[OutcomeStatus.UNKNOWN] = OutcomeStatus.UNKNOWN
    reason: Reason
    suggested_actions: Suggestions


type ActionOutcome = ActionCompleted | ActionStopped | Rejected | ActionUnknown
type QueryOutcome = ReadCompleted | Rejected | Failed | WriteUnknown
type Outcome = ActionOutcome | QueryOutcome
type QueryData = native.InspectData | native.CatalogData | native.SearchData | native.LookupData


@dataclass(frozen=True, slots=True)
class ActionToolResult:
    outcome: ActionOutcome
    observation: native.Observation | None


@dataclass(frozen=True, slots=True)
class QueryToolResult:
    outcome: QueryOutcome
    observation: native.Observation | None
    data: QueryData | MISSING = MISSING


type ToolResult = ActionToolResult | QueryToolResult
