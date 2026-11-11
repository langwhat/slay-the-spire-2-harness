import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import httpx2 as httpx
import pytest
from test_semantic_judge import AcceptableJudge, Actor
from test_takeover import FakeRestorer, retain_case, source_case

from sts2_harness.journal import ArchiveError, Journal, SceneVariant
from sts2_harness.llm.messages import ModelInput, ToolDefinition
from sts2_harness.materials import FileDocuments
from sts2_harness.note_values import NotesState
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval import take_over
from sts2_harness_eval.annotations import (
    AnnotationStore,
    BattleRubric,
    HumanAuthor,
    Origin,
    RubricRecord,
)
from sts2_harness_eval.assessment import AssessmentSelection, assess_sample
from sts2_harness_eval.assessment_records import (
    Assessed,
    SceneInitialRef,
)
from sts2_harness_eval.cases import Case
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted
from sts2_harness_eval.variants import MaterialVariant, SceneStart


class FreshRestorer(FakeRestorer):
    def __init__(self, directory: Path, case: Case) -> None:
        super().__init__(directory, case)
        state = self.observation.state
        self.observation = self.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={"player": state.player.model_copy(update={"hp": 17})}
                )
            }
        )

    def respond(self, request: httpx.Request) -> httpx.Response:
        response = super().respond(request)
        if request.url.path == "/action":
            state = self.observation.state
            self.observation = self.observation.model_copy(
                update={
                    "state": state.model_copy(
                        update={
                            "player": state.player.model_copy(update={"hp": state.player.hp - 2})
                        }
                    )
                }
            )
        return response


def test_scene_assessment_binds_fresh_initial_archive_and_anchor_then_reopens(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = await source_case(tmp_path / "source.sqlite", start=False)
        cases, case = retain_case(tmp_path / "cases", source)
        annotations = AnnotationStore(tmp_path / "annotations", cases)
        actors: list[list[tuple[ModelInput, tuple[ToolDefinition, ...]]]] = []
        selection = AssessmentSelection()
        judge = AcceptableJudge()
        for name in ("plain", "judged"):
            branch = tmp_path / f"{name}.sqlite"
            async with Actor(tmp_path, '{"steps":[{"kind":"end_turn"}]}') as actor:
                result = await take_over(
                    case,
                    FreshRestorer(tmp_path, case),
                    branch,
                    actor,
                    PackagedPrompts(),
                    FileDocuments(),
                    SampleBudget(3, 30),
                    variant=MaterialVariant(SceneStart(NotesState(other="Fresh plan.")), ()),
                    stop_requested=lambda: False,
                )
            assert isinstance(result, TakeoverCompleted)
            actors.append(actor.inputs)
            report = await assess_sample(case, result, annotations, selection, judgment=judge)
            assert report.endpoint_reached
            assert isinstance(report.provenance.target, SceneInitialRef)
            assert report.provenance.sample is not None
            assert report.provenance.target.recording_id == result.recording_id
            assert report.provenance.sample.recording_id == result.recording_id
            assert report.provenance.parent is None
            assert report.evidence.metrics.target_hp == 17
            assert report.evidence.metrics.endpoint_hp == 13
            assert report.evidence.metrics.net_hp_change == -4
            assert len(report.evidence.actions) == 2
            with closing(Journal.open(branch)) as journal:
                assert isinstance(journal.manifest.variant, SceneVariant)
                assert report.provenance.variant == journal.manifest.variant
                assert journal.manifest.variant.initial.observation.location.node_id == "B-battle"
                summaries = journal.summaries()
                assert report.provenance.first_sequence == min(item.sequence for item in summaries)
                assert report.provenance.last_sequence == max(item.sequence for item in summaries)
                first = journal.requests()[0]
                assert report.provenance.first_sequence is not None
                assert report.provenance.first_sequence < first.sequence
                assert result.anchor.anchor.model_sequence == first.sequence
                assert first.sequence != case.source.anchor.model_sequence
            reference = annotations.append_assessment(report, id=name)
            assert AnnotationStore(annotations.directory, cases).assessment(reference) == report
            if name == "plain":
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
                continue
            assert isinstance(report.quality, Assessed)
            assert judge.inputs[-1].evidence.metrics.target_hp == 17
            assert judge.inputs[-1].evidence.metrics.net_hp_change == -4
            received = judge.inputs[-1]
            assert received.target is not None and received.endpoint is not None
            assert received.target.ref == report.provenance.target
            assert received.target.observation.state.player.hp == 17
            assert received.endpoint.ref == report.provenance.endpoint
            assert received.endpoint.observation.state.player.hp == 13
            assert received.before_actions[0].before.observation == received.target.observation
            assert isinstance(received.target.ref, SceneInitialRef)
            with pytest.raises(ValueError, match="anchor"):
                await assess_sample(
                    case,
                    replace(result, anchor=replace(result.anchor, anchor=case.source.anchor)),
                    annotations,
                    selection,
                    judgment=judge,
                )
            forged = report.model_copy(
                update={
                    "provenance": report.provenance.model_copy(
                        update={"target": SceneInitialRef(recording_id="sibling")}
                    )
                }
            )
            with pytest.raises(ValueError, match="target"):
                annotations.append_assessment(forged, id="wrong-target")
            with closing(Journal.open(branch)) as journal:
                manifest = journal.manifest
                declaration = manifest.variant
            assert isinstance(declaration, SceneVariant)
            original = branch.read_bytes()
            damaged = (
                declaration.model_copy(update={"case_id": "another-case"}),
                declaration.model_copy(
                    update={
                        "source": declaration.source.model_copy(update={"archive_sha256": "0" * 64})
                    }
                ),
                declaration.model_copy(
                    update={
                        "initial": declaration.initial.model_copy(
                            update={
                                "restore": declaration.initial.restore.model_copy(
                                    update={"checkpoint_sha256": "0" * 64}
                                )
                            }
                        )
                    }
                ),
            )
            for variant in damaged:
                with closing(sqlite3.connect(branch)) as connection:
                    connection.execute(
                        "UPDATE manifest SET value = ?",
                        (manifest.model_copy(update={"variant": variant}).model_dump_json(),),
                    )
                    connection.commit()
                with pytest.raises(ValueError, match="Scene"):
                    await assess_sample(case, result, annotations, selection, judgment=judge)
            branch.write_bytes(original)
            assert AnnotationStore(annotations.directory, cases).assessment(reference) == report
            branch.write_bytes(b"changed scene archive")
            with pytest.raises((ValueError, ArchiveError)):
                AnnotationStore(annotations.directory, cases).assessment(reference)
        assert actors[0] == actors[1]

    asyncio.run(scenario())
