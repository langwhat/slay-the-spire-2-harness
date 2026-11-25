"""Semantic tool results enter Context after IO and execution have finished."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import MISSING, Field

from sts2_harness.client import models as native
from sts2_harness.knowledge import DocumentView, KnowledgeLookup, SkillList
from sts2_harness.note_values import NotesView
from sts2_harness.route_plan import PlanStatus


class SemanticValue(native.WireModel):
    def to_json(self) -> str:
        return self.model_dump_json()


class OutcomeStatus(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReasonCode(StrEnum):
    BUSY = "busy"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_ARGUMENTS = "invalid_arguments"
    BATCH_ABORTED = "batch_aborted"
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
    UNKNOWN_POSITION = "unknown_position"
    DESTINATIONS_UNKNOWN = "destinations_unknown"
    EMPTY = "empty"
    UNKNOWN_NODE = "unknown_node"
    DUPLICATE_EDGE = "duplicate_edge"
    SELF_LOOP = "self_loop"
    AMBIGUOUS_NEXT = "ambiguous_next"
    DISCONNECTED = "disconnected"
    CYCLE = "cycle"
    NO_PLAN = "no_plan"
    PENDING_CHOICE = "pending_choice"
    TRAVEL_NOT_AVAILABLE = "travel_not_available"


class Reason(SemanticValue):
    code: ReasonCode
    message: str


class Suggestion(SemanticValue):
    message: str


type Suggestions = Annotated[tuple[Suggestion, ...], Field(min_length=1)]
type StepCount = Annotated[int, Field(ge=0)]
type StepPosition = Annotated[int, Field(ge=1)]
type GroupCount = Annotated[int, Field(ge=0)]
type GroupPosition = Annotated[int, Field(ge=1)]


class ReadCompleted(SemanticValue):
    status: Literal[OutcomeStatus.COMPLETED] = OutcomeStatus.COMPLETED


class ActionCompleted(SemanticValue):
    status: Literal[OutcomeStatus.COMPLETED] = OutcomeStatus.COMPLETED
    completed_steps: StepCount


class ActionStopped(SemanticValue):
    status: Literal[OutcomeStatus.STOPPED] = OutcomeStatus.STOPPED
    completed_steps: StepCount
    next_step: StepPosition
    reason: Reason
    suggested_actions: Suggestions


class Rejected(SemanticValue):
    status: Literal[OutcomeStatus.REJECTED] = OutcomeStatus.REJECTED
    reason: Reason
    suggested_actions: Suggestions


class Failed(SemanticValue):
    status: Literal[OutcomeStatus.FAILED] = OutcomeStatus.FAILED
    reason: Reason
    suggested_actions: Suggestions


class ActionUnknown(SemanticValue):
    status: Literal[OutcomeStatus.UNKNOWN] = OutcomeStatus.UNKNOWN
    confirmed_steps: StepCount
    uncertain_step: StepPosition
    reason: Reason
    suggested_actions: Suggestions


class WriteUnknown(SemanticValue):
    status: Literal[OutcomeStatus.UNKNOWN] = OutcomeStatus.UNKNOWN
    reason: Reason
    suggested_actions: Suggestions


class RewardBatchCompleted(SemanticValue):
    status: Literal[OutcomeStatus.COMPLETED] = OutcomeStatus.COMPLETED
    completed_groups: GroupCount


class RewardBatchStopped(SemanticValue):
    status: Literal[OutcomeStatus.STOPPED] = OutcomeStatus.STOPPED
    completed_groups: GroupCount
    next_group: GroupPosition
    reason: Reason
    suggested_actions: Suggestions


class RewardBatchUnknown(SemanticValue):
    status: Literal[OutcomeStatus.UNKNOWN] = OutcomeStatus.UNKNOWN
    confirmed_groups: GroupCount
    uncertain_group: GroupPosition
    reason: Reason
    suggested_actions: Suggestions


type ActionOutcome = ActionCompleted | ActionStopped | Rejected | ActionUnknown
type RewardBatchOutcome = RewardBatchCompleted | RewardBatchStopped | Rejected | RewardBatchUnknown
type QueryOutcome = ReadCompleted | Rejected | Failed | WriteUnknown
type Outcome = ActionOutcome | RewardBatchOutcome | QueryOutcome


class NotesText(SemanticValue):
    notes: NotesView


class NotesUpdated(SemanticValue):
    updated: Literal[True] = True
    summary: str | MISSING = MISSING


class HandoffCompletion(SemanticValue):
    call_id: str
    summary: native.Nonempty


class PlanAccepted(SemanticValue):
    status: Literal[PlanStatus.ACCEPTED] = PlanStatus.ACCEPTED
    next: str


class PlanRejected(SemanticValue):
    status: Literal[PlanStatus.REJECTED] = PlanStatus.REJECTED


class PlanPresentationStatus(StrEnum):
    NOT_AVAILABLE = "not_available"
    NOT_ATTEMPTED = "not_attempted"
    SHOWN = "shown"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PlanRestorationStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    RESTORED = "restored"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PlanPresentation(SemanticValue):
    status: PlanPresentationStatus


class PlanRestoration(SemanticValue):
    status: PlanRestorationStatus = PlanRestorationStatus.NOT_NEEDED


class PlanData(SemanticValue):
    plan: PlanAccepted | PlanRejected
    presentation: PlanPresentation
    restoration: PlanRestoration = PlanRestoration()


type QueryData = (
    native.InspectData
    | native.CatalogData
    | native.SearchData
    | native.LookupData
    | KnowledgeLookup
    | DocumentView
    | SkillList
    | NotesText
    | NotesUpdated
    | PlanData
)


@dataclass(frozen=True, slots=True)
class CardRewardResult:
    offer_ref: str | None
    before: native.Observation
    after: native.Observation | None
    outcome: ActionOutcome
    local_decline: bool = False
    display_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActionToolResult:
    outcome: ActionOutcome
    observation: native.Observation | None


@dataclass(frozen=True, slots=True)
class CardRewardBatchResult:
    outcome: RewardBatchOutcome
    observation: native.Observation | None
    groups: tuple[CardRewardResult, ...] = ()

    @property
    def completed_groups(self) -> tuple[CardRewardResult, ...]:
        return tuple(group for group in self.groups if isinstance(group.outcome, ActionCompleted))


@dataclass(frozen=True, slots=True)
class QueryToolResult:
    outcome: QueryOutcome
    observation: native.Observation | None
    data: QueryData | MISSING = MISSING


type ExecutionResult = ActionToolResult | CardRewardBatchResult
type ToolResult = ExecutionResult | QueryToolResult
