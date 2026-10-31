import asyncio
import sqlite3
import zlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.journal import ArchiveError, Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import (
    ConfigurationSnapshot,
    PreparationEvidence,
    ToolSet,
    archive_input,
)
from sts2_harness.llm.messages import (
    FailureKind,
    ModelUnavailable,
    RequestPreparation,
    TextMessage,
    TextRole,
)
from sts2_harness_eval._replay import ReplayModel, ReplayRejected, Timeline
from sts2_harness_eval.results import ModelAnchor, Reached


def test_preparation_persists_full_before_after_and_rejects_missing_content(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    journal = Journal.create(
        path,
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="recording",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    before = archive_input((TextMessage(TextRole.USER, "old input"),))
    after = archive_input((TextMessage(TextRole.USER, "current input"),))
    baseline = archive_input((TextMessage(TextRole.USER, "prior baseline"),))[0]
    evidence = PreparationEvidence(
        group_id=2,
        baseline=baseline,
        input_before=before,
        estimate_before=290_000,
        decision="compacted",
        input_after=after,
        estimate_after=15_000,
        retained_complete_group_id=1,
        active_group_id=2,
        validation="passed",
    )
    sequence = journal.begin_model(
        native.DecisionOwner.RUN,
        "run:1",
        after,
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path)),
        preparation=evidence,
    )
    journal.finish_model(sequence, failure=FailureKind.PROVIDER)
    journal.end_model()
    recorded = journal.request(sequence).preparation
    assert recorded is not None
    assert (recorded.input_before, recorded.input_after, recorded.baseline) == (
        before,
        after,
        baseline,
    )
    assert (recorded.group_id, recorded.retained_complete_group_id, recorded.active_group_id) == (
        2,
        1,
        2,
    )
    assert recorded.input_before_digest == recorded.input_before_ref
    assert recorded.input_after_digest == recorded.input_after_ref
    assert recorded.baseline_digest == recorded.baseline_ref
    assert recorded.tools == ()
    assert journal.request(sequence).attempts == ()
    journal.close()

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE blobs SET content=? WHERE digest=?",
            (zlib.compress(b"changed"), recorded.input_before_ref),
        )
    archive = Journal.open(path)
    try:
        with pytest.raises(ArchiveError, match="integrity"):
            archive.request(sequence)
    finally:
        archive.close()


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
        validation="failed",
    )
    sequence = journal.begin_model(
        native.DecisionOwner.RUN,
        "run:1",
        before,
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path)),
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


@pytest.mark.parametrize("protocol", tuple(Protocol))
def test_replay_uses_recorded_window_and_checks_unsent_preparation(
    tmp_path: Path, protocol: Protocol
) -> None:
    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "replay.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="replay",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                capacity_policy=CapacityPolicy(context_window=5_000),
            ),
        )
        input = (TextMessage(TextRole.USER, "unsent"),)
        evidence = PreparationEvidence(
            group_id=4,
            baseline=None,
            input_before=archive_input(input),
            estimate_before=6_000,
            decision="minimum_exceeds_window",
            input_after=None,
            estimate_after=None,
            retained_complete_group_id=3,
            active_group_id=4,
            validation="failed",
        )
        sequence = journal.begin_model(
            native.DecisionOwner.RUN,
            "run:1",
            archive_input(input),
            ToolSet(tools=()),
            ConfigurationSnapshot.of(
                replace(configuration(tmp_path), protocol=protocol, context_window=5_000)
            ),
            preparation=evidence,
        )
        journal.finish_model(sequence, failure=FailureKind.CAPACITY)
        journal.end_model()
        record = journal.request(sequence)
        reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
        timeline = Timeline((record,))
        replay = ReplayModel(
            timeline,
            native.DecisionOwner.RUN,
            "run:1",
            ModelAnchor("replay", 2),
            reached,
        )
        preparation = RequestPreparation(
            group_id=4,
            baseline=None,
            input_before=input,
            estimate_before=6_000,
            decision="minimum_exceeds_window",
            input_after=None,
            estimate_after=None,
            retained_complete_group_id=3,
            active_group_id=4,
            validation="failed",
        )
        try:
            assert replay.capacity_policy.context_window == 5_000
            assert replay.prepare(input, ()).input == input
            await replay.interrupt_preparation(
                preparation, (), ModelUnavailable(FailureKind.CAPACITY)
            )
            assert timeline.index == 1
        finally:
            await replay.close()
            journal.close()

        rejected = ReplayModel(
            Timeline((record,)),
            native.DecisionOwner.RUN,
            "run:1",
            ModelAnchor("replay", 2),
            reached,
        )
        with pytest.raises(ReplayRejected):
            await rejected.interrupt_preparation(
                replace(preparation, group_id=5), (), ModelUnavailable(FailureKind.CAPACITY)
            )
        await rejected.close()

    asyncio.run(scenario())
