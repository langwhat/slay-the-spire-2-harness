import asyncio
import sqlite3
from contextlib import closing
from hashlib import file_digest, sha256
from pathlib import Path
from typing import Literal

import pytest
from pydantic import TypeAdapter
from test_annotations import cases_fixture
from test_lifecycle import battle_state, run_state
from test_responses import configuration

from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.game import GameReply, Observe, Observed, Settle, Settlement
from sts2_harness.journal import ArchiveError, HistoricalVariant, Journal, ParentPrefix
from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet
from sts2_harness_eval.annotations import (
    AgentAuthor,
    AnnotationRecord,
    AnnotationStore,
    BattleRubric,
    CardRewardRubric,
    HumanAuthor,
    Origin,
    RubricRecord,
    RubricRef,
)
from sts2_harness_eval.assessment import (
    AssessmentSelection,
    HumanJudgment,
    Unscored,
    archive_identity,
    assess_sample,
)
from sts2_harness_eval.assessment_records import (
    Assessed,
    AssessmentRecord,
    ObservationRef,
    Unassessed,
    UnassessedReason,
    Uncertainty,
)
from sts2_harness_eval.cases import Case, SourceRef
from sts2_harness_eval.execution_record import (
    BattleEndpoint,
    CompletedExecution,
    ExecutionRecord,
    execution_record,
)
from sts2_harness_eval.results import ModelAnchor, Reached
from sts2_harness_eval.sample import BattleEnd, Completed
from sts2_harness_eval.takeover import ProtocolMismatch, TakeoverCompleted, TakeoverUnsupported


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
                    boundary=native.InputReadiness.READY,
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


@pytest.mark.parametrize("change", ["anchor", "endpoint", "variant"])
@pytest.mark.parametrize("operation", ["append", "read"])
def test_historical_reports_revalidate_execution_binding(
    tmp_path: Path,
    change: Literal["anchor", "endpoint", "variant"],
    operation: Literal["append", "read"],
) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    branch = tmp_path / "branch.sqlite"
    observation = battle_state(1, "battle")
    with closing(new_branch(branch, case)) as journal:
        write_step(
            journal, observation, native.EndTurn(kind=native.StepKind.END_TURN), "turn", observation
        )
    store = AnnotationStore(tmp_path / "annotations", cases)
    report = asyncio.run(
        assess_sample(case, completed(case, branch), store, AssessmentSelection(), judgment=None)
    )
    reference = store.append_assessment(report, id="report")
    execution = report.execution
    assert isinstance(execution, CompletedExecution)
    if change == "anchor":
        execution = execution.model_copy(
            update={"anchor": execution.anchor.model_copy(update={"semantic_digest": "0" * 64})}
        )
    elif change == "endpoint":
        execution = execution.model_copy(update={"endpoint": BattleEndpoint(node_id="another")})
    else:
        with closing(Journal.open(branch)) as journal:
            manifest = journal.manifest
        assert manifest.parent is not None
        variant = HistoricalVariant(
            case_id=case.id,
            case_sha256="0" * 64,
            source=manifest.parent,
            replacements=(),
        )
        with closing(sqlite3.connect(branch)) as connection:
            connection.execute(
                "UPDATE manifest SET value = ?",
                (manifest.model_copy(update={"variant": variant}).model_dump_json(),),
            )
            connection.commit()
        report = report.model_copy(
            update={
                "provenance": report.provenance.model_copy(
                    update={"sample": archive_identity(branch), "variant": variant}
                )
            }
        )
    forged = report.model_copy(update={"execution": execution})
    with pytest.raises(ValueError, match=change):
        asyncio.run(assess_sample(case, execution, store, AssessmentSelection(), judgment=None))
    if operation == "append":
        with pytest.raises(ValueError, match=change):
            store.append_assessment(forged, id="forged")
        return
    path = store.directory / case.id / "assessments" / f"{reference.id}.json"
    path.write_text(
        AssessmentRecord(case_id=case.id, id=reference.id, assessment=forged).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=change):
        AnnotationStore(store.directory, cases).assessment(reference)


def test_unscored_report_reopens_with_actual_archive_identity(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    branch = tmp_path / "branch.sqlite"
    with case.source.archive.open("rb") as source:
        digest = file_digest(source, "sha256").hexdigest()
    with closing(Journal.open(case.source.archive)) as source:
        manifest = source.manifest.model_copy(
            update={
                "recording_id": "sample",
                "parent": ParentPrefix(
                    archive=case.source.archive,
                    recording_id="source",
                    before_sequence=case.source.anchor.model_sequence,
                    archive_sha256=digest,
                ),
            }
        )
    with closing(Journal.create(branch, manifest)) as journal:
        observation = battle_state(1, "battle")
        journal.begin(Observe())
        journal.finish(
            GameReply(outcome=Observed(observation=observation), observations=(observation,))
        )
        journal.end()
    execution = TakeoverCompleted(
        branch,
        Reached(case.source.anchor, 0, case.source.semantic_digest),
        Completed(BattleEnd("battle"), 1, 1),
        recording_id=archive_identity(branch).recording_id,
    )
    store = AnnotationStore(tmp_path / "annotations", cases)
    report = asyncio.run(
        assess_sample(case, execution, store, AssessmentSelection(), judgment=None)
    )
    assert isinstance(report.quality, Unscored)
    assert report.provenance.sample is not None
    assert report.provenance.sample.recording_id == "sample"
    reference = store.append_assessment(report, id="first")
    assert AnnotationStore(store.directory, cases).assessment(reference) == report
    forged = report.model_copy(
        update={"execution": report.execution.model_copy(update={"recording_id": "sibling"})}
    )
    with pytest.raises(ValueError, match="recording"):
        store.append_assessment(forged, id="wrong-execution")


@pytest.mark.parametrize(
    "step",
    [
        native.EndTurn(kind=native.StepKind.END_TURN),
        native.PlayCard(kind=native.StepKind.PLAY_CARD, card="c1"),
    ],
)
def test_human_judgment_binds_selected_agent_standard_and_reopens(
    tmp_path: Path,
    step: native.NativeStep,
) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    store = AnnotationStore(tmp_path / "annotations", cases)
    annotation = store.append_annotation(
        AnnotationRecord(
            case_id=case.id,
            id="lines",
            origin=Origin.AGENT,
            author=AgentAuthor(id="author", model="test"),
            text="Either line is acceptable.",
            evidence_refs=(),
        )
    )
    rubric = store.append_rubric(
        RubricRecord(
            case_id=case.id,
            id="battle",
            origin=Origin.HUMAN,
            author=HumanAuthor(id="reviewer"),
            criterion=BattleRubric(description="Preserve HP."),
        )
    )
    selection = AssessmentSelection(annotation=annotation, rubric=rubric)
    branch = tmp_path / "branch.sqlite"
    observation = battle_state(1, "battle")
    with closing(new_branch(branch, case)) as journal:
        write_step(journal, observation, step, "line", observation)
    execution = completed(case, branch)
    judgment = HumanJudgment(
        case_id=case.id,
        sample=archive_identity(branch),
        selection=selection,
        acceptable=True,
        rationale="A valid line.",
        author=HumanAuthor(id="judge"),
    )
    report = asyncio.run(assess_sample(case, execution, store, selection, judgment=judgment))
    assert isinstance(report.quality, Assessed)
    assert report.quality.judgment.acceptable
    assert report.standards.annotation is not None
    assert report.standards.annotation.origin is Origin.AGENT
    reference = store.append_assessment(report, id="judgment")
    assert AnnotationStore(store.directory, cases).assessment(reference) == report
    with pytest.raises(FileExistsError):
        store.append_assessment(report, id="judgment")
    with pytest.raises(ValueError, match="human judgment"):
        asyncio.run(
            assess_sample(
                case,
                execution,
                store,
                selection,
                judgment=judgment.model_copy(update={"selection": AssessmentSelection()}),
            )
        )
    with pytest.raises(ValueError, match="rubric"):
        asyncio.run(
            assess_sample(
                case,
                execution,
                store,
                AssessmentSelection(rubric=RubricRef(case_id="other", id="battle")),
                judgment=None,
            )
        )


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


@pytest.mark.parametrize("choice", ["c1", "c2", "skip"])
@pytest.mark.parametrize(
    "confirmed, consistent, separate_publication, extra_write",
    [
        (True, True, False, None),
        (False, True, False, None),
        (True, False, False, None),
        (True, True, True, None),
        (False, True, True, None),
        (True, False, True, None),
        (True, True, True, "completed"),
        (True, True, True, "unknown"),
        (True, True, True, "missing"),
        (True, True, True, "rejected"),
    ],
)
def test_synthetic_reward_requires_confirmed_choice_and_native_deck_change(
    tmp_path: Path,
    choice: str,
    confirmed: bool,
    consistent: bool,
    separate_publication: bool,
    extra_write: str | None,
) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    store = AnnotationStore(tmp_path / "annotations", cases)
    rubric = store.append_rubric(
        RubricRecord(
            case_id=case.id,
            id="reward",
            origin=Origin.HUMAN,
            author=HumanAuthor(id="maintainer"),
            criterion=CardRewardRubric(
                acceptable_card_ids=("CARD.ONE", "CARD.TWO"), allow_skip=True
            ),
        )
    )
    selection = AssessmentSelection(rubric=rubric)
    combat = battle_state(1, "battle")
    card = combat.objects["c1"]
    assert isinstance(card, native.CardEntry)
    before = run_state(1, "reward")
    assert isinstance(before.state, native.RunState)
    before = before.model_copy(
        update={
            "objects": {
                "owned": card,
                "c1": card,
                "c2": card.model_copy(update={"definition_id": "CARD.TWO"}),
            },
            "state": before.state.model_copy(
                update={
                    "screen": native.RunScreen.REWARDS,
                    "deck": ("owned",),
                    "selection": native.Selection(
                        prompt="Choose a card",
                        source=None,
                        min=0,
                        max=1,
                        candidates=("c1", "c2"),
                        selected=(),
                    ),
                }
            ),
        }
    )
    refs = () if choice == "skip" else (choice,)
    selected_card = before.objects[choice] if refs else None
    if not consistent:
        selected_card = card.model_copy(update={"definition_id": "CARD.UNEXPECTED"})
    after = before.model_copy(
        update={
            "objects": {"owned": card, "deck:1": selected_card}
            if selected_card
            else {"owned": card},
            "state": before.state.model_copy(
                update={
                    "selection": None,
                    "deck": ("owned", "deck:1") if selected_card else ("owned",),
                }
            ),
        }
    )
    branch = tmp_path / "reward.sqlite"
    with closing(new_branch(branch, case)) as journal:
        journal.begin(Observe())
        journal.finish(GameReply(outcome=Observed(observation=before), observations=(before,)))
        journal.end()
        if confirmed:
            write_step(
                journal,
                before,
                native.Select(kind=native.StepKind.SELECT, refs=refs),
                "choice",
                None if separate_publication else after,
            )
        if extra_write == "completed":
            write_step(
                journal,
                before,
                native.Interact(kind=native.StepKind.INTERACT, action="claim-gold"),
                "gold",
                None,
            )
        elif extra_write is not None:
            route = native.RouteKey.model_validate(before.location.model_dump())
            step = native.Interact(kind=native.StepKind.INTERACT, action="claim-gold")
            journal.begin(Settle(route=route, step=step))
            if extra_write != "missing":
                receipt = (
                    native.ReceiptUnknown(
                        operation_id="gold",
                        status=native.ResultStatus.UNKNOWN,
                        error=native.UnknownExecution(
                            code=native.ErrorCode.EXECUTION_UNCERTAIN,
                            message="Execution unconfirmed.",
                        ),
                    )
                    if extra_write == "unknown"
                    else native.ReceiptRejected(
                        operation_id="gold",
                        status=native.ResultStatus.REJECTED,
                        error=native.ActionUnavailable(
                            code=native.ErrorCode.ACTION_UNAVAILABLE,
                            message="Gold is unavailable.",
                        ),
                    )
                )
                journal.finish(
                    GameReply(
                        outcome=Settlement(
                            request=native.StepRequest(
                                operation_id="gold", expected_route=route, step=step
                            ),
                            receipt=receipt,
                        )
                    )
                )
            journal.end()
        if not confirmed or separate_publication:
            journal.begin(Observe())
            journal.finish(GameReply(outcome=Observed(observation=after), observations=(after,)))
            journal.end()
    judgment = HumanJudgment(
        case_id=case.id,
        sample=archive_identity(branch),
        selection=selection,
        acceptable=True,
        rationale="One accepted reward.",
        author=HumanAuthor(id="reviewer"),
    )
    report = asyncio.run(
        assess_sample(case, completed(case, branch), store, selection, judgment=judgment)
    )
    if not confirmed or not consistent or extra_write in ("completed", "unknown", "missing"):
        assert isinstance(report.quality, Unassessed)
        assert report.quality.reason is UnassessedReason.INSUFFICIENT_EVIDENCE
        assert report.evidence.metrics.reward is None
        assert (
            Uncertainty.INCOMPLETE_RECORDS
            if extra_write == "missing"
            else Uncertainty.REWARD_EVIDENCE
        ) in report.uncertainty
        if extra_write == "completed":
            assert tuple(action.operation_id for action in report.evidence.actions) == (
                "choice",
                "gold",
            )
        elif extra_write is not None:
            assert tuple(action.operation_id for action in report.evidence.actions) == ("choice",)
        return
    assert isinstance(report.quality, Assessed)
    reward = report.evidence.metrics.reward
    assert reward is not None
    assert reward.candidates == ("CARD.ONE", "CARD.TWO")
    assert reward.selected == (
        () if choice == "skip" else ("CARD.ONE",) if choice == "c1" else ("CARD.TWO",)
    )
    assert reward.skipped == (choice == "skip")
    assert reward.before_deck == ("CARD.ONE",)
    assert reward.after_deck == ("CARD.ONE",) + reward.selected
    assert reward.operation_id == "choice"
    assert reward.sequence == 2
    assert reward.before == ObservationRef(recording_id="reward", sequence=1, index=0)
    assert reward.after == ObservationRef(
        recording_id="reward",
        sequence=3 + int(extra_write is not None) if separate_publication else 2,
        index=0,
    )
    reference = store.append_assessment(report, id="reward")
    assert store.assessment(reference) == report
    unjudged = asyncio.run(
        assess_sample(case, completed(case, branch), store, selection, judgment=None)
    )
    assert unjudged.evidence.metrics.reward == reward
    assert isinstance(unjudged.quality, Unassessed)
    assert unjudged.quality.reason is UnassessedReason.NO_JUDGMENT
    assert Uncertainty.REWARD_EVIDENCE not in unjudged.uncertainty


def test_inherited_variant_uses_current_direct_parent_interval(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    original = cases.load("battle")
    first = tmp_path / "first.sqlite"
    initial = new_branch(first, original)
    manifest = initial.manifest
    initial.close()
    first.unlink()
    assert manifest.parent is not None
    manifest = manifest.model_copy(
        update={
            "variant": HistoricalVariant(
                case_id=original.id,
                case_sha256=sha256(original.model_dump_json().encode()).hexdigest(),
                source=manifest.parent,
                replacements=(),
            )
        }
    )
    opening = battle_state(1, "battle")
    with closing(Journal.create(first, manifest)) as journal:
        journal.begin(Observe())
        journal.finish(GameReply(outcome=Observed(observation=opening), observations=(opening,)))
        journal.end()
        write_step(
            journal,
            opening,
            native.UsePotion(kind=native.StepKind.USE_POTION, potion="p1"),
            "ancestor-use",
            opening,
        )
        sequence = journal.begin_model(
            native.DecisionOwner.COMBAT,
            "child",
            (),
            ToolSet(tools=()),
            ConfigurationSnapshot.of(configuration(tmp_path)),
        )
        journal.finish_model(sequence)
        journal.end_model()
        target = journal.request(sequence)
    child = original.model_copy(
        update={
            "id": "child",
            "source": SourceRef(
                archive=first,
                anchor=ModelAnchor("first", sequence),
                semantic_digest=target.semantic_digest,
                target_cursor=target.confirmed_operation_cursor,
            ),
        }
    )
    cases.publish(child)
    branch = tmp_path / "second.sqlite"
    with closing(new_branch(branch, child)) as journal:
        write_step(
            journal, opening, native.EndTurn(kind=native.StepKind.END_TURN), "child-turn", opening
        )
    store = AnnotationStore(tmp_path / "annotations", cases)
    report = asyncio.run(
        assess_sample(child, completed(child, branch), store, AssessmentSelection(), judgment=None)
    )
    assert report.evidence.metrics.potion_uses == ()
    assert tuple(action.operation_id for action in report.evidence.actions) == ("child-turn",)
    assert isinstance(report.provenance.variant, HistoricalVariant)
    assert report.provenance.parent is not None
    assert report.provenance.parent.before_sequence == 3
    assert report.provenance.variant.source.before_sequence == 1
    reference = store.append_assessment(report, id="child-report")
    assert store.assessment(reference) == report

    assert report.provenance.endpoint is not None
    wrong = report.model_copy(
        update={
            "provenance": report.provenance.model_copy(
                update={
                    "endpoint": report.provenance.endpoint.model_copy(update={"sequence": 1}),
                }
            )
        }
    )
    with pytest.raises(ValueError, match="endpoint"):
        store.append_assessment(wrong, id="wrong-range")


def test_uncreated_execution_does_not_read_existing_branch(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    branch = tmp_path / "old.sqlite"
    with closing(new_branch(branch, case)):
        pass
    execution = TakeoverUnsupported(
        branch,
        ProtocolMismatch(Protocol.RESPONSES, Protocol.CHAT_COMPLETIONS),
        recording_id=None,
    )
    store = AnnotationStore(tmp_path / "annotations", cases)
    report = asyncio.run(
        assess_sample(case, execution, store, AssessmentSelection(), judgment=None)
    )
    assert report.provenance.sample is None
    assert report.evidence.metrics.net_hp_change is None
    reference = store.append_assessment(report, id="uncreated")
    assert AnnotationStore(store.directory, cases).assessment(reference) == report


def test_execution_rejects_same_case_sibling_archive(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    case = cases.load("battle")
    branch = tmp_path / "first.sqlite"
    sibling = tmp_path / "sibling.sqlite"
    with closing(new_branch(branch, case)):
        pass
    execution = completed(case, branch)
    with closing(new_branch(sibling, case)):
        pass
    branch.write_bytes(sibling.read_bytes())
    store = AnnotationStore(tmp_path / "annotations", cases)
    with pytest.raises(ValueError, match="recording"):
        asyncio.run(assess_sample(case, execution, store, AssessmentSelection(), judgment=None))
