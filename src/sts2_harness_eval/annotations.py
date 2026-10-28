"""Immutable evaluation standards and review events, separate from restore cases."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, model_validator

from sts2_harness.client.models import Nonempty, WireModel
from sts2_harness_eval.cases import CaseId, CaseStore, Sequence


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


class RecordKind(StrEnum):
    ANNOTATIONS = "annotations"
    REVIEWS = "reviews"
    RUBRICS = "rubrics"


class AnnotationStore:
    def __init__(self, directory: Path, cases: CaseStore) -> None:
        self.directory = directory.resolve()
        self.cases = cases

    def _append(self, group: RecordKind, record: RecordRef) -> None:
        path = self.directory / record.case_id / group / f"{record.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / ("." + uuid4().hex)
        try:
            with temporary.open("xb") as destination:
                destination.write(record.model_dump_json().encode())
                destination.flush()
                os.fsync(destination.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def append_annotation(self, record: AnnotationRecord) -> AnnotationRef:
        self.cases.load(record.case_id)
        self._append(RecordKind.ANNOTATIONS, record)
        return AnnotationRef(case_id=record.case_id, id=record.id)

    def append_review(self, record: ReviewRecord) -> ReviewRef:
        records = self.load(record.case_id)
        self._check_review(record, record.case_id, records.annotations)
        self._append(RecordKind.REVIEWS, record)
        return ReviewRef(case_id=record.case_id, id=record.id)

    def append_rubric(self, record: RubricRecord) -> RubricRef:
        self.cases.load(record.case_id)
        self._append(RecordKind.RUBRICS, record)
        return RubricRef(case_id=record.case_id, id=record.id)

    def _read[T: RecordRef](self, case_id: str, group: RecordKind, model: type[T]) -> tuple[T, ...]:
        records: list[T] = []
        for path in sorted((self.directory / case_id / group).glob("*.json")):
            record = model.model_validate_json(path.read_bytes())
            if record.case_id != case_id or record.id != path.stem:
                raise ValueError("The stored record does not match its case and record identity.")
            records.append(record)
        return tuple(records)

    @staticmethod
    def _check_review(
        review: ReviewRecord, case_id: str, annotations: tuple[AnnotationRecord, ...]
    ) -> None:
        if review.annotation.case_id != case_id or not any(
            annotation.id == review.annotation.id for annotation in annotations
        ):
            raise ValueError("The review must reference an existing annotation in its case.")

    def load(self, case_id: str) -> CaseAnnotations:
        case_id = TypeAdapter(CaseId).validate_python(case_id)
        self.cases.load(case_id)
        annotations = self._read(case_id, RecordKind.ANNOTATIONS, AnnotationRecord)
        reviews = self._read(case_id, RecordKind.REVIEWS, ReviewRecord)
        rubrics = self._read(case_id, RecordKind.RUBRICS, RubricRecord)
        for review in reviews:
            self._check_review(review, case_id, annotations)
        return CaseAnnotations(
            case_id=case_id, annotations=annotations, reviews=reviews, rubrics=rubrics
        )
