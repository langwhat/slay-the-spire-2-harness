from pathlib import Path

from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy, CompactionValidation
from sts2_harness.client import models as native
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import (
    ConfigurationSnapshot,
    PreparationEvidence,
    ToolSet,
    archive_input,
)
from sts2_harness.llm.messages import (
    FailureKind,
    TextMessage,
    TextRole,
)


def test_failed_preparation_records_candidate_but_no_attempt(tmp_path: Path) -> None:
    journal = Journal.create(
        tmp_path / "failed.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="failed",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    before = archive_input((TextMessage(TextRole.USER, "cannot fit"),))
    candidate = archive_input((TextMessage(TextRole.USER, "still cannot fit"),))
    evidence = PreparationEvidence(
        group_id=1,
        baseline=None,
        input_before=before,
        estimate_before=400_000,
        decision="minimum_exceeds_window",
        input_after=candidate,
        estimate_after=320_000,
        retained_complete_group_id=None,
        active_group_id=1,
        validation=CompactionValidation.FAILED,
    )
    sequence = journal.begin_model(
        native.DecisionOwner.RUN,
        "run:1",
        before,
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path), CapacityPolicy()),
        preparation=evidence,
    )
    journal.finish_model(sequence, failure=FailureKind.PROVIDER)
    journal.end_model()
    record = journal.request(sequence)
    assert record.attempts == ()
    assert record.preparation is not None
    assert record.preparation.input_before == before
    assert record.preparation.input_after == candidate
    assert record.preparation.input_after_ref is not None
    assert record.preparation.estimate_after == 320_000
    journal.close()
