"""Concrete append-only assessment records shared by projection and storage."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from sts2_harness.client import models as native
from sts2_harness.journal import HistoricalVariant, ParentPrefix
from sts2_harness_eval.annotation_records import (
    AnnotationRecord,
    AnnotationRef,
    HumanAuthor,
    RecordRef,
    ReviewRecord,
    ReviewRef,
    RubricRecord,
    RubricRef,
)
from sts2_harness_eval.cases import CaseId, SourceRef
from sts2_harness_eval.checkpoints import Sha256
from sts2_harness_eval.execution_record import ExecutionRecord


class AssessmentSelection(native.WireModel):
    annotation: AnnotationRef | None = None
    review: ReviewRef | None = None
    rubric: RubricRef | None = None


class ArchiveIdentity(native.WireModel):
    recording_id: native.Nonempty
    sha256: Sha256


class ObservationKind(StrEnum):
    CASE_TARGET = "case_target"
    RECORDED = "recorded"


class CaseTargetRef(native.WireModel):
    kind: Literal[ObservationKind.CASE_TARGET] = ObservationKind.CASE_TARGET
    case_id: CaseId
    reference_sha256: Sha256
    recording_id: native.Nonempty
    sequence: int


class ObservationRef(native.WireModel):
    kind: Literal[ObservationKind.RECORDED] = ObservationKind.RECORDED
    recording_id: native.Nonempty
    sequence: int
    index: int


class SampleProvenance(native.WireModel):
    case_id: CaseId
    case_sha256: Sha256
    source: SourceRef
    archive: Path
    sample: ArchiveIdentity | None
    parent: ParentPrefix | None
    variant: HistoricalVariant | None
    first_sequence: int | None
    last_sequence: int | None
    target: CaseTargetRef | None
    endpoint: ObservationRef | None


class PotionUse(native.WireModel):
    operation_id: native.Nonempty
    sequence: int
    definition_id: str | None


class ConfirmedAction(native.WireModel):
    operation_id: native.Nonempty
    sequence: int
    step: native.NativeStep
    route: native.RouteKey


class RewardChoice(native.WireModel):
    candidates: tuple[str, ...]
    selected: tuple[str, ...]
    skipped: bool
    before_deck: tuple[str, ...]
    after_deck: tuple[str, ...]
    before: Annotated[CaseTargetRef | ObservationRef, Field(discriminator="kind")]
    after: ObservationRef
    operation_id: str
    sequence: int


class SampleMetrics(native.WireModel):
    target_hp: int | None
    endpoint_hp: int | None
    net_hp_change: int | None
    native_result: native.TerminalResult | None
    potion_uses: tuple[PotionUse, ...] | None
    reward: RewardChoice | None


class SampleEvidence(native.WireModel):
    actions: tuple[ConfirmedAction, ...]
    metrics: SampleMetrics


class HumanJudgment(native.WireModel):
    case_id: CaseId
    sample: ArchiveIdentity
    selection: AssessmentSelection
    acceptable: bool
    rationale: native.Nonempty
    author: HumanAuthor


class QualityKind(StrEnum):
    UNSCORED = "unscored"
    UNASSESSED = "unassessed"
    ASSESSED = "assessed"


class Unscored(native.WireModel):
    kind: Literal[QualityKind.UNSCORED] = QualityKind.UNSCORED


class UnassessedReason(StrEnum):
    NO_JUDGMENT = "no_judgment"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class Unassessed(native.WireModel):
    kind: Literal[QualityKind.UNASSESSED] = QualityKind.UNASSESSED
    reason: UnassessedReason


class Assessed(native.WireModel):
    kind: Literal[QualityKind.ASSESSED] = QualityKind.ASSESSED
    judgment: HumanJudgment


type Quality = Annotated[Unscored | Unassessed | Assessed, Field(discriminator="kind")]


class Uncertainty(StrEnum):
    NO_ARCHIVE = "no_archive"
    NO_TARGET = "no_target"
    NO_ENDPOINT = "no_endpoint"
    NO_NATIVE_RESULT = "no_native_result"
    INCOMPLETE_RECORDS = "incomplete_records"
    REWARD_EVIDENCE = "reward_evidence"


class SelectedStandards(native.WireModel):
    selection: AssessmentSelection
    annotation: AnnotationRecord | None
    review: ReviewRecord | None
    rubric: RubricRecord | None


class SampleAssessment(native.WireModel):
    execution: ExecutionRecord
    endpoint_reached: bool
    evidence: SampleEvidence
    quality: Quality
    provenance: SampleProvenance
    standards: SelectedStandards
    uncertainty: tuple[Uncertainty, ...]
    sample_count: Literal[1] = 1


class AssessmentRef(RecordRef):
    pass


class AssessmentRecord(AssessmentRef):
    assessment: SampleAssessment
