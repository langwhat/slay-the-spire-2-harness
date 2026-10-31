from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_lifecycle import battle_state
from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.client import models as native
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import ConfigurationSnapshot, ToolSet
from sts2_harness_eval.annotations import (
    AgentAuthor,
    AnnotationRecord,
    AnnotationRef,
    AnnotationStore,
    BattleRubric,
    CardRewardRubric,
    EvidenceRef,
    HumanAuthor,
    Origin,
    ReviewDecision,
    ReviewRecord,
    RubricRecord,
)
from sts2_harness_eval.cases import (
    Artifact,
    Case,
    CaseStore,
    CheckpointRef,
    ShortcutBinding,
    SourceRef,
    TargetBattle,
)
from sts2_harness_eval.checkpoints import IdentityFacts, IdentityRead, ReloadTarget
from sts2_harness_eval.results import ModelAnchor
from sts2_harness_eval.verification import KeyedRefs, Publication, Reference


def cases_fixture(directory: Path) -> CaseStore:
    directory.mkdir()
    archive = directory / "source.sqlite"
    with closing(
        Journal.create(
            archive,
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="source",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
    ) as journal:
        sequence = journal.begin_model(
            native.DecisionOwner.COMBAT,
            "combat",
            (),
            ToolSet(tools=()),
            ConfigurationSnapshot.of(configuration(directory), CapacityPolicy()),
        )
        journal.finish_model(sequence)
        journal.end_model()
        request = journal.request(sequence)
    observation = battle_state(1, "battle")
    publication = Publication(
        source_sequence=sequence,
        source_stamp=observation.stamp,
        read=IdentityRead(
            status=native.ResultStatus.COMPLETED,
            observation=observation,
            identities=IdentityFacts(cards=(), creatures=(), map_travel=(), relics=()),
        ),
    )
    store = CaseStore(directory)
    reference = store.content(
        Artifact.REFERENCES,
        Reference(
            boundary=publication,
            suffix=(),
            target=publication,
            follow_on_before=(),
            follow_on=(),
        ),
    )
    binding = store.content(
        Artifact.BINDINGS,
        ShortcutBinding(
            suffix=(),
            target=KeyedRefs(source_sequence=sequence, refs=()),
            follow_on=(),
            reference=reference,
        ),
    )
    evidence = store.content(Artifact.EVIDENCE, TargetBattle())
    checkpoint_file = directory / "synthetic-checkpoint"
    checkpoint_file.write_bytes(b"synthetic checkpoint")
    checkpoint = CheckpointRef(
        sha256=sha256(checkpoint_file.read_bytes()).hexdigest(),
        bytes=20,
        boundary_sequence=sequence,
        boundary_cursor=0,
        target=ReloadTarget(
            canonical_seed="SEED",
            game_mode="standard",
            character="IRONCLAD",
            ascension=0,
            act=1,
            floor=1,
        ),
    )
    store.retain_checkpoint(checkpoint, checkpoint_file)
    for case_id in ("battle", "other"):
        store.publish(
            Case(
                id=case_id,
                source=SourceRef(
                    archive=archive,
                    anchor=ModelAnchor("source", sequence),
                    semantic_digest=request.semantic_digest,
                    target_cursor=0,
                ),
                checkpoint=checkpoint,
                binding=binding,
                evidence=evidence,
                endpoint=TargetBattle(),
            )
        )
    return store


def test_agent_annotation_keeps_origin_after_conflicting_human_reviews(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    original = (cases.directory / "cases" / "battle.json").read_bytes()
    directory = tmp_path / "annotations"
    store = AnnotationStore(directory, cases)
    agent = AgentAuthor(id="candidate", model="test-model")
    annotation = AnnotationRecord(
        case_id="battle",
        id="route",
        origin=Origin.AGENT,
        author=agent,
        text="Both defensive lines are acceptable.",
        evidence_refs=(),
    )
    annotation_ref = store.append_annotation(annotation)
    unreviewed = store.load("battle")
    assert unreviewed.annotations == (annotation,)
    assert unreviewed.reviews == ()
    human = HumanAuthor(id="maintainer")
    rejected = ReviewRecord(
        case_id="battle",
        id="first",
        annotation=annotation_ref,
        decision=ReviewDecision.REJECTED,
        reviewer=human,
        rationale="Needs evidence.",
    )
    first_ref = store.append_review(rejected)
    accepted = ReviewRecord(
        case_id="battle",
        id="second",
        annotation=annotation_ref,
        decision=ReviewDecision.ACCEPTED,
        reviewer=human,
        rationale="Verified both lines.",
    )
    second_ref = store.append_review(accepted)
    rubric = RubricRecord(
        case_id="battle",
        id="survival",
        origin=Origin.HUMAN,
        author=human,
        criterion=BattleRubric(description="Survive while conserving resources."),
    )
    rubric_ref = store.append_rubric(rubric)
    reopened = AnnotationStore(directory, CaseStore(cases.directory))
    records = reopened.load("battle")
    assert records.annotations == (annotation,)
    assert records.annotations[0].origin is Origin.AGENT
    assert records.annotations[0].author == agent
    assert records.reviews == (rejected, accepted)
    assert records.rubrics == (rubric,)
    assert (first_ref.id, second_ref.id, rubric_ref.id) == ("first", "second", "survival")
    assert reopened.load("other").annotations == ()
    assert (cases.directory / "cases" / "battle.json").read_bytes() == original


def test_records_reject_overwrite_and_reviews_cannot_cross_cases(tmp_path: Path) -> None:
    cases = cases_fixture(tmp_path / "cases")
    store = AnnotationStore(tmp_path / "annotations", cases)
    human = HumanAuthor(id="maintainer")
    annotation = AnnotationRecord(
        case_id="battle",
        id="choice",
        origin=Origin.HUMAN,
        author=human,
        text="Either card is acceptable.",
        evidence_refs=(EvidenceRef(recording_id="source", sequence=1),),
    )
    reference = store.append_annotation(annotation)
    review = ReviewRecord(
        case_id="battle",
        id="review",
        annotation=reference,
        decision=ReviewDecision.ACCEPTED,
        reviewer=human,
        rationale="Checked the actual deck.",
    )
    store.append_review(review)
    rubric = RubricRecord(
        case_id="battle",
        id="reward",
        origin=Origin.HUMAN,
        author=human,
        criterion=CardRewardRubric(
            acceptable_card_ids=("CARD.SHRUG_IT_OFF", "CARD.TRUE_GRIT"),
            allow_skip=True,
        ),
    )
    store.append_rubric(rubric)
    before = store.load("battle")
    with pytest.raises(FileExistsError):
        store.append_annotation(annotation.model_copy(update={"text": "Changed."}))
    with pytest.raises(FileExistsError):
        store.append_review(review)
    with pytest.raises(FileExistsError):
        store.append_rubric(rubric)
    with pytest.raises(ValueError, match="existing annotation in its case"):
        store.append_review(review.model_copy(update={"case_id": "other", "id": "wrong-case"}))
    with pytest.raises(ValueError, match="existing annotation in its case"):
        store.append_review(
            review.model_copy(
                update={
                    "id": "missing",
                    "annotation": AnnotationRef(case_id="battle", id="missing"),
                }
            )
        )
    with pytest.raises(FileNotFoundError):
        store.append_annotation(annotation.model_copy(update={"case_id": "missing"}))
    with pytest.raises(ValidationError):
        store.load("../battle")
    assert AnnotationStore(store.directory, cases).load("battle") == before
    assert store.load("other").reviews == ()
