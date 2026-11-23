"""Portable native decision inputs and user labels, without rendered materials."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from sts2_harness.client import models as native
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import BattleReport
from sts2_harness.fights import Fight, FightEncounter
from sts2_harness.game import Catalog, Inspect
from sts2_harness.journal_models import NativeSnapshot
from sts2_harness.results import ActionOutcome, Outcome, PlanData
from sts2_harness.tools import ToolName


class DecisionSource(native.WireModel):
    round: str
    seed: str
    floor: int
    model_sequence: int
    archive: Path


class ChoiceLabels(native.WireModel):
    good: tuple[str, ...] = ()
    bad: tuple[str, ...] = ()
    multiselect: bool = False


class RewardGroupLabels(ChoiceLabels):
    offer: native.Nonempty


class DecisionLabels(ChoiceLabels):
    comment: str
    reward_groups: tuple[RewardGroupLabels, ...] = ()
    reward_order: tuple[native.Nonempty, ...] = ()

    @model_validator(mode="after")
    def distinct_reward_groups(self) -> Self:
        if len({group.offer for group in self.reward_groups}) != len(self.reward_groups):
            raise ValueError("Each card reward group can have one label.")
        if len(set(self.reward_order)) != len(self.reward_order):
            raise ValueError("Each card reward group can appear once in the expected order.")
        return self


class RewardGroupDecision(native.WireModel):
    offer: str | None
    choices: tuple[str, ...]
    reason: str


class RouteState(native.WireModel):
    position: str | None
    edges: tuple[tuple[str | None, str], ...]


class HarnessState(native.WireModel):
    notes: dict[str, JsonValue] = {}
    added: int = 0
    skipped: int = 0
    observed_plays: dict[str, int] = {}
    route: RouteState | None = None
    fights: tuple[Fight, ...] = ()
    elites_fought: tuple[FightEncounter, ...] | None = None


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
    recorded_reward_groups: tuple[RewardGroupDecision, ...] = ()


class DecisionCase(RecordedDecision):
    version: Literal[1] = 1
    minimal: bool = False
    viewer: bool = True
    entry_documents_enabled: bool = True
    configured_guidance: tuple[str, ...] = ()
    # Existing cases may record this; replays deliver no candidate links.
    candidate_links_enabled: bool = False
    events: Annotated[tuple[HistoryEvent, ...], Field(min_length=1)]
    native_reads: tuple[NativeRead, ...] = ()
    combat_history: native.HistoryData | None = None
    decision_tool: str
    decision_offer: str | None = None


class DecisionVerdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNLABELED = "unlabeled"
    DEVIATED = "deviated"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"


class ProposalStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class DecisionVariant(StrEnum):
    RUN = "run"
    DECK = "deck"


def score(labels: ChoiceLabels, choices: tuple[str, ...]) -> DecisionVerdict:
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


def score_decision(
    labels: DecisionLabels,
    choices: tuple[str, ...],
    groups: tuple[RewardGroupDecision, ...],
) -> DecisionVerdict:
    if not labels.reward_groups and not labels.reward_order:
        return DecisionVerdict.UNLABELED if len(groups) > 1 else score(labels, choices)
    if labels.reward_order and tuple(group.offer for group in groups) != labels.reward_order:
        return DecisionVerdict.FAILED
    by_offer = {group.offer: group.choices for group in groups}
    results = tuple(
        score(label, by_offer[label.offer]) if label.offer in by_offer else DecisionVerdict.FAILED
        for label in labels.reward_groups
    )
    if DecisionVerdict.FAILED in results:
        return DecisionVerdict.FAILED
    if DecisionVerdict.UNLABELED in results:
        return DecisionVerdict.UNLABELED
    return DecisionVerdict.PASSED


class RequestSize(native.WireModel):
    system_characters: int
    tools_characters: int
    knowledge_characters: int
    volatile_characters: int
    encoded_bytes: int
    estimated_input_tokens: int
    digest: str


class CapturedProposal(native.WireModel):
    tool: ToolName
    arguments: str


class RouteMetrics(native.WireModel):
    full_route: bool
    planned_elites: int
    hp: int
    max_hp: int
    low_hp: bool | None
    forced_elite_before_recovery: bool | None
    bad_low_hp_route: bool | None


class DecisionSample(native.WireModel):
    source: DecisionSource
    sample: int
    verdict: DecisionVerdict
    choices: tuple[str, ...]
    reason: str
    reward_groups: tuple[RewardGroupDecision, ...] = ()
    error: str | None = None
    proposal: CapturedProposal | None = None
    proposal_status: ProposalStatus | None = None
    route_metrics: RouteMetrics | None = None
    requests: tuple[RequestSize, ...] = ()
    usage: tuple[dict[str, int | None], ...] = ()
    elapsed_seconds: float = 0
    over: str | None = None
    raw_calls: tuple[dict[str, JsonValue], ...] = ()
    request_bodies: tuple[dict[str, JsonValue], ...] = ()
    model_replies: tuple[str, ...] = ()
    attempts: tuple[dict[str, JsonValue], ...] = ()

    def display_choices(self) -> tuple[str, ...]:
        if len(self.reward_groups) <= 1:
            return self.choices
        return tuple(f"{group.offer} [{', '.join(group.choices)}]" for group in self.reward_groups)

    @model_validator(mode="after")
    def require_proposal_and_status_together(self) -> Self:
        if (self.proposal is None) != (self.proposal_status is None):
            raise ValueError("A proposal and its status must be supplied together.")
        if self.route_metrics is not None and (
            self.proposal is None
            or self.proposal.tool is not ToolName.RUN_PLAN
            or self.proposal_status is not ProposalStatus.VALID
        ):
            raise ValueError("Route metrics require a valid captured route plan.")
        return self
