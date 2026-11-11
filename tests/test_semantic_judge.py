import asyncio
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_responses import configuration
from test_takeover import FakeRestorer, retain_case, source_case

from sts2_harness.journal import Journal
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    ModelAttemptSink,
    ModelInput,
    ModelReply,
    PreparedRequest,
    RequestPreparation,
    ToolDefinition,
)
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval import take_over
from sts2_harness_eval.annotations import (
    AgentAuthor,
    AnnotationRecord,
    AnnotationStore,
    BattleRubric,
    HumanAuthor,
    Origin,
    ReviewDecision,
    ReviewRecord,
    RubricRecord,
)
from sts2_harness_eval.assessment import AssessmentSelection, assess_sample
from sts2_harness_eval.assessment_records import (
    Assessed,
    Judged,
    JudgeFailed,
    JudgmentInput,
    ObservationRef,
    Unassessed,
    UnassessedReason,
    Unscored,
)
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted


class Actor(Llm):
    def __init__(self, directory: Path, first_action: str) -> None:
        responses = iter(
            [
                reply(("first", "combat_act", first_action)),
                reply(("second", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )

        def respond(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=next(responses), headers={"Content-Type": "text/event-stream"}
            )

        super().__init__(configuration(directory), transport=httpx.MockTransport(respond))
        self.inputs: list[tuple[ModelInput, tuple[ToolDefinition, ...]]] = []

    async def complete_prepared(
        self,
        prepared: PreparedRequest,
        *,
        evidence: ModelAttemptSink | None = None,
        preparation: RequestPreparation | None = None,
    ) -> ModelReply:
        self.inputs.append((prepared.input, prepared.tools))
        return await super().complete_prepared(prepared, evidence=evidence, preparation=preparation)


class AcceptableJudge:
    def __init__(self) -> None:
        self.inputs: list[JudgmentInput] = []

    async def assess(self, input: JudgmentInput) -> Judged:
        self.inputs.append(input)
        return Judged(
            case_id=input.case_id,
            sample=input.sample,
            selection=input.standards.selection,
            acceptable=True,
            rationale="Both confirmed lines preserve the selected battle criterion.",
            author=AgentAuthor(id="judge", model="synthetic-semantic-judge"),
        )


@pytest.mark.parametrize(
    "first_action",
    [
        '{"steps":[{"kind":"play_card","card":"card:1:1","target":"enemy:1:1"}]}',
        '{"steps":[{"kind":"end_turn"}]}',
    ],
)
def test_injected_judgment_reopens_with_permanent_sources_and_isolated_actor(
    tmp_path: Path, first_action: str
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        actors: list[list[tuple[ModelInput, tuple[ToolDefinition, ...]]]] = []
        judge = AcceptableJudge()
        selection = AssessmentSelection()
        for name in ("plain", "standards", "judged"):
            async with Actor(tmp_path, first_action) as actor:
                result = await take_over(
                    case,
                    FakeRestorer(tmp_path, case),
                    tmp_path / f"{name}.sqlite",
                    actor,
                    PackagedPrompts(),
                    FileDocuments(),
                    SampleBudget(3, 30),
                    stop_requested=lambda: False,
                )
            assert isinstance(result, TakeoverCompleted)
            actors.append(actor.inputs)
            if name == "plain":
                annotation = annotations.append_annotation(
                    AnnotationRecord(
                        case_id=case.id,
                        id="agent-lines",
                        origin=Origin.AGENT,
                        author=AgentAuthor(id="annotator", model="synthetic-annotation-agent"),
                        text="Either line is acceptable; the source-run future is evaluator-only.",
                        evidence_refs=(),
                    )
                )
                review = annotations.append_review(
                    ReviewRecord(
                        case_id=case.id,
                        id="human-review",
                        annotation=annotation,
                        decision=ReviewDecision.ACCEPTED,
                        reviewer=HumanAuthor(id="reviewer"),
                        rationale="Both choices are usable standards.",
                    )
                )
                rubric = annotations.append_rubric(
                    RubricRecord(
                        case_id=case.id,
                        id="battle",
                        origin=Origin.HUMAN,
                        author=HumanAuthor(id="reviewer"),
                        criterion=BattleRubric(description="Preserve HP using a valid line."),
                    )
                )
                selection = AssessmentSelection(annotation=annotation, review=review, rubric=rubric)
                continue
            report = await assess_sample(case, result, annotations, selection, judgment=judge)
            assert isinstance(report.quality, Assessed)
            assert isinstance(report.quality.judgment, Judged)
            assert report.quality.judgment.acceptable
            assert report.quality.judgment.author.model == "synthetic-semantic-judge"
            assert report.standards.annotation is not None
            assert report.standards.annotation.origin is Origin.AGENT
            assert report.standards.review is not None
            assert report.standards.review.reviewer == HumanAuthor(id="reviewer")
            assert judge.inputs[-1].standards == annotations.select(case, selection)
            assert judge.inputs[-1].sample == report.provenance.sample
            assert judge.inputs[-1].endpoint_reached
            assert len(judge.inputs[-1].evidence.actions) == 2
            assert judge.inputs[-1].evidence.metrics.net_hp_change == 0
            received = judge.inputs[-1]
            assert received.target is not None and received.endpoint is not None
            assert received.target.ref == report.provenance.target
            assert received.endpoint.ref == report.provenance.endpoint
            target = annotations.cases.reference(annotations.cases.binding(case)).target
            assert received.target.observation == target.read.observation
            with closing(Journal.open(result.branch)) as journal:
                records = journal.operations(start=case.source.anchor.model_sequence)
                assert records[-1].reply is not None
                assert received.endpoint.observation == records[-1].reply.observations[-1]
                assert tuple(item.operation_id for item in received.before_actions) == tuple(
                    action.operation_id for action in report.evidence.actions
                )
                assert received.before_actions[0].before == received.target
                previous = next(
                    record
                    for record in reversed(records)
                    if record.sequence < report.evidence.actions[1].sequence
                    and record.reply is not None
                    and record.reply.observations
                )
                assert previous.reply is not None
                assert (
                    received.before_actions[1].before.observation == previous.reply.observations[-1]
                )
                assert isinstance(received.before_actions[1].before.ref, ObservationRef)
                assert received.before_actions[1].before.ref.sequence == previous.sequence
            reference = annotations.append_assessment(report, id=name)
            assert AnnotationStore(annotations.directory, cases).assessment(reference) == report
        assert len(actors[0]) == 2
        assert actors[0] == actors[1] == actors[2]

    asyncio.run(scenario())


class FailedJudge:
    def __init__(self) -> None:
        self.inputs: list[JudgmentInput] = []

    async def assess(self, input: JudgmentInput) -> JudgeFailed:
        self.inputs.append(input)
        return JudgeFailed(
            case_id=input.case_id,
            sample=input.sample,
            selection=input.standards.selection,
            author=AgentAuthor(id="failed-judge", model="synthetic-unavailable-judge"),
            reason="The caller's judge could not complete its assessment.",
        )


class CancelledJudge:
    async def assess(self, input: JudgmentInput) -> Judged:
        raise asyncio.CancelledError


def test_judge_failure_preserves_execution_facts_and_provenance_without_hiding_cancellation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        async with Actor(tmp_path, '{"steps":[{"kind":"end_turn"}]}') as actor:
            result = await take_over(
                case,
                FakeRestorer(tmp_path, case),
                tmp_path / "sample.sqlite",
                actor,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(3, 30),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        rubric = annotations.append_rubric(
            RubricRecord(
                case_id=case.id,
                id="battle",
                origin=Origin.HUMAN,
                author=HumanAuthor(id="reviewer"),
                criterion=BattleRubric(description="Preserve HP."),
            )
        )
        selection = AssessmentSelection(rubric=rubric)
        judge = FailedJudge()
        unscored = await assess_sample(
            case, result, annotations, AssessmentSelection(), judgment=judge
        )
        assert isinstance(unscored.quality, Unscored)
        assert not judge.inputs
        unassessed = await assess_sample(case, result, annotations, selection, judgment=None)
        assert isinstance(unassessed.quality, Unassessed)
        assert unassessed.quality.reason is UnassessedReason.NO_JUDGMENT
        failed = await assess_sample(case, result, annotations, selection, judgment=judge)
        assert isinstance(failed.quality, Unassessed)
        assert failed.quality.reason is UnassessedReason.JUDGE_FAILED
        failure = failed.quality.failure
        assert failure is not None
        assert failure.author.model == "synthetic-unavailable-judge"
        assert failed.execution == unscored.execution == unassessed.execution
        assert failed.endpoint_reached == unscored.endpoint_reached == unassessed.endpoint_reached
        assert failed.evidence == unscored.evidence == unassessed.evidence
        assert failed.evidence.metrics.net_hp_change == 0
        assert len(failed.evidence.actions) == 2
        reference = annotations.append_assessment(failed, id="failed")
        assert AnnotationStore(annotations.directory, cases).assessment(reference) == failed
        wrong_failure = failure.model_copy(update={"selection": AssessmentSelection()})
        forged = failed.model_copy(
            update={"quality": failed.quality.model_copy(update={"failure": wrong_failure})}
        )
        with pytest.raises(ValueError, match="associations"):
            annotations.append_assessment(forged, id="wrong-failure")

        class MisboundJudge:
            async def assess(self, input: JudgmentInput) -> JudgeFailed:
                return wrong_failure

        with pytest.raises(ValueError, match="judge result"):
            await assess_sample(case, result, annotations, selection, judgment=MisboundJudge())
        with pytest.raises(asyncio.CancelledError):
            await assess_sample(case, result, annotations, selection, judgment=CancelledJudge())
        assert AnnotationStore(annotations.directory, cases).assessment(reference) == failed

    asyncio.run(scenario())
