import asyncio
import sqlite3
import zlib
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import httpx2 as httpx
from test_combat import reply
from test_lifecycle import Campaign
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame, OperationKind, Settle, Settlement
from sts2_harness.journal import (
    Journal,
    Manifest,
    Record,
    RecordedGame,
    RecordedResult,
    SessionMaterials,
    StoredReply,
)
from sts2_harness.knowledge import (
    Documents,
)
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval import ModelAnchor, Reached, Rejected, RejectReason, reach
from sts2_harness_eval.results import (
    InputComponent,
    InputDifference,
    Unsupported,
    UnsupportedCause,
)


async def record_campaign(
    path: Path,
    campaign: Campaign,
    *,
    documents: Documents | None = None,
    start: bool = False,
) -> tuple[ModelAnchor, ...]:
    manifest = Manifest(
        materials=SessionMaterials(
            entry_documents_enabled=False, act_guidance={}, battle_guidance=()
        ),
        recording_id="recording",
        core_version="historical-software",
        game_instance_id="game",
        run_id="run",
        initial_notes="Opening plan.",
    )
    journal = Journal.create(path, manifest)
    async with (
        ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)) as client,
        Llm(configuration(path.parent), transport=httpx.MockTransport(campaign.model)) as model,
    ):

        def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
            return RecordedLlm(model, journal, role=role, stream=stream)

        dependencies = SessionDependencies(
            RecordedGame(
                LiveGame(
                    client,
                    namespace=manifest.recording_id,
                    audit=journal,
                    game_instance_id=manifest.game_instance_id,
                    run_id=manifest.run_id,
                ),
                journal,
            ),
            RecordedPrompts(PackagedPrompts(), journal),
            RecordedDocuments(documents if documents is not None else FileDocuments(), journal),
            factory,
        )
        async with RunSession(manifest, dependencies) as session:
            if start:
                await session.start()
            await session.run()
    journal.close()
    archive = Journal.open(path)
    try:
        return tuple(ModelAnchor("recording", record.sequence) for record in archive.requests())
    finally:
        archive.close()


def test_reach_rebuilds_notes_and_inspection_at_distinct_same_cursor_anchors(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        campaign.responses = iter(
            [
                reply(("edit", "run_notes", '{"mode":"replace","text":"Updated plan."}')),
                reply(("map", "run_inspect", '{"area":"map"}')),
                *campaign.responses,
            ]
        )
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, campaign)
        journal = Journal.open(path)
        records = journal.requests()
        journal.close()
        assert [record.confirmed_operation_cursor for record in records[:3]] == [0, 0, 0]
        source = path.read_bytes()
        for anchor, record in zip(anchors[:3], records[:3], strict=True):
            tasks_before = asyncio.all_tasks()
            async with reach(path, anchor) as result:
                assert result == Reached(anchor, 0, record.semantic_digest)
                owned = asyncio.all_tasks() - tasks_before
                assert len(owned) == 1
                assert all(not task.done() for task in owned)
            assert all(task.done() for task in owned)
            assert asyncio.all_tasks() == tasks_before
            assert path.read_bytes() == source

    asyncio.run(scenario())


class StartedCampaign(Campaign):
    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            return httpx.Response(200, json={"mode": "paused"})
        return super().mod(request)


def test_reach_runs_start_combat_handoff_and_act_rebuild(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, StartedCampaign(), start=True)
        archive = Journal.open(path)
        records = archive.requests()
        archive.close()
        assert [(record.role, record.stream) for record in records] == [
            (native.DecisionOwner.RUN, "run:1"),
            (native.DecisionOwner.RUN, "run:1"),
            (native.DecisionOwner.COMBAT, "combat:1"),
            (native.DecisionOwner.COMBAT, "combat:1"),
            (native.DecisionOwner.RUN, "run:1"),
            (native.DecisionOwner.RUN, "run:1"),
            (native.DecisionOwner.RUN, "run:2"),
            (native.DecisionOwner.RUN, "run:2"),
            (native.DecisionOwner.COMBAT, "combat:2"),
        ]
        for anchor, record in zip(anchors[3:], records[3:], strict=True):
            async with reach(path, anchor) as result:
                assert result == Reached(
                    anchor, record.confirmed_operation_cursor, record.semantic_digest
                )

    asyncio.run(scenario())


def test_reach_uses_branch_suffix_with_recorded_mixed_operation_ids(tmp_path: Path) -> None:
    from sts2_harness.game import GameReply
    from sts2_harness.journal import ParentPrefix
    from sts2_harness.journal_models import MaterialRecord, ModelRecord, ToolSet

    async def scenario() -> None:
        source_path = tmp_path / "parent.sqlite"
        anchors = await record_campaign(source_path, StartedCampaign(), start=True)
        with closing(Journal.open(source_path)) as source:
            cutoff = anchors[2].model_sequence
            target = source.request(anchors[-1].model_sequence)
            branch = Journal.create(
                tmp_path / "branch.sqlite",
                source.manifest.model_copy(
                    update={
                        "recording_id": "branch",
                        "core_version": "current-software",
                        "parent": ParentPrefix(
                            archive=source_path,
                            recording_id=source.manifest.recording_id,
                            before_sequence=cutoff,
                            archive_sha256=sha256(source_path.read_bytes()).hexdigest(),
                        ),
                    }
                ),
            )
            for record in source.timeline(start=cutoff, stop=target.sequence):
                if isinstance(record, ModelRecord):
                    sequence = branch.begin_model(
                        record.role,
                        record.stream,
                        record.input,
                        ToolSet(tools=record.tools),
                        record.configuration,
                        preparation=record.preparation,
                    )
                    for attempt in record.attempts:
                        branch.model_attempt(sequence, attempt)
                    branch.finish_model(sequence, failure=record.failure)
                    branch.end_model()
                    continue
                if isinstance(record, MaterialRecord):
                    sequence = branch.begin_material(record.request)
                    assert record.reply is not None
                    branch.finish_material(sequence, record.reply)
                    continue
                assert record.reply is not None
                outcome = record.reply.outcome
                if isinstance(outcome, Settlement):
                    assert outcome.receipt is not None
                    operation_id = f"native-B:{record.sequence}"
                    outcome = outcome.model_copy(
                        update={
                            "request": outcome.request.model_copy(
                                update={"operation_id": operation_id}
                            ),
                            "receipt": outcome.receipt.model_copy(
                                update={"operation_id": operation_id}
                            ),
                        }
                    )
                branch.begin(record.request)
                branch.finish(GameReply(outcome=outcome, observations=record.reply.observations))
                branch.end()
            branch.close()
        with closing(Journal.open(branch.path)) as reader:
            writes = reader.operations(kind=OperationKind.SETTLE)
            assert writes[0].operation_id == "recording:1"
            assert all(
                record.operation_id == f"native-B:{record.sequence}" for record in writes[1:]
            )
            assert reader.timeline()[-1].sequence == target.sequence
            assert reader.request(target.sequence).semantic_digest == target.semantic_digest
            assert reader.requests()[2].base_sequence is None
            assert reader.request(anchors[4].model_sequence).position == 3
        anchor = ModelAnchor("branch", target.sequence)
        async with reach(branch.path, anchor) as result:
            assert result == Reached(
                anchor, target.confirmed_operation_cursor, target.semantic_digest
            )
        changed = writes[1]
        assert changed.reply is not None and isinstance(changed.reply.outcome, Settlement)
        receipt = changed.reply.outcome.receipt
        assert receipt is not None
        replace_result(
            branch.path,
            changed.sequence,
            changed.reply.model_copy(
                update={
                    "outcome": changed.reply.outcome.model_copy(
                        update={"receipt": receipt.model_copy(update={"operation_id": "wrong"})}
                    )
                }
            ),
        )
        async with reach(branch.path, anchor) as result:
            assert result == Rejected(
                RejectReason.INPUT_MISMATCH,
                changed.sequence,
                InputDifference(InputComponent.OPERATION_ID),
            )

    asyncio.run(scenario())


def store(connection: sqlite3.Connection, value: native.WireModel) -> str:
    content = value.model_dump_json().encode()
    digest = sha256(content).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO blobs VALUES (?,?,?)",
        (digest, zlib.compress(content), "value"),
    )
    return digest


def replace_result(path: Path, sequence: int, reply: RecordedResult) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        stored = StoredReply(
            outcome=store(connection, reply.outcome),
            observations=tuple(
                store(connection, observation) for observation in reply.observations
            ),
        )
        connection.execute(
            "UPDATE records SET reply=? WHERE sequence=?", (stored.model_dump_json(), sequence)
        )


def make_unknown(path: Path, record: Record) -> None:
    assert record.reply is not None and isinstance(record.reply.outcome, Settlement)
    outcome = record.reply.outcome
    replace_result(
        path,
        record.sequence,
        record.reply.model_copy(
            update={
                "outcome": outcome.model_copy(
                    update={
                        "receipt": native.ReceiptUnknown(
                            operation_id=outcome.request.operation_id,
                            status=native.ResultStatus.UNKNOWN,
                            error=native.UnknownExecution(
                                code=native.ErrorCode.EXECUTION_UNCERTAIN,
                                message="Synthetic unknown write.",
                            ),
                        )
                    }
                )
            }
        ),
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE summaries SET is_error=1,value=json_set(value,'$.result','unknown') "
            "WHERE sequence=?",
            (record.sequence,),
        )


def test_reach_rejects_changed_observations_with_original_model_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, Campaign())
        journal = Journal.open(path)
        records = journal.operations(stop=anchors[0].model_sequence)
        journal.close()
        for record in records:
            assert record.reply is not None
            observations = tuple(
                observation.model_copy(
                    update={
                        "state": observation.state.model_copy(
                            update={
                                "player": observation.state.player.model_copy(
                                    update={"gold": observation.state.player.gold + 1}
                                )
                            }
                        )
                    }
                )
                for observation in record.reply.observations
            )
            replace_result(
                path,
                record.sequence,
                record.reply.model_copy(update={"observations": observations}),
            )
        async with reach(path, anchors[0]) as result:
            assert result == Rejected(
                RejectReason.INPUT_MISMATCH,
                anchors[0].model_sequence,
                InputDifference(InputComponent.MODEL_INPUT),
            )

    asyncio.run(scenario())


def test_reach_rejects_missing_prefix_and_unknown_writes(tmp_path: Path) -> None:
    async def scenario() -> None:
        original = tmp_path / "archive.sqlite"
        anchors = await record_campaign(original, Campaign())
        missing = tmp_path / "missing.sqlite"
        missing.write_bytes(original.read_bytes())
        with closing(sqlite3.connect(missing)) as connection, connection:
            connection.execute("DELETE FROM materials WHERE sequence=2")
            connection.execute("DELETE FROM summaries WHERE sequence=2")
        async with reach(missing, anchors[0]) as result:
            assert result == Rejected(
                RejectReason.UNSUPPORTED_HISTORY, 2, Unsupported(UnsupportedCause.PREFIX_GAP)
            )
        journal = Journal.open(original)
        first_settle = next(
            record for record in journal.operations() if isinstance(record.request, Settle)
        )
        journal.close()
        make_unknown(original, first_settle)
        async with reach(original, anchors[2]) as result:
            assert result == Rejected(
                RejectReason.UNSUPPORTED_HISTORY,
                first_settle.sequence,
                Unsupported(UnsupportedCause.UNKNOWN_WRITE),
            )

    asyncio.run(scenario())
