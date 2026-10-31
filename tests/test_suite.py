import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_responses import configuration
from test_scene_assessment import FreshRestorer
from test_semantic_judge import AcceptableJudge
from test_takeover import retain_case, source_case

from sts2_harness.journal import Journal
from sts2_harness.journal_models import MessageSnapshot, ToolSnapshot
from sts2_harness.llm.client import Llm
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval.annotations import (
    AnnotationStore,
    BattleRubric,
    HumanAuthor,
    Origin,
    RubricRecord,
)
from sts2_harness_eval.assessment_records import (
    Assessed,
    AssessmentSelection,
    HumanJudgment,
    Judged,
    JudgmentInput,
    Unassessed,
    Uncertainty,
    Unscored,
)
from sts2_harness_eval.cases import Case
from sts2_harness_eval.checkpoints import CheckpointUncertain
from sts2_harness_eval.execution_record import CompletedExecution
from sts2_harness_eval.native import LoadedTarget, NativeLaunch, NativeRestorer
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.suite import ArmDependencies, read_suite, run_suite
from sts2_harness_eval.suite_records import (
    Arm,
    ArmAddress,
    AssessmentAvailable,
    AssessmentChoice,
    NotStarted,
    Partition,
    Started,
    SuiteCase,
    SuitePlan,
    SuiteSpec,
    SuiteVariant,
)
from sts2_harness_eval.variants import (
    HistoricalStart,
    MaterialReplacement,
    MaterialVariant,
    SceneStart,
)


class SuiteRestorer(NativeRestorer):
    def __init__(self, directory: Path, annotations: AnnotationStore) -> None:
        super().__init__(NativeLaunch(directory, directory, directory), annotations.cases)
        self.directory = directory
        self.closed = 0

    @asynccontextmanager
    async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
        child = FreshRestorer(self.directory, case)
        async with child.loaded(case) as loaded:
            yield loaded
        self.closed += 1


def actor(directory: Path, *, model: str = "suite-live") -> Llm:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=reply(("turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            headers={"Content-Type": "text/event-stream"},
        )

    return Llm(configuration(directory, LLM_MODEL=model), transport=httpx.MockTransport(respond))


def specification(case: Case, *, pairs: int = 2, requests: int = 2) -> SuiteCase:
    return SuiteCase(
        case_id=case.id,
        partition=Partition.DEVELOPMENT,
        pairs=pairs,
        baseline=SuiteVariant(name="baseline", material=MaterialVariant(SceneStart("Plan."), ())),
        candidate=SuiteVariant(name="candidate", material=MaterialVariant(SceneStart("Plan."), ())),
        budget=SampleBudget(requests, 30),
        standards=AssessmentSelection(),
    )


def test_suite_pairs_real_samples_alternates_order_and_reopens_partitions(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        _, held_out = retain_case(tmp_path / "cases", source.model_copy(update={"id": "held-out"}))
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        restorer = SuiteRestorer(tmp_path, annotations)
        spec = SuiteSpec(
            cases=(
                specification(case),
                specification(held_out, pairs=1).model_copy(
                    update={"partition": Partition.HELD_OUT}
                ),
            )
        )
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            report = await run_suite(
                spec,
                cases,
                restorer,
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: False,
            )
        assert restorer.closed == 6
        assert [pair.order for pair in report.pairs] == [
            (Arm.BASELINE, Arm.CANDIDATE),
            (Arm.CANDIDATE, Arm.BASELINE),
            (Arm.BASELINE, Arm.CANDIDATE),
        ]
        ids: set[str] = set()
        first_inputs: list[tuple[tuple[MessageSnapshot, ...], tuple[ToolSnapshot, ...]]] = []
        for pair in report.pairs:
            assert pair.comparison.net_hp_change == 0
            assert pair.comparison.potion_uses == 0
            assert pair.comparison.victory is None
            assert pair.comparison.acceptable is None
            for arm in (pair.baseline, pair.candidate):
                assert isinstance(arm.progress, AssessmentAvailable)
                assert arm.assessment is not None
                assert isinstance(arm.assessment.quality, Unscored)
                assert isinstance(arm.assessment.execution, CompletedExecution)
                assert arm.assessment.execution.requests == 2
                assert arm.assessment.evidence.metrics.net_hp_change == -4
                assert arm.assessment.provenance.sample is not None
                ids.add(arm.assessment.provenance.sample.recording_id)
                assert len(arm.progress.observed_configurations) == 1
                with closing(Journal.open(arm.assessment.provenance.archive)) as journal:
                    requests = journal.requests()
                    request = requests[0]
                    assert arm.progress.observed_configurations == (request.configuration,)
                    assert request.configuration.capacity_policy == journal.manifest.capacity_policy
                    assert request.configuration.capacity_policy.context_window == 300_000
                    assert len(requests) == 2
                    first_inputs.append((request.input, request.tools))
        assert len(ids) == 6
        # Identical full gameplay inputs across partitions exclude suite-only evaluator data.
        assert all(value == first_inputs[0] for value in first_inputs)
        development, held = report.partitions
        assert development.partition is Partition.DEVELOPMENT
        assert development.planned_pairs == 2
        assert development.assessment_available == 4
        assert development.net_hp_change.values == (0, 0)
        assert development.net_hp_change.minimum == development.net_hp_change.maximum == 0
        assert development.acceptable_pairs == 0
        assert held.partition is Partition.HELD_OUT
        assert held.planned_pairs == 1
        assert report.planned_live_requests == 12
        assert read_suite(tmp_path / "suite", annotations) == report

    asyncio.run(scenario())


def test_budget_stops_remain_in_denominator_without_metric_or_quality_regression(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        restorer = SuiteRestorer(tmp_path, annotations)
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            report = await run_suite(
                SuiteSpec(cases=(specification(case, requests=1),)),
                cases,
                restorer,
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: False,
            )
        partition = report.partitions[0]
        assert partition.planned_pairs == 2
        assert partition.incomplete == 4
        assert partition.completed == partition.endpoints_reached == 0
        assert partition.unscored == 4
        assert partition.net_hp_change.values == ()
        assert partition.net_hp_change.minimum is None
        assert restorer.closed == 4
        for pair in report.pairs:
            assert pair.comparison.net_hp_change is None
            assert pair.comparison.potion_uses is None
            assert pair.baseline.assessment is not None
            assert pair.baseline.assessment.evidence.metrics.net_hp_change == -2

    asyncio.run(scenario())


def test_explicit_stop_reports_only_actual_order_and_keeps_completed_sample(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        restorer = SuiteRestorer(tmp_path, annotations)
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            report = await run_suite(
                SuiteSpec(cases=(specification(case),)),
                cases,
                restorer,
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: restorer.closed == 1,
            )
        assert len(report.pairs) == 2
        assert report.pairs[0].order == (Arm.BASELINE,)
        assert report.pairs[1].order == ()
        assert report.partitions[0].assessment_available == 1
        assert report.partitions[0].not_started == 3
        assert restorer.closed == 1

    asyncio.run(scenario())


def with_rubric(annotations: AnnotationStore, case: Case) -> SuiteCase:
    rubric = annotations.append_rubric(
        RubricRecord(
            case_id=case.id,
            id="battle",
            origin=Origin.HUMAN,
            author=HumanAuthor(id="reviewer"),
            criterion=BattleRubric(description="Preserve HP."),
        )
    )
    return specification(case).model_copy(update={"standards": AssessmentSelection(rubric=rubric)})


def test_human_reassessment_requires_explicit_refs_for_same_sample_and_standards(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        item = with_rubric(annotations, case)
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            original = await run_suite(
                SuiteSpec(cases=(item,)),
                cases,
                SuiteRestorer(tmp_path, annotations),
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: False,
            )
        choices: list[AssessmentChoice] = []
        for arm, report in (
            (Arm.BASELINE, original.pairs[0].baseline),
            (Arm.CANDIDATE, original.pairs[0].candidate),
        ):
            assessment = report.assessment
            assert assessment is not None and assessment.provenance.sample is not None
            assert isinstance(assessment.quality, Unassessed)
            reviewed = assessment.model_copy(
                update={
                    "quality": Assessed(
                        judgment=HumanJudgment(
                            case_id=case.id,
                            sample=assessment.provenance.sample,
                            selection=item.standards,
                            acceptable=arm is Arm.BASELINE,
                            rationale="Reviewed the complete confirmed sample.",
                            author=HumanAuthor(id="reviewer"),
                        )
                    )
                }
            )
            reference = annotations.append_assessment(reviewed, id=f"human-{arm}")
            choices.append(
                AssessmentChoice(
                    address=ArmAddress(case_id=case.id, repeat_index=0, arm=arm),
                    assessment=reference,
                )
            )
        assert read_suite(tmp_path / "suite", annotations) == original
        reviewed = read_suite(tmp_path / "suite", annotations, assessments=tuple(choices))
        assert reviewed.pairs[0].comparison.acceptable == (True, False)
        assert reviewed.partitions[0].acceptable_pairs == 1
        assert reviewed.partitions[0].planned_pairs == 2
        assert reviewed.partitions[0].net_hp_change == original.partitions[0].net_hp_change
        with pytest.raises(ValueError, match="suite arm"):
            read_suite(
                tmp_path / "suite",
                annotations,
                assessments=(
                    choices[0].model_copy(
                        update={"assessment": choices[1].assessment},
                    ),
                ),
            )
        baseline_report = original.pairs[0].baseline.assessment
        assert baseline_report is not None
        different_standard = baseline_report.model_copy(
            update={
                "standards": annotations.select(case, AssessmentSelection()),
                "quality": Unscored(),
            }
        )
        other = annotations.append_assessment(different_standard, id="different-standards")
        with pytest.raises(ValueError, match="standards"):
            read_suite(
                tmp_path / "suite",
                annotations,
                assessments=(
                    choices[0].model_copy(
                        update={"assessment": other},
                    ),
                ),
            )
        plan_path = tmp_path / "suite" / "plan.json"
        original_plan = plan_path.read_bytes()
        plan = SuitePlan.model_validate_json(original_plan)
        changed_item = item.model_copy(
            update={
                "baseline": SuiteVariant(
                    name="baseline", material=MaterialVariant(SceneStart("Other."), ())
                ),
                "candidate": SuiteVariant(
                    name="candidate", material=MaterialVariant(SceneStart("Other."), ())
                ),
            }
        )
        changed_plan = plan.model_copy(update={"spec": SuiteSpec(cases=(changed_item,))})
        plan_path.write_text(changed_plan.model_dump_json())
        with pytest.raises(ValueError, match="variant"):
            read_suite(tmp_path / "suite", annotations)
        plan_path.write_bytes(original_plan)
        archive = baseline_report.provenance.archive
        original_bytes = archive.read_bytes()
        archive.write_bytes(original_bytes + b"changed")
        with pytest.raises(ValueError, match="changed"):
            read_suite(tmp_path / "suite", annotations, assessments=tuple(choices))

    asyncio.run(scenario())


@pytest.mark.parametrize("changed_field", ["metrics", "uncertainty"])
def test_reassessment_cannot_replace_sample_facts(tmp_path: Path, changed_field: str) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            original = await run_suite(
                SuiteSpec(cases=(specification(case, pairs=1),)),
                cases,
                SuiteRestorer(tmp_path, annotations),
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: False,
            )
        assessment = original.pairs[0].baseline.assessment
        assert assessment is not None
        assert isinstance(assessment.execution, CompletedExecution)
        if changed_field == "metrics":
            changed = assessment.model_copy(
                update={
                    "evidence": assessment.evidence.model_copy(
                        update={
                            "metrics": assessment.evidence.metrics.model_copy(
                                update={"net_hp_change": 50}
                            )
                        }
                    )
                }
            )
        else:
            changed = assessment.model_copy(
                update={"uncertainty": (Uncertainty.INCOMPLETE_RECORDS,)}
            )
        reference = annotations.append_assessment(changed, id="changed-facts")
        with pytest.raises(ValueError, match="suite arm"):
            read_suite(
                tmp_path / "suite",
                annotations,
                assessments=(
                    AssessmentChoice(
                        address=ArmAddress(case_id=case.id, repeat_index=0, arm=Arm.BASELINE),
                        assessment=reference,
                    ),
                ),
            )

    asyncio.run(scenario())


class InterruptedRestorer(SuiteRestorer):
    def __init__(
        self, directory: Path, annotations: AnnotationStore, entered: asyncio.Event
    ) -> None:
        super().__init__(directory, annotations)
        self.entered = entered

    @asynccontextmanager
    async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
        if self.closed == 1:
            self.entered.set()
            await asyncio.Event().wait()
        async with super().loaded(case) as loaded:
            yield loaded


class InterruptedJudge(AcceptableJudge):
    def __init__(self, entered: asyncio.Event) -> None:
        super().__init__()
        self.entered = entered

    async def assess(self, input: JudgmentInput) -> Judged:
        if len(self.inputs) == 1:
            self.entered.set()
            await asyncio.Event().wait()
        return await super().assess(input)


@pytest.mark.parametrize("stage", ["native", "judge"])
def test_cancellation_preserves_three_states_and_only_known_execution_identity(
    tmp_path: Path,
    stage: str,
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        entered = asyncio.Event()
        restorer = (
            InterruptedRestorer(tmp_path, annotations, entered)
            if stage == "native"
            else SuiteRestorer(tmp_path, annotations)
        )
        judge = InterruptedJudge(entered) if stage == "judge" else AcceptableJudge()
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            task = asyncio.create_task(
                run_suite(
                    SuiteSpec(cases=(with_rubric(annotations, case),)),
                    cases,
                    restorer,
                    annotations,
                    ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                    ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                    tmp_path / "suite",
                    stop_requested=lambda: False,
                    judge=judge,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        report = read_suite(tmp_path / "suite", annotations)
        first, second = report.pairs
        assert isinstance(first.baseline.progress, AssessmentAvailable)
        assert isinstance(first.candidate.progress, Started)
        assert first.candidate.assessment is None
        assert isinstance(second.baseline.progress, NotStarted)
        assert isinstance(second.candidate.progress, NotStarted)
        assert first.order == (Arm.BASELINE, Arm.CANDIDATE)
        assert second.order == ()
        assert report.partitions[0].assessment_available == 1
        assert report.partitions[0].started_without_assessment == 1
        assert report.partitions[0].not_started == 2
        if stage == "native":
            assert first.candidate.progress.execution is None
            assert first.candidate.progress.observed_configurations == ()
            assert restorer.closed == 1
        else:
            execution = first.candidate.progress.execution
            assert isinstance(execution, CompletedExecution)
            assert execution.recording_id != first.baseline.progress.execution.recording_id
            assert len(first.candidate.progress.observed_configurations) == 1
            assert restorer.closed == 2
            assert report.partitions[0].endpoints_reached == 2

    asyncio.run(scenario())


def test_historical_material_pair_records_only_new_request_configurations(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        original_source_bytes = source.source.archive.read_bytes()
        prompt = PackagedPrompts().read("combat-task-rules")
        item = specification(case, pairs=1).model_copy(
            update={
                "baseline": SuiteVariant(
                    name="baseline", material=MaterialVariant(HistoricalStart(), ())
                ),
                "candidate": SuiteVariant(
                    name="candidate",
                    material=MaterialVariant(
                        HistoricalStart(),
                        (
                            MaterialReplacement(
                                prompt.id, frozenset({prompt.version}), "Act carefully."
                            ),
                        ),
                    ),
                ),
            }
        )
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            report = await run_suite(
                SuiteSpec(cases=(item,)),
                cases,
                SuiteRestorer(tmp_path, annotations),
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: False,
            )
        for arm in (report.pairs[0].baseline, report.pairs[0].candidate):
            assert arm.assessment is not None
            assert arm.assessment.endpoint_reached
            assert arm.assessment.provenance.parent is not None
            assert isinstance(arm.progress, AssessmentAvailable)
            assert tuple(config.model for config in arm.progress.observed_configurations) == (
                "suite-live",
            )
        assert isinstance(report.plan.spec.cases[0].baseline.material.start, HistoricalStart)
        assert read_suite(tmp_path / "suite", annotations) == report
        assert case.source.archive.read_bytes() == original_source_bytes

    asyncio.run(scenario())


class UncertainFirstRestorer(SuiteRestorer):
    def __init__(self, directory: Path, annotations: AnnotationStore) -> None:
        super().__init__(directory, annotations)
        self.attempts = 0

    @asynccontextmanager
    async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
        self.attempts += 1
        if self.attempts == 1:
            raise CheckpointUncertain("Synthetic uncertain load")
        async with super().loaded(case) as loaded:
            yield loaded


def test_unrecorded_failure_keeps_unknown_identity_and_runs_sibling_without_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        restorer = UncertainFirstRestorer(tmp_path, annotations)
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            report = await run_suite(
                SuiteSpec(cases=(specification(case, pairs=1),)),
                cases,
                restorer,
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                tmp_path / "suite",
                stop_requested=lambda: False,
            )
        assert restorer.attempts == 2
        assert restorer.closed == 1
        pair = report.pairs[0]
        assert isinstance(pair.baseline.progress, AssessmentAvailable)
        assert pair.baseline.progress.execution.recording_id is None
        assert pair.baseline.progress.observed_configurations == ()
        assert pair.baseline.assessment is not None
        assert pair.baseline.assessment.provenance.sample is None
        assert pair.baseline.assessment.evidence.metrics.potion_uses is None
        assert pair.candidate.assessment is not None and pair.candidate.assessment.endpoint_reached
        assert pair.comparison.net_hp_change is None
        assert pair.comparison.potion_uses is None
        assert report.partitions[0].planned_pairs == 1
        assert report.partitions[0].incomplete == 1
        assert report.partitions[0].completed == 1

    asyncio.run(scenario())


def test_suite_retains_unsealed_arm_as_unrecorded_and_continues_sibling(tmp_path: Path) -> None:
    import sqlite3

    readers: list[sqlite3.Connection] = []
    output = tmp_path / "suite"

    class ReadingRestorer(FreshRestorer):
        def respond(self, request: httpx.Request) -> httpx.Response:
            response = super().respond(request)
            if request.url.path == "/action" and not readers:
                path = output / "case-0-baseline.sqlite"
                reader = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
                reader.execute("SELECT COUNT(*) FROM records").fetchone()
                readers.append(reader)
            return response

    class Restorer(SuiteRestorer):
        @asynccontextmanager
        async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
            child = ReadingRestorer(tmp_path, case)
            async with child.loaded(case) as loaded:
                yield loaded
            self.closed += 1

    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        restorer = Restorer(tmp_path, annotations)
        judge = AcceptableJudge()
        async with actor(tmp_path) as baseline, actor(tmp_path) as candidate:
            report = await run_suite(
                SuiteSpec(cases=(with_rubric(annotations, case).model_copy(update={"pairs": 1}),)),
                cases,
                restorer,
                annotations,
                ArmDependencies(baseline, PackagedPrompts(), FileDocuments()),
                ArmDependencies(candidate, PackagedPrompts(), FileDocuments()),
                output,
                stop_requested=lambda: False,
                judge=judge,
            )
        first, second = report.pairs[0].baseline, report.pairs[0].candidate
        assert isinstance(first.progress, AssessmentAvailable)
        assert first.progress.execution.recording_id is not None
        assert first.progress.observed_configurations == ()
        assert first.assessment is not None
        assert first.assessment.provenance.sample is None
        assert isinstance(first.assessment.quality, Unassessed)
        assert len(judge.inputs) == 1
        assert first.assessment.uncertainty == (Uncertainty.NO_ARCHIVE,)
        assert first.assessment.evidence.metrics.net_hp_change is None
        assert second.assessment is not None and second.assessment.endpoint_reached
        assert report.partitions[0].incomplete == 1
        assert report.partitions[0].completed == 1
        assert report.pairs[0].comparison.net_hp_change is None
        assert read_suite(output, annotations) == report

    try:
        asyncio.run(scenario())
    finally:
        for reader in readers:
            reader.close()
