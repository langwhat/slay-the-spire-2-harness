"""Portable native decision inputs and user labels, without rendered materials."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from sts2_harness.client import models as native
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import BattleReport
from sts2_harness.game import Catalog, Inspect
from sts2_harness.journal_models import NativeSnapshot
from sts2_harness.results import ActionOutcome, Outcome, PlanData


class DecisionSource(native.WireModel):
    round: str
    seed: str
    floor: int
    model_sequence: int
    archive: Path


class DecisionLabels(native.WireModel):
    good: tuple[str, ...] = ()
    bad: tuple[str, ...] = ()
    comment: str
    multiselect: bool = False


class RouteState(native.WireModel):
    position: str
    edges: tuple[tuple[str, str], ...]


class HarnessState(native.WireModel):
    notes: dict[str, JsonValue] = {}
    added: int = 0
    skipped: int = 0
    observed_plays: dict[str, int] = {}
    route: RouteState | None = None


class BattleState(native.WireModel):
    report: BattleReport
    review: str | None


class TaskEvent(native.WireModel):
    kind: Literal["task"] = "task"
    observation: native.Observation
    state: HarnessState
    handoff: BattleState | None = None
    previous_battle: BattleState | None = None
    handoff_only: bool = False
    declined_offers: tuple[str, ...] = ()


class AssistantEvent(native.WireModel):
    kind: Literal["assistant"] = "assistant"
    reply: NativeSnapshot


class ResultEvent(native.WireModel):
    kind: Literal["result"] = "result"
    call_id: str
    outcome: Outcome
    action: bool
    observation: native.Observation | None
    # Native replies and harness-owned values only; supplementary documents are reread.
    native_data: (
        native.InspectData | native.CatalogData | native.SearchData | native.LookupData | None
    ) = None
    notes_read: bool = False
    notes_updated: bool = False
    plan: PlanData | None = None
    document: tuple[str, str | None] | None = None
    knowledge_ids: tuple[str, ...] | None = None
    skill_listing: tuple[int, int] | None = None
    declined_offers: tuple[str, ...] = ()


class AutomaticEvent(native.WireModel):
    kind: Literal["automatic"] = "automatic"
    progress: AutomaticProgress
    outcome: ActionOutcome
    observation: native.Observation | None
    invalidated: bool = False
    declined_offers: tuple[str, ...] = ()


class CorrectionEvent(native.WireModel):
    kind: Literal["correction"] = "correction"
    message: str


class NativeRead(native.WireModel):
    request: Inspect | Catalog
    result: native.InspectCompleted | native.CatalogCompleted


type HistoryEvent = Annotated[
    TaskEvent | AssistantEvent | ResultEvent | AutomaticEvent | CorrectionEvent,
    Field(discriminator="kind"),
]


class RecordedDecision(native.WireModel):
    source: DecisionSource
    labels: DecisionLabels
    recorded_choices: tuple[str, ...]
    recorded_reason: str


class DecisionCase(RecordedDecision):
    version: Literal[1] = 1
    minimal: bool = False
    viewer: bool = True
    entry_documents_enabled: bool = True
    configured_guidance: tuple[str, ...] = ()
    candidate_links_enabled: bool = True
    events: Annotated[tuple[HistoryEvent, ...], Field(min_length=1)]
    native_reads: tuple[NativeRead, ...] = ()
    decision_tool: str
    decision_offer: str | None = None


class DecisionVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNLABELED = "unlabeled"
    DEVIATED = "deviated"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"


class DecisionVariant(StrEnum):
    RUN = "run"
    DECK = "deck"


def score(labels: DecisionLabels, choices: tuple[str, ...]) -> DecisionVerdict:
    selected = set(choices)
    if selected.intersection(labels.bad):
        return DecisionVerdict.FAILED
    if labels.multiselect:
        return (
            DecisionVerdict.PASSED
            if set(labels.good).issubset(selected)
            else DecisionVerdict.FAILED
        )
    return (
        DecisionVerdict.PASSED
        if len(selected) == 1 and selected.issubset(labels.good)
        else DecisionVerdict.UNLABELED
    )


class RequestSize(native.WireModel):
    system_characters: int
    tools_characters: int
    knowledge_characters: int
    volatile_characters: int
    encoded_bytes: int
    estimated_input_tokens: int
    digest: str


class DecisionSample(native.WireModel):
    source: DecisionSource
    sample: int
    verdict: DecisionVerdict
    choices: tuple[str, ...]
    reason: str
    error: str | None = None
    requests: tuple[RequestSize, ...] = ()
    usage: tuple[dict[str, int | None], ...] = ()
    elapsed_seconds: float = 0
    over: str | None = None
    raw_calls: tuple[dict[str, JsonValue], ...] = ()
    request_bodies: tuple[dict[str, JsonValue], ...] = ()
    model_replies: tuple[str, ...] = ()
    attempts: tuple[dict[str, JsonValue], ...] = ()
