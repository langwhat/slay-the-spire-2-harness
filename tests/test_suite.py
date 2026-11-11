import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx2 as httpx
from test_combat import reply
from test_responses import configuration
from test_scene_assessment import FreshRestorer
from test_takeover import retain_case, source_case

from sts2_harness.journal import Journal
from sts2_harness.journal_models import MessageSnapshot, ToolSnapshot
from sts2_harness.llm.client import Llm
from sts2_harness.materials import FileDocuments
from sts2_harness.note_values import NotesState
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval.annotations import (
    AnnotationStore,
)
from sts2_harness_eval.assessment_records import (
    AssessmentSelection,
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
    AssessmentAvailable,
    Partition,
    SuiteCase,
    SuiteSpec,
    SuiteVariant,
)
from sts2_harness_eval.variants import (
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
        baseline=SuiteVariant(
            name="baseline", material=MaterialVariant(SceneStart(NotesState(other="Plan.")), ())
        ),
        candidate=SuiteVariant(
            name="candidate", material=MaterialVariant(SceneStart(NotesState(other="Plan.")), ())
        ),
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
