"""Finite paired evaluation, separate from gameplay context.

``run_suite`` creates a new output directory containing an immutable plan and
append-only arm progress. Assessment bodies remain in AnnotationStore; reports
load and validate their references. ``read_suite`` can select explicit later
AssessmentChoice records without running or retrying any arm.

Differences are candidate minus baseline, with observations and min/max ranges
reported separately for each predeclared partition. Missing evidence stays
unknown. Live budgets exclude restoration, retries, safe settlement and judging;
the plan's model configuration is requested configuration, not server identity.
"""

import os
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sts2_harness.client import models as native
from sts2_harness.journal import ArchiveError, HistoricalVariant, Journal, SceneVariant
from sts2_harness.journal_models import ConfigurationSnapshot
from sts2_harness.knowledge import Documents
from sts2_harness.llm.client import Llm
from sts2_harness.prompts import PromptReader
from sts2_harness_eval.annotations import AnnotationStore
from sts2_harness_eval.assessment import archive_identity, assess_sample
from sts2_harness_eval.assessment_records import (
    Assessed,
    AssessmentRef,
    SampleAssessment,
    SemanticJudge,
    Unassessed,
    Uncertainty,
    Unscored,
)
from sts2_harness_eval.cases import CaseStore
from sts2_harness_eval.execution_record import ExecutionKind, ExecutionRecord, execution_record
from sts2_harness_eval.native import NativeRestorer
from sts2_harness_eval.suite_records import (
    Arm,
    ArmAddress,
    ArmProgress,
    ArmReport,
    AssessmentAvailable,
    AssessmentChoice,
    Distribution,
    NotStarted,
    PairComparison,
    PairReport,
    Partition,
    PartitionReport,
    ProgressRecord,
    Started,
    SuiteCase,
    SuitePlan,
    SuiteReport,
    SuiteSpec,
)
from sts2_harness_eval.takeover import take_over
from sts2_harness_eval.variants import HistoricalStart, SceneStart, replacements


@dataclass(frozen=True, slots=True)
class ArmDependencies:
    model: Llm
    prompts: PromptReader
    documents: Documents


def _append(path: Path, record: native.WireModel) -> None:
    temporary = path.parent / ("." + uuid4().hex)
    try:
        with temporary.open("xb") as destination:
            destination.write(record.model_dump_json().encode())
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _branch(output: Path, address: ArmAddress) -> Path:
    return output / f"{address.case_id}-{address.repeat_index}-{address.arm}.sqlite"


class _Progress:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.sequence = 0

    def append(self, address: ArmAddress, progress: ArmProgress) -> None:
        _append(
            self.output / "progress" / f"{self.sequence:08d}.json",
            ProgressRecord(address=address, progress=progress),
        )
        self.sequence += 1


def _observed(execution: ExecutionRecord) -> tuple[ConfigurationSnapshot, ...]:
    if execution.recording_id is None:
        return ()
    try:
        identity = archive_identity(execution.branch)
    except ArchiveError:
        if execution.kind is ExecutionKind.COMPLETED:
            raise
        return ()
    if identity.recording_id != execution.recording_id:
        raise ValueError("The suite execution does not match its recording.")
    with closing(Journal.open(execution.branch)) as journal:
        start = journal.manifest.parent.before_sequence if journal.manifest.parent else 1
        return tuple(
            dict.fromkeys(record.configuration for record in journal.requests(start=start))
        )


async def run_suite(
    spec: SuiteSpec,
    cases: CaseStore,
    restorer: NativeRestorer,
    annotations: AnnotationStore,
    baseline: ArmDependencies,
    candidate: ArmDependencies,
    output: Path,
    *,
    stop_requested: Callable[[], bool],
    judge: SemanticJudge | None = None,
) -> SuiteReport:
    """Use equal per-arm live budgets; restoration, settlement and judging have no outer timeout."""
    plan = SuitePlan(
        spec=spec,
        cases=tuple(cases.load(item.case_id) for item in spec.cases),
        baseline=ConfigurationSnapshot.of(baseline.model.configuration),
        candidate=ConfigurationSnapshot.of(candidate.model.configuration),
    )
    for item, case in zip(spec.cases, plan.cases, strict=True):
        if annotations.cases.load(case.id) != case:
            raise ValueError("The annotation store must bind the same suite case.")
        annotations.select(case, item.standards)
    output = output.resolve()
    output.mkdir(parents=True)
    (output / "progress").mkdir()
    _append(output / "plan.json", plan)
    progress = _Progress(output)
    for item in spec.cases:
        for repeat in range(item.pairs):
            for arm in item.order(repeat):
                progress.append(
                    ArmAddress(case_id=item.case_id, repeat_index=repeat, arm=arm), NotStarted()
                )
    for item, case in zip(spec.cases, plan.cases, strict=True):
        for repeat in range(item.pairs):
            for arm in item.order(repeat):
                if stop_requested():
                    return read_suite(output, annotations)
                address = ArmAddress(case_id=case.id, repeat_index=repeat, arm=arm)
                dependencies = baseline if arm is Arm.BASELINE else candidate
                variant = item.baseline if arm is Arm.BASELINE else item.candidate
                progress.append(address, Started())
                result = await take_over(
                    case,
                    restorer,
                    _branch(output, address),
                    dependencies.model,
                    dependencies.prompts,
                    dependencies.documents,
                    item.budget,
                    stop_requested=stop_requested,
                    variant=variant.material,
                )
                execution = execution_record(result)
                # Persist the returned identity before any archive reading or awaited judging.
                progress.append(address, Started(execution=execution))
                observed = _observed(execution)
                progress.append(
                    address, Started(execution=execution, observed_configurations=observed)
                )
                assessment = await assess_sample(
                    case,
                    result,
                    annotations,
                    item.standards,
                    judgment=judge,
                )
                reference = annotations.append_assessment(assessment, id=uuid4().hex)
                progress.append(
                    address,
                    AssessmentAvailable(
                        execution=execution,
                        observed_configurations=observed,
                        assessment=reference,
                    ),
                )
    return read_suite(output, annotations)


def _comparison(
    baseline: SampleAssessment | None, candidate: SampleAssessment | None
) -> PairComparison:
    empty = PairComparison(None, None, None, None, None)
    if baseline is None or candidate is None:
        return empty
    if not baseline.endpoint_reached or not candidate.endpoint_reached:
        return empty
    blocked = {
        Uncertainty.NO_ARCHIVE,
        Uncertainty.NO_TARGET,
        Uncertainty.NO_ENDPOINT,
        Uncertainty.INCOMPLETE_RECORDS,
    }
    if blocked.intersection(baseline.uncertainty) or blocked.intersection(candidate.uncertainty):
        return empty
    before, after = baseline.evidence.metrics, candidate.evidence.metrics
    hp = (
        after.net_hp_change - before.net_hp_change
        if before.net_hp_change is not None and after.net_hp_change is not None
        else None
    )
    potions = (
        len(after.potion_uses) - len(before.potion_uses)
        if before.potion_uses is not None and after.potion_uses is not None
        else None
    )
    known_results = {native.TerminalResult.VICTORY, native.TerminalResult.DEFEAT}
    victory = (
        int(after.native_result is native.TerminalResult.VICTORY)
        - int(before.native_result is native.TerminalResult.VICTORY)
        if before.native_result in known_results and after.native_result in known_results
        else None
    )
    reward = (
        len(after.reward.selected) - len(before.reward.selected)
        if before.reward is not None
        and after.reward is not None
        and Uncertainty.REWARD_EVIDENCE not in baseline.uncertainty + candidate.uncertainty
        else None
    )
    acceptable = (
        (baseline.quality.judgment.acceptable, candidate.quality.judgment.acceptable)
        if isinstance(baseline.quality, Assessed)
        and isinstance(candidate.quality, Assessed)
        and baseline.standards.selection == candidate.standards.selection
        else None
    )
    return PairComparison(hp, potions, victory, reward, acceptable)


def _distribution(values: tuple[int | None, ...]) -> Distribution:
    comparable = tuple(value for value in values if value is not None)
    return Distribution(comparable, min(comparable, default=None), max(comparable, default=None))


def _partition(partition: Partition, pairs: tuple[PairReport, ...]) -> PartitionReport:
    selected = tuple(pair for pair in pairs if pair.partition is partition)
    arms = tuple(arm for pair in selected for arm in (pair.baseline, pair.candidate))
    executions = tuple(
        arm.progress.execution
        for arm in arms
        if not isinstance(arm.progress, NotStarted) and arm.progress.execution is not None
    )
    assessments = tuple(arm.assessment for arm in arms if arm.assessment is not None)
    return PartitionReport(
        partition=partition,
        planned_pairs=len(selected),
        not_started=sum(isinstance(arm.progress, NotStarted) for arm in arms),
        started_without_assessment=sum(isinstance(arm.progress, Started) for arm in arms),
        assessment_available=len(assessments),
        completed=sum(execution.kind is ExecutionKind.COMPLETED for execution in executions),
        incomplete=sum(execution.kind is ExecutionKind.INCOMPLETE for execution in executions),
        unsupported=sum(execution.kind is ExecutionKind.UNSUPPORTED for execution in executions),
        endpoints_reached=sum(
            execution.kind is ExecutionKind.COMPLETED for execution in executions
        ),
        unscored=sum(isinstance(assessment.quality, Unscored) for assessment in assessments),
        unassessed=sum(isinstance(assessment.quality, Unassessed) for assessment in assessments),
        assessed=sum(isinstance(assessment.quality, Assessed) for assessment in assessments),
        acceptable_pairs=sum(pair.comparison.acceptable is not None for pair in selected),
        net_hp_change=_distribution(tuple(pair.comparison.net_hp_change for pair in selected)),
        potion_uses=_distribution(tuple(pair.comparison.potion_uses for pair in selected)),
        victory=_distribution(tuple(pair.comparison.victory for pair in selected)),
        reward_added_cards=_distribution(
            tuple(pair.comparison.reward_added_cards for pair in selected)
        ),
    )


def _read_progress(
    output: Path,
    plan: SuitePlan,
    annotations: AnnotationStore,
) -> tuple[dict[ArmAddress, ArmProgress], dict[tuple[str, int], list[Arm]]]:
    states: dict[ArmAddress, ArmProgress] = {}
    order: dict[tuple[str, int], list[Arm]] = {}
    for item, case in zip(plan.spec.cases, plan.cases, strict=True):
        if item.case_id != case.id or annotations.cases.load(case.id) != case:
            raise ValueError("The suite plan does not match its immutable case.")
        for repeat in range(item.pairs):
            for arm in item.order(repeat):
                states[ArmAddress(case_id=case.id, repeat_index=repeat, arm=arm)] = NotStarted()
    for sequence, path in enumerate(sorted((output / "progress").glob("*.json"))):
        if path.name != f"{sequence:08d}.json":
            raise ValueError("Suite progress contains a missing or unordered event.")
        record = ProgressRecord.model_validate_json(path.read_bytes())
        if record.address not in states:
            raise ValueError("Suite progress names an unplanned arm.")
        previous = states[record.address]
        current = record.progress
        if isinstance(previous, NotStarted) and not isinstance(current, NotStarted):
            if not isinstance(current, Started) or current.execution is not None:
                raise ValueError("A suite arm must start before returning execution evidence.")
            key = (record.address.case_id, record.address.repeat_index)
            order.setdefault(key, []).append(record.address.arm)
        if isinstance(previous, AssessmentAvailable) or (
            not isinstance(previous, NotStarted) and isinstance(current, NotStarted)
        ):
            raise ValueError("Suite progress cannot restart or overwrite an arm.")
        if (
            not isinstance(previous, NotStarted)
            and previous.execution is not None
            and (isinstance(current, NotStarted) or previous.execution != current.execution)
        ):
            raise ValueError("Suite progress cannot change a known execution.")
        if (
            not isinstance(current, NotStarted)
            and current.execution is not None
            and (current.execution.branch != _branch(output, record.address))
        ):
            raise ValueError("Suite progress has a mismatched branch.")
        states[record.address] = current
    return states, order


def _check_sample(
    item: SuiteCase,
    address: ArmAddress,
    progress: AssessmentAvailable,
    report: SampleAssessment,
) -> None:
    if report.provenance.sample is None:
        if progress.observed_configurations:
            raise ValueError("An unrecorded suite arm cannot claim observed configurations.")
        return
    declared = item.baseline.material if address.arm is Arm.BASELINE else item.candidate.material
    with closing(Journal.open(report.provenance.archive)) as journal:
        actual = journal.manifest.variant
        if (
            actual is None
            or actual.replacements != replacements(declared)
            or isinstance(declared.start, HistoricalStart)
            and not isinstance(actual, HistoricalVariant)
            or isinstance(declared.start, SceneStart)
            and (
                not isinstance(actual, SceneVariant)
                or journal.manifest.initial_notes != declared.start.initial_notes
            )
        ):
            raise ValueError("The sample variant differs from its suite declaration.")
        start = journal.manifest.parent.before_sequence if journal.manifest.parent else 1
        observed = tuple(
            dict.fromkeys(record.configuration for record in journal.requests(start=start))
        )
        if observed != progress.observed_configurations:
            raise ValueError("Observed suite configurations differ from the actual live requests.")


def _arm_report(
    plan: SuitePlan,
    item: SuiteCase,
    address: ArmAddress,
    state: ArmProgress,
    selected: AssessmentRef | None,
    annotations: AnnotationStore,
) -> ArmReport:
    assessment = None
    reference = None
    if isinstance(state, AssessmentAvailable):
        original = annotations.assessment(state.assessment)
        reference = selected or state.assessment
        assessment = (
            original if reference == state.assessment else annotations.assessment(reference)
        )
        if (
            assessment.execution != state.execution
            or original.execution != state.execution
            or assessment.provenance != original.provenance
            or assessment.evidence != original.evidence
            or assessment.uncertainty != original.uncertainty
            or assessment.provenance.case_id != item.case_id
            or assessment.standards.selection != item.standards
            or original.standards.selection != item.standards
        ):
            raise ValueError("The selected assessment does not bind this suite arm and standards.")
        _check_sample(item, address, state, assessment)
    return ArmReport(
        plan.baseline if address.arm is Arm.BASELINE else plan.candidate,
        state,
        assessment,
        reference,
    )


def read_suite(
    output: Path,
    annotations: AnnotationStore,
    *,
    assessments: tuple[AssessmentChoice, ...] = (),
) -> SuiteReport:
    """Recheck immutable evidence and explicitly selected reassessments without running samples."""
    output = output.resolve()
    plan = SuitePlan.model_validate_json((output / "plan.json").read_bytes())
    states, order = _read_progress(output, plan, annotations)
    choices = {choice.address: choice.assessment for choice in assessments}
    if len(choices) != len(assessments):
        raise ValueError("Select at most one assessment for each suite arm.")
    if any(
        address not in states or not isinstance(states[address], AssessmentAvailable)
        for address in choices
    ):
        raise ValueError("Reassessment requires an existing completed assessment for that arm.")
    pairs: list[PairReport] = []
    for item in plan.spec.cases:
        for repeat in range(item.pairs):
            reports: list[ArmReport] = []
            for arm in (Arm.BASELINE, Arm.CANDIDATE):
                address = ArmAddress(case_id=item.case_id, repeat_index=repeat, arm=arm)
                reports.append(
                    _arm_report(
                        plan,
                        item,
                        address,
                        states[address],
                        choices.get(address),
                        annotations,
                    )
                )
            before, after = reports
            pairs.append(
                PairReport(
                    item.case_id,
                    repeat,
                    item.partition,
                    tuple(order.get((item.case_id, repeat), ())),
                    before,
                    after,
                    _comparison(before.assessment, after.assessment),
                )
            )
    result = tuple(pairs)
    return SuiteReport(
        plan,
        result,
        tuple(_partition(partition, result) for partition in Partition),
        sum(2 * item.pairs * item.budget.requests for item in plan.spec.cases),
    )
