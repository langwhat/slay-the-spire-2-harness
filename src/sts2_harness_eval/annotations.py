"""Immutable evaluation standards and review events, separate from restore cases."""

from __future__ import annotations

import os
from contextlib import closing
from enum import StrEnum
from hashlib import file_digest, sha256
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from sts2_harness.journal import Journal
from sts2_harness_eval.annotation_records import (
    AgentAuthor as AgentAuthor,
)
from sts2_harness_eval.annotation_records import (
    AnnotationRecord as AnnotationRecord,
)
from sts2_harness_eval.annotation_records import (
    AnnotationRef as AnnotationRef,
)
from sts2_harness_eval.annotation_records import (
    Author as Author,
)
from sts2_harness_eval.annotation_records import (
    AuthoredRecord as AuthoredRecord,
)
from sts2_harness_eval.annotation_records import (
    BattleRubric as BattleRubric,
)
from sts2_harness_eval.annotation_records import (
    CardRewardRubric as CardRewardRubric,
)
from sts2_harness_eval.annotation_records import (
    CaseAnnotations as CaseAnnotations,
)
from sts2_harness_eval.annotation_records import (
    EvidenceRef as EvidenceRef,
)
from sts2_harness_eval.annotation_records import (
    HumanAuthor as HumanAuthor,
)
from sts2_harness_eval.annotation_records import (
    Origin as Origin,
)
from sts2_harness_eval.annotation_records import (
    RecordRef as RecordRef,
)
from sts2_harness_eval.annotation_records import (
    ReviewDecision as ReviewDecision,
)
from sts2_harness_eval.annotation_records import (
    ReviewRecord as ReviewRecord,
)
from sts2_harness_eval.annotation_records import (
    ReviewRef as ReviewRef,
)
from sts2_harness_eval.annotation_records import (
    RubricKind as RubricKind,
)
from sts2_harness_eval.annotation_records import (
    RubricRecord as RubricRecord,
)
from sts2_harness_eval.annotation_records import (
    RubricRef as RubricRef,
)
from sts2_harness_eval.assessment_records import (
    Assessed,
    AssessmentRecord,
    AssessmentRef,
    AssessmentSelection,
    CaseTargetRef,
    SampleAssessment,
    SelectedStandards,
    Unassessed,
)
from sts2_harness_eval.cases import Case, CaseId, CaseStore
from sts2_harness_eval.execution_record import CompletedExecution


class RecordKind(StrEnum):
    ANNOTATIONS = "annotations"
    REVIEWS = "reviews"
    RUBRICS = "rubrics"
    ASSESSMENTS = "assessments"


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

    def select(
        self,
        case: Case,
        selection: AssessmentSelection,
    ) -> SelectedStandards:
        records = self.load(case.id)
        if self.cases.load(case.id) != case:
            raise ValueError("The supplied case differs from its immutable stored case.")
        annotation = None
        review = None
        rubric = None
        if selection.annotation is not None:
            annotation = next(
                (r for r in records.annotations if r.id == selection.annotation.id), None
            )
            if selection.annotation.case_id != case.id or annotation is None:
                raise ValueError("The selected annotation does not exist in this case.")
        if selection.review is not None:
            review = next((r for r in records.reviews if r.id == selection.review.id), None)
            if (
                selection.review.case_id != case.id
                or review is None
                or review.annotation != selection.annotation
            ):
                raise ValueError("The selected review must refer to the selected annotation.")
        if selection.rubric is not None:
            rubric = next((r for r in records.rubrics if r.id == selection.rubric.id), None)
            if selection.rubric.case_id != case.id or rubric is None:
                raise ValueError("The selected rubric does not exist in this case.")
        return SelectedStandards(
            selection=selection, annotation=annotation, review=review, rubric=rubric
        )

    def _check_assessment(self, report: SampleAssessment) -> None:
        provenance = report.provenance
        case = self.cases.load(provenance.case_id)
        if (
            sha256(case.model_dump_json().encode()).hexdigest() != provenance.case_sha256
            or case.source != provenance.source
        ):
            raise ValueError("The assessment does not match its immutable case.")
        if self.select(case, report.standards.selection) != report.standards:
            raise ValueError("The assessment standards do not match the selected records.")
        if provenance.archive != report.execution.branch or report.endpoint_reached != isinstance(
            report.execution, CompletedExecution
        ):
            raise ValueError("The report execution and sample provenance disagree.")
        sample = provenance.sample
        if sample is not None and sample.recording_id != report.execution.recording_id:
            raise ValueError("The report recording does not match its execution.")
        if sample is None and (
            provenance.parent is not None
            or provenance.first_sequence is not None
            or provenance.last_sequence is not None
            or report.endpoint_reached
        ):
            raise ValueError("An unrecorded sample cannot claim recorded evidence.")
        if sample is not None:
            with provenance.archive.open("rb") as source:
                digest = file_digest(source, "sha256").hexdigest()
            with closing(Journal.open(provenance.archive)) as journal:
                manifest = journal.manifest
                parent = manifest.parent
                if (
                    parent is None
                    or parent.archive.resolve() != case.source.archive.resolve()
                    or parent.recording_id != case.source.anchor.recording_id
                    or parent.before_sequence != case.source.anchor.model_sequence
                    or provenance.first_sequence != parent.before_sequence
                ):
                    raise ValueError("The report sample does not match the case source interval.")
                summaries = journal.summaries(start=parent.before_sequence)
                if provenance.last_sequence != max(
                    (item.sequence for item in summaries), default=None
                ):
                    raise ValueError("The report evidence interval differs from its sample.")
                binding = self.cases.binding(case)
                target = self.cases.reference(binding).target
                if provenance.target != CaseTargetRef(
                    case_id=case.id,
                    reference_sha256=binding.reference,
                    recording_id=case.source.anchor.recording_id,
                    sequence=target.source_sequence,
                ):
                    raise ValueError("The report target is not the verified case publication.")
                endpoint = provenance.endpoint
                if endpoint is not None:
                    observations = tuple(
                        record
                        for record in journal.operations(
                            start=endpoint.sequence, stop=endpoint.sequence
                        )
                        if record.reply is not None
                    )
                    if (
                        endpoint.recording_id != sample.recording_id
                        or endpoint.sequence < parent.before_sequence
                        or not observations
                        or observations[0].reply is None
                        or not 0 <= endpoint.index < len(observations[0].reply.observations)
                    ):
                        raise ValueError(
                            "The report endpoint is not a recorded sample observation."
                        )
                if (
                    sample.sha256 != digest
                    or sample.recording_id != manifest.recording_id
                    or provenance.parent != manifest.parent
                    or provenance.variant != manifest.variant
                ):
                    raise ValueError("The assessment sample archive has changed.")
        judgment = None
        if isinstance(report.quality, Assessed):
            judgment = report.quality.judgment
        elif isinstance(report.quality, Unassessed):
            judgment = report.quality.failure
        if judgment is not None and (
            judgment.case_id != case.id
            or judgment.sample != sample
            or judgment.selection != report.standards.selection
        ):
            raise ValueError("The assessment judgment has mismatched associations.")

    def append_assessment(self, report: SampleAssessment, *, id: str) -> AssessmentRef:
        record = AssessmentRecord(
            case_id=report.provenance.case_id,
            id=TypeAdapter(CaseId).validate_python(id),
            assessment=report,
        )
        self._check_assessment(report)
        self._append(RecordKind.ASSESSMENTS, record)
        return AssessmentRef(case_id=record.case_id, id=record.id)

    def assessment(self, reference: AssessmentRef) -> SampleAssessment:
        path = self.directory / reference.case_id / RecordKind.ASSESSMENTS / f"{reference.id}.json"
        record = AssessmentRecord.model_validate_json(path.read_bytes())
        if record.case_id != reference.case_id or record.id != reference.id:
            raise ValueError("The stored assessment does not match its reference.")
        self._check_assessment(record.assessment)
        return record.assessment
