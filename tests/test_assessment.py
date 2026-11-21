import asyncio
from contextlib import closing
from hashlib import file_digest
from pathlib import Path
from typing import Literal

import pytest
from pydantic import TypeAdapter
from test_annotations import cases_fixture
from test_lifecycle import battle_state, run_state, terminal

from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.game import GameReply, Observe, Observed, Settle, Settlement
from sts2_harness.journal import ArchiveError, Journal, ParentPrefix
from sts2_harness_eval.annotations import (
    AnnotationStore,
)
from sts2_harness_eval.assessment import (
    AssessmentSelection,
    archive_identity,
    assess_sample,
)
from sts2_harness_eval.cases import Case, RunEndSpec
from sts2_harness_eval.execution_record import (
    BattleEndpoint,
    CompletedExecution,
    ExecutionRecord,
    RunEndpoint,
    execution_record,
)
from sts2_harness_eval.results import Reached
from sts2_harness_eval.sample import BattleEnd, Completed, Incomplete, StopCause
from sts2_harness_eval.takeover import (
    ProtocolMismatch,
    TakeoverCompleted,
    TakeoverIncomplete,
    TakeoverUnsupported,
)


def new_branch(path: Path, case: Case) -> Journal:
    with case.source.archive.open("rb") as source:
        digest = file_digest(source, "sha256").hexdigest()
    with closing(Journal.open(case.source.archive)) as source:
        manifest = source.manifest.model_copy(
            update={
                "recording_id": path.stem,
                "parent": ParentPrefix(
                    archive=case.source.archive,
                    recording_id=case.source.anchor.recording_id,
                    before_sequence=case.source.anchor.model_sequence,
                    archive_sha256=digest,
                ),
            }
        )
    return Journal.create(path, manifest)


def write_step(
    journal: Journal,
    before: native.Observation,
    step: native.NativeStep,
    operation_id: str,
    after: native.Observation | None,
    *,
    boundary: Literal[
        native.InputReadiness.READY, native.InputReadiness.SELECTION, native.InputReadiness.TERMINAL
    ] = native.InputReadiness.READY,
) -> None:
    route = native.RouteKey.model_validate(before.location.model_dump())
    journal.begin(Settle(route=route, step=step))
    journal.finish(
        GameReply(
            outcome=Settlement(
                request=native.StepRequest(
                    operation_id=operation_id, expected_route=route, step=step
                ),
                receipt=native.ReceiptCompleted(
                    operation_id=operation_id,
                    status=native.ResultStatus.COMPLETED,
                    boundary=boundary,
                ),
            ),
            observations=(after,) if after is not None else (),
        )
    )
    journal.end()


def completed(case: Case, branch: Path) -> TakeoverCompleted:
    return TakeoverCompleted(
        branch,
        Reached(case.source.anchor, case.source.target_cursor, case.source.semantic_digest),
        Completed(BattleEnd("battle"), 1, 1),
        recording_id=archive_identity(branch).recording_id,
    )


def test_persisted_execution_can_be_reassessed_without_replaying_game(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    branch = tmp_path / "branch.sqlite"
    observation = battle_state(1, "battle")
    with closing(new_branch(branch, case)) as journal:
        write_step(
            journal, observation, native.EndTurn(kind=native.StepKind.END_TURN), "turn", observation
        )
    execution = completed(case, branch)
    record = TypeAdapter(ExecutionRecord).validate_json(
        execution_record(execution).model_dump_json()
    )
    assert isinstance(record, CompletedExecution)
    store = AnnotationStore(tmp_path / "annotations", cases)
    first = asyncio.run(assess_sample(case, execution, store, AssessmentSelection(), judgment=None))
    saved = store.append_assessment(first, id="first")
    reopened = AnnotationStore(store.directory, cases).assessment(saved)
    before = branch.read_bytes()
    second = asyncio.run(
        assess_sample(case, reopened.execution, store, AssessmentSelection(), judgment=None)
    )
    assert second == first
    assert record == second.execution
    assert branch.read_bytes() == before
    assert isinstance(record.endpoint, BattleEndpoint)
    bad = record.model_copy(update={"endpoint": BattleEndpoint(node_id="another-battle")})
    with pytest.raises(ValueError, match="endpoint"):
        asyncio.run(assess_sample(case, bad, store, AssessmentSelection(), judgment=None))


@pytest.mark.parametrize("ending", ["victory", "defeat", "combat", "escaped", "other_run"])
def test_completed_run_requires_native_victory_or_defeat_in_the_sample(
    tmp_path: Path, ending: str
) -> None:
    cases = cases_fixture(tmp_path / "cases", RunEndSpec())
    case = cases.load("battle")
    branch = tmp_path / "run.sqlite"
    opening = battle_state(1, "battle")
    final = opening if ending == "combat" else terminal(opening)
    if ending == "other_run":
        final = final.model_copy(
            update={"location": final.location.model_copy(update={"run_id": "other"})}
        )
    elif ending in {"defeat", "escaped"}:
        final = final.model_copy(
            update={
                "state": final.state.model_copy(update={"result": native.TerminalResult(ending)})
            }
        )
    with closing(new_branch(branch, case)) as journal:
        journal.begin(Observe())
        journal.finish(GameReply(outcome=Observed(observation=final), observations=(final,)))
        journal.end()
    execution = execution_record(completed(case, branch)).model_copy(
        update={"endpoint": RunEndpoint()}
    )
    store = AnnotationStore(tmp_path / "annotations", cases)
    if ending not in {"victory", "defeat"}:
        with pytest.raises(ValueError, match="no native victory or defeat"):
            asyncio.run(assess_sample(case, execution, store, AssessmentSelection(), judgment=None))
        partial = TakeoverIncomplete(
            branch,
            completed(case, branch).anchor,
            Incomplete(StopCause.TIME_BUDGET, 1, 1),
            recording_id=archive_identity(branch).recording_id,
        )
        report = asyncio.run(
            assess_sample(case, partial, store, AssessmentSelection(), judgment=None)
        )
        forged = report.model_copy(update={"execution": execution, "endpoint_reached": True})
        with pytest.raises(ValueError, match="no native victory or defeat"):
            store.append_assessment(forged, id="forged")
        return
    report = asyncio.run(
        assess_sample(case, execution, store, AssessmentSelection(), judgment=None)
    )
    assert report.endpoint_reached
    assert report.evidence.metrics.native_result == native.TerminalResult(ending)
    saved = store.append_assessment(report, id="valid")
    assert store.assessment(saved) == report
    bad_metrics = report.evidence.metrics.model_copy(update={"native_result": None})
    forged = report.model_copy(
        update={"evidence": report.evidence.model_copy(update={"metrics": bad_metrics})}
    )
    with pytest.raises(ValueError, match="verified native run endpoint"):
        store.append_assessment(forged, id="forged")


@pytest.mark.parametrize("ending", ["healed", "missing", "changed_run", "ordinary_exit"])
def test_facts_count_confirmed_uses_and_healing_without_inventing_victory(
    tmp_path: Path,
    ending: str,
) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    store = AnnotationStore(tmp_path / "annotations", cases)
    opening = battle_state(1, "battle")
    assert isinstance(opening.state, native.CombatState)
    potion = native.PotionEntry(
        kind=native.ObjectKind.POTION,
        definition_id="POTION.HEALTH",
        name="Health",
        usable=True,
        needs_target=False,
        target_type=None,
        usage=None,
    )
    opening = opening.model_copy(
        update={
            "objects": {**opening.objects, "p1": potion, "p2": potion},
            "state": opening.state.model_copy(update={"potions": ("p1", "p2")}),
        }
    )
    healed = opening.model_copy(
        update={
            "state": opening.state.model_copy(
                update={
                    "player": opening.state.player.model_copy(update={"hp": 15, "max_hp": 20}),
                    "potions": ("p2",),
                }
            )
        }
    )
    branch = tmp_path / "facts.sqlite"
    with closing(new_branch(branch, case)) as journal:
        journal.begin(Observe())
        journal.finish(GameReply(outcome=Observed(observation=opening), observations=(opening,)))
        journal.end()
        use = native.UsePotion(kind=native.StepKind.USE_POTION, potion="p1")
        write_step(journal, opening, use, "used", healed)
        # Repeated confirmation and publication of one operation is still one use.
        write_step(journal, opening, use, "used", healed)
        write_step(
            journal,
            healed,
            native.DiscardPotion(kind=native.StepKind.DISCARD_POTION, potion="p2"),
            "discarded",
            healed,
        )
        # A native inventory decrease without UsePotion is not evidence of use.
        final = healed.model_copy(update={"state": healed.state.model_copy(update={"potions": ()})})
        if ending == "changed_run":
            final = final.model_copy(
                update={"location": final.location.model_copy(update={"run_id": "different"})}
            )
        if ending == "ordinary_exit":
            final = run_state(1, "reward")
        if ending == "missing":
            write_step(journal, healed, native.EndTurn(kind=native.StepKind.END_TURN), "last", None)
        else:
            journal.begin(Observe())
            journal.finish(GameReply(outcome=Observed(observation=final), observations=(final,)))
            journal.end()
    report = asyncio.run(
        assess_sample(case, completed(case, branch), store, AssessmentSelection(), judgment=None)
    )
    uses = report.evidence.metrics.potion_uses
    assert uses is not None
    assert [(use.operation_id, use.definition_id) for use in uses] == [("used", "POTION.HEALTH")]
    assert report.evidence.metrics.net_hp_change == (
        5 if ending == "healed" else 0 if ending == "ordinary_exit" else None
    )
    assert report.evidence.metrics.native_result is None


def test_reports_reject_replaced_archive_and_preserve_unstarted_failure(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    store = AnnotationStore(tmp_path / "annotations", cases)
    failure = TakeoverUnsupported(
        tmp_path / "absent.sqlite",
        ProtocolMismatch(Protocol.RESPONSES, Protocol.CHAT_COMPLETIONS),
        recording_id=None,
    )
    report = asyncio.run(assess_sample(case, failure, store, AssessmentSelection(), judgment=None))
    assert report.provenance.sample is None
    assert report.evidence.metrics.potion_uses is None
    assert report.evidence.metrics.net_hp_change is None
    assert not report.endpoint_reached
    reference = store.append_assessment(report, id="unstarted")
    assert store.assessment(reference) == report
    branch = tmp_path / "branch.sqlite"
    observation = battle_state(1, "battle")
    with closing(new_branch(branch, case)) as journal:
        write_step(
            journal, observation, native.EndTurn(kind=native.StepKind.END_TURN), "turn", observation
        )
    report = asyncio.run(
        assess_sample(case, completed(case, branch), store, AssessmentSelection(), judgment=None)
    )
    reference = store.append_assessment(report, id="recorded")
    branch.write_bytes(b"replaced archive")
    with pytest.raises((ValueError, ArchiveError)):
        store.assessment(reference)
