"""Finite paired plans, append-only progress, and comparison projections."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from sts2_harness.client.models import Nonempty, WireModel
from sts2_harness.journal_models import ConfigurationSnapshot
from sts2_harness_eval.assessment_records import (
    AssessmentRef,
    AssessmentSelection,
    SampleAssessment,
)
from sts2_harness_eval.cases import Case, CaseId
from sts2_harness_eval.execution_record import ExecutionRecord
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.variants import MaterialVariant, SceneStart


class Arm(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class Partition(StrEnum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


class SuiteVariant(WireModel):
    name: Nonempty
    material: MaterialVariant


class SuiteCase(WireModel):
    case_id: CaseId
    partition: Partition
    pairs: Annotated[int, Field(gt=0)]
    baseline: SuiteVariant
    candidate: SuiteVariant
    budget: SampleBudget
    standards: AssessmentSelection

    @model_validator(mode="after")
    def require_paired_conditions(self) -> Self:
        baseline, candidate = self.baseline, self.candidate
        if baseline.name == candidate.name:
            raise ValueError("Paired variants require distinct names.")
        if type(baseline.material.start) is not type(candidate.material.start):
            raise ValueError("Paired variants require the same starting mode.")
        if isinstance(baseline.material.start, SceneStart) and (
            not isinstance(candidate.material.start, SceneStart)
            or baseline.material.start.initial_notes != candidate.material.start.initial_notes
        ):
            raise ValueError("Paired Scene variants require identical initial notes.")
        if self.budget.requests < 0 or self.budget.seconds < 0 or not isfinite(self.budget.seconds):
            raise ValueError("Live budgets must be finite and nonnegative.")
        return self

    def order(self, repeat_index: int) -> tuple[Arm, Arm]:
        return (
            (Arm.BASELINE, Arm.CANDIDATE)
            if repeat_index % 2 == 0
            else (Arm.CANDIDATE, Arm.BASELINE)
        )


class SuiteSpec(WireModel):
    cases: Annotated[tuple[SuiteCase, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_distinct_cases(self) -> Self:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Each suite case must be declared once.")
        return self


class SuitePlan(WireModel):
    spec: SuiteSpec
    cases: tuple[Case, ...]
    baseline: ConfigurationSnapshot
    candidate: ConfigurationSnapshot


class ArmAddress(WireModel):
    case_id: CaseId
    repeat_index: Annotated[int, Field(ge=0)]
    arm: Arm


class ProgressKind(StrEnum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    ASSESSMENT_AVAILABLE = "assessment_available"


class NotStarted(WireModel):
    kind: Literal[ProgressKind.NOT_STARTED] = ProgressKind.NOT_STARTED


class Started(WireModel):
    kind: Literal[ProgressKind.STARTED] = ProgressKind.STARTED
    execution: ExecutionRecord | None = None
    observed_configurations: tuple[ConfigurationSnapshot, ...] = ()


class AssessmentAvailable(WireModel):
    kind: Literal[ProgressKind.ASSESSMENT_AVAILABLE] = ProgressKind.ASSESSMENT_AVAILABLE
    execution: ExecutionRecord
    observed_configurations: tuple[ConfigurationSnapshot, ...]
    assessment: AssessmentRef


type ArmProgress = Annotated[
    NotStarted | Started | AssessmentAvailable, Field(discriminator="kind")
]


class ProgressRecord(WireModel):
    address: ArmAddress
    progress: ArmProgress


class AssessmentChoice(WireModel):
    address: ArmAddress
    assessment: AssessmentRef


@dataclass(frozen=True, slots=True)
class ArmReport:
    configured: ConfigurationSnapshot
    progress: ArmProgress
    assessment: SampleAssessment | None
    selected_assessment: AssessmentRef | None


@dataclass(frozen=True, slots=True)
class PairComparison:
    net_hp_change: int | None
    potion_uses: int | None
    victory: int | None
    reward_added_cards: int | None
    acceptable: tuple[bool, bool] | None


@dataclass(frozen=True, slots=True)
class PairReport:
    case_id: CaseId
    repeat_index: int
    partition: Partition
    order: tuple[Arm, ...]
    baseline: ArmReport
    candidate: ArmReport
    comparison: PairComparison


@dataclass(frozen=True, slots=True)
class Distribution:
    values: tuple[int, ...]
    minimum: int | None
    maximum: int | None

    @property
    def comparable_pairs(self) -> int:
        return len(self.values)


@dataclass(frozen=True, slots=True)
class PartitionReport:
    partition: Partition
    planned_pairs: int
    not_started: int
    started_without_assessment: int
    assessment_available: int
    completed: int
    incomplete: int
    unsupported: int
    endpoints_reached: int
    unscored: int
    unassessed: int
    assessed: int
    acceptable_pairs: int
    net_hp_change: Distribution
    potion_uses: Distribution
    victory: Distribution
    reward_added_cards: Distribution


@dataclass(frozen=True, slots=True)
class SuiteReport:
    plan: SuitePlan
    pairs: tuple[PairReport, ...]
    partitions: tuple[PartitionReport, ...]
    planned_live_requests: int
