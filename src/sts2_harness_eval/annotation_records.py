"""Immutable annotation standards and their permanent authorship."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from sts2_harness.client.models import Nonempty, WireModel
from sts2_harness_eval.cases import CaseId, Sequence


class Origin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class EvidenceRef(WireModel):
    recording_id: Nonempty
    sequence: Sequence


class HumanAuthor(WireModel):
    kind: Literal[Origin.HUMAN] = Origin.HUMAN
    id: Nonempty


class AgentAuthor(WireModel):
    kind: Literal[Origin.AGENT] = Origin.AGENT
    id: Nonempty
    model: Nonempty | None = None
    generation: EvidenceRef | None = None

    @model_validator(mode="after")
    def require_generation_source(self) -> Self:
        if self.model is None and self.generation is None:
            raise ValueError("An agent author requires a model or generation record reference.")
        return self


type Author = Annotated[HumanAuthor | AgentAuthor, Field(discriminator="kind")]


class RecordRef(WireModel):
    case_id: CaseId
    id: CaseId


class AnnotationRef(RecordRef):
    pass


class ReviewRef(RecordRef):
    pass


class RubricRef(RecordRef):
    pass


class AuthoredRecord(RecordRef):
    origin: Origin
    author: Author

    @model_validator(mode="after")
    def require_matching_origin(self) -> Self:
        if self.origin is not self.author.kind:
            raise ValueError("Record origin must match its author's permanent source.")
        return self


class AnnotationRecord(AuthoredRecord):
    text: Nonempty
    evidence_refs: tuple[EvidenceRef, ...]


class ReviewDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class ReviewRecord(RecordRef):
    annotation: AnnotationRef
    decision: ReviewDecision
    reviewer: Author
    rationale: Nonempty


class RubricKind(StrEnum):
    BATTLE = "battle"
    CARD_REWARD = "card_reward"


class BattleRubric(WireModel):
    kind: Literal[RubricKind.BATTLE] = RubricKind.BATTLE
    description: Nonempty


class CardRewardRubric(WireModel):
    kind: Literal[RubricKind.CARD_REWARD] = RubricKind.CARD_REWARD
    acceptable_card_ids: tuple[Nonempty, ...]
    allow_skip: bool
    description: str = ""


class RubricRecord(AuthoredRecord):
    criterion: Annotated[BattleRubric | CardRewardRubric, Field(discriminator="kind")]


class CaseAnnotations(WireModel):
    case_id: CaseId
    annotations: tuple[AnnotationRecord, ...]
    reviews: tuple[ReviewRecord, ...]
    rubrics: tuple[RubricRecord, ...]
