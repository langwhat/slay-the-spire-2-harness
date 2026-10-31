import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import zlib
from contextlib import closing
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Never

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame, OperationKind, Settle, Settlement, Stopped
from sts2_harness.journal import (
    DependencyKind,
    Journal,
    Manifest,
    ModelSummary,
    Record,
    RecordedGame,
    RecordedResult,
    SessionMaterials,
    StoredReply,
)
from sts2_harness.journal_models import DocumentRead, MaterialFailed, ResultSnapshot
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    SkillRead,
    SubjectId,
    UnknownDocument,
)
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.results import Rejected as RejectedQuery
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval import ModelAnchor, Reached, Rejected, RejectReason, reach
from sts2_harness_eval.results import (
    ArchiveUnreadable,
    InputComponent,
    InputDifference,
    MissingTarget,
    OrderDifference,
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


class DocumentCampaign(Campaign):
    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/catalog":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "data": {
                        "entries": [
                            {
                                "category": "relic",
                                "definition_id": "RELIC.ONE",
                                "name": "Relic",
                                "native": {
                                    "definition_id": "RELIC.ONE",
                                    "name": "Relic",
                                    "text": "Synthetic native facts.",
                                    "rarity": None,
                                },
                            }
                        ],
                        "missing_ids": [],
                    },
                },
            )
        return super().mod(request)


def test_reach_replays_explicit_document_reads_without_entry_injection(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = DocumentCampaign()
        campaign.responses = iter(
            [
                reply(("list", "run_skill", '{"mode":"list"}')),
                reply(("read", "run_skill", '{"mode":"read","id":"guide"}')),
                reply(("lookup", "run_knowledge", '{"mode":"lookup","ids":["RELIC.ONE"]}')),
                *campaign.responses,
            ]
        )
        guide = tmp_path / "guide.txt"
        guide.write_text("Synthetic explicit guidance.")
        documents = FileDocuments(
            (
                DocumentSource(
                    "guide",
                    "Guide",
                    "Example",
                    guide,
                    subjects=(SubjectId(category="relic", definition_id="RELIC.ONE"),),
                ),
            )
        )
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, campaign, documents=documents)
        guide.unlink()
        async with reach(path, anchors[3]) as result:
            assert isinstance(result, Reached)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("document_id", "message"),
    [
        ("guide", "Resource 'missing' is not advertised by document 'guide'."),
        ("unknown", "Document 'unknown' is not configured."),
    ],
)
def test_reach_preserves_missing_resource_feedback(
    tmp_path: Path, document_id: str, message: str
) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        campaign.responses = iter(
            [
                reply(
                    (
                        "read",
                        "run_skill",
                        json.dumps({"mode": "read", "id": document_id, "resource": "missing"}),
                    )
                ),
                *campaign.responses,
            ]
        )
        guide = tmp_path / "guide.txt"
        guide.write_text("Synthetic guidance.", encoding="utf-8")
        documents = FileDocuments((DocumentSource("guide", "Guide", "Example", guide),))
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, campaign, documents=documents)
        archive = Journal.open(path)
        failure = next(
            record for record in archive.materials() if isinstance(record.request, DocumentRead)
        )
        assert isinstance(failure.reply, MaterialFailed) and failure.reply.message == message
        feedback = next(
            item
            for item in archive.request(anchors[1].model_sequence).input
            if isinstance(item, ResultSnapshot)
        )
        assert isinstance(feedback.outcome, RejectedQuery)
        assert feedback.outcome.reason.message == message
        archive.close()
        async with reach(path, anchors[1]) as result:
            assert isinstance(result, Reached)
        changed = failure.reply.model_copy(update={"message": "Different historical failure."})
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE materials SET reply=? WHERE sequence=?",
                (store(connection, changed), failure.sequence),
            )
        async with reach(path, anchors[1]) as result:
            assert result == Rejected(
                RejectReason.INPUT_MISMATCH,
                anchors[1].model_sequence,
                InputDifference(InputComponent.MODEL_INPUT),
            )

    asyncio.run(scenario())


class FailedDocuments(FileDocuments):
    def __init__(self, failure: MaterialUnavailable | UnknownDocument) -> None:
        super().__init__()
        self.failure = failure

    async def read(self, request: SkillRead) -> DocumentView:
        raise self.failure

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        raise self.failure


@pytest.mark.parametrize(
    ("tool", "arguments", "failure"),
    [
        (
            "run_skill",
            '{"mode":"read","id":"guide"}',
            MaterialUnavailable("The custom document service is temporarily unavailable."),
        ),
        (
            "run_skill",
            '{"mode":"read","id":"guide"}',
            UnknownDocument("The custom document source does not advertise this guide."),
        ),
        (
            "run_knowledge",
            '{"mode":"lookup","ids":["RELIC.ONE"]}',
            MaterialUnavailable("The custom supplement service is temporarily unavailable."),
        ),
    ],
)
def test_reach_preserves_custom_material_failure_feedback(
    tmp_path: Path, tool: str, arguments: str, failure: MaterialUnavailable | UnknownDocument
) -> None:
    async def scenario() -> None:
        campaign = DocumentCampaign()
        campaign.responses = iter([reply(("read", tool, arguments)), *campaign.responses])
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, campaign, documents=FailedDocuments(failure))
        async with reach(path, anchors[1]) as result:
            assert isinstance(result, Reached)

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


class SnapshotChange(StrEnum):
    MAPPING_ORDER = "mapping_order"
    MAPPING_VALUE = "mapping_value"
    MESSAGE_ORDER = "message_order"


@pytest.mark.parametrize("change", list(SnapshotChange))
def test_reach_compares_typed_input_without_mapping_key_order(
    tmp_path: Path, change: SnapshotChange
) -> None:
    from pydantic import MISSING

    from sts2_harness.context.models import ActContent
    from sts2_harness.journal import ParentPrefix
    from sts2_harness.journal_models import PreparationEvidence, TaskSnapshot, ToolSet

    async def scenario() -> None:
        source_path = tmp_path / "source.sqlite"
        anchors = await record_campaign(source_path, Campaign())
        original = source_path.read_bytes()
        with closing(Journal.open(source_path)) as source:
            target = source.request(anchors[2].model_sequence)
            position, snapshot = next(
                (index, message)
                for index, message in enumerate(target.input)
                if isinstance(message, TaskSnapshot)
            )
            assert isinstance(snapshot.content, ActContent)
            observation = snapshot.content.observation
            profiles = observation.catalog_additions
            assert profiles is not MISSING
            reordered = dict(reversed(tuple(profiles.items())))
            assert tuple(reordered) != tuple(profiles)
            if change is SnapshotChange.MAPPING_VALUE:
                key, profile = next(iter(reordered.items()))
                reordered[key] = profile.model_copy(update={"language": "changed"})
            reordered_observation = observation.model_copy(update={"catalog_additions": reordered})
            content = snapshot.content.model_copy(update={"observation": reordered_observation})
            messages = list(target.input)
            messages[position] = snapshot.model_copy(update={"content": content})
            if change is SnapshotChange.MESSAGE_ORDER:
                messages.reverse()
            assert (tuple(messages) == target.input) is (change is SnapshotChange.MAPPING_ORDER)
            with closing(
                Journal.create(
                    tmp_path / "branch.sqlite",
                    source.manifest.model_copy(
                        update={
                            "recording_id": "branch",
                            "parent": ParentPrefix(
                                archive=source_path,
                                recording_id=source.manifest.recording_id,
                                before_sequence=target.sequence,
                                archive_sha256=sha256(original).hexdigest(),
                            ),
                        }
                    ),
                )
            ) as branch:
                assert target.preparation is not None
                preparation = PreparationEvidence.model_validate(
                    target.preparation.model_dump(include=set(PreparationEvidence.model_fields))
                ).model_copy(update={"input_after": tuple(messages)})
                sequence = branch.begin_model(
                    target.role,
                    target.stream,
                    tuple(messages),
                    ToolSet(tools=target.tools),
                    target.configuration,
                    preparation=preparation,
                )
                branch.finish_model(sequence)
                branch.end_model()
        with closing(Journal.open(branch.path)) as reader:
            recorded = reader.request(sequence)
            assert (recorded.input == target.input) is (change is SnapshotChange.MAPPING_ORDER)
            assert recorded.semantic_digest != target.semantic_digest
        anchor = ModelAnchor("branch", sequence)
        branch_bytes = branch.path.read_bytes()
        try:
            async with reach(branch.path, anchor) as result:
                if change is SnapshotChange.MAPPING_ORDER:
                    assert result == Reached(
                        anchor, target.confirmed_operation_cursor, recorded.semantic_digest
                    )
                else:
                    assert result == Rejected(
                        RejectReason.INPUT_MISMATCH,
                        sequence,
                        InputDifference(InputComponent.MODEL_INPUT),
                    )
        finally:
            assert source_path.read_bytes() == original
            assert branch.path.read_bytes() == branch_bytes

    asyncio.run(scenario())


def test_reach_accepts_generated_directories_across_process_hash_seeds(tmp_path: Path) -> None:
    from pydantic import MISSING

    from sts2_harness.context.models import ActContent
    from sts2_harness.journal_models import TaskSnapshot

    path = tmp_path / "source.sqlite"
    campaign = Campaign()
    opening = campaign.states[1]
    campaign.states[1] = opening.model_copy(
        update={"generated": {**opening.generated, "c2": opening.generated["c1"]}}
    )
    anchors = asyncio.run(record_campaign(path, campaign))
    with closing(Journal.open(path)) as journal:
        target = journal.request(anchors[2].model_sequence)
        task = next(message for message in target.input if isinstance(message, TaskSnapshot))
        assert isinstance(task.content, ActContent)
        current = task.content.observation.current
        assert current is not MISSING
        generated = current.generated
        assert generated is not MISSING and len(generated) >= 2
    original = path.read_bytes()
    check = """
import asyncio
import sys
from pathlib import Path
from sts2_harness_eval import ModelAnchor, Reached, reach

async def main() -> None:
    anchor = ModelAnchor("recording", int(sys.argv[2]))
    async with reach(Path(sys.argv[1]), anchor) as result:
        assert result == Reached(anchor, int(sys.argv[3]), sys.argv[4]), result

asyncio.run(main())
"""
    for seed in range(4):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                check,
                str(path),
                str(target.sequence),
                str(target.confirmed_operation_cursor),
                target.semantic_digest,
            ],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, (seed, completed.stdout, completed.stderr)
    assert path.read_bytes() == original


def test_reach_rejects_corrupted_input_blob(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source.sqlite"
        anchors = await record_campaign(source, Campaign())
        original = source.read_bytes()
        damaged = tmp_path / "damaged.sqlite"
        damaged.write_bytes(original)
        with closing(sqlite3.connect(damaged)) as connection, connection:
            connection.execute(
                "UPDATE blobs SET content=? WHERE digest="
                "(SELECT input FROM model_requests WHERE sequence=?)",
                (zlib.compress(b"corrupted input delta"), anchors[0].model_sequence),
            )
        async with reach(damaged, anchors[0]) as result:
            assert isinstance(result, Rejected)
            assert result.reason is RejectReason.UNSUPPORTED_HISTORY
            assert isinstance(result.detail, ArchiveUnreadable)
        assert source.read_bytes() == original

    asyncio.run(scenario())


class FailedAutoCampaign(StartedCampaign):
    def __init__(self) -> None:
        from test_auto import scene

        super().__init__()
        self.states.insert(
            0,
            scene(
                native.RunScreen.TREASURE,
                "chest",
                (
                    native.Action(
                        ref="chest",
                        label="打开宝箱",
                        enabled=True,
                        operation=native.ActionOperation.OPEN_CHEST,
                    ),
                ),
            ),
        )
        self.model_failed = False

    def mod(self, request: httpx.Request) -> httpx.Response:
        response = super().mod(request)
        if request.url.path == "/action" and len(self.actions) == 1:
            return httpx.Response(
                200,
                json={
                    "operation_id": self.actions[0].operation_id,
                    "status": "failed",
                    "error": {"code": "dispatch_failed", "message": "Synthetic failed auto."},
                },
            )
        return response

    def model(self, request: httpx.Request) -> httpx.Response:
        if not self.model_failed:
            self.model_failed = True
            return httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
        return super().model(request)


def test_reach_replays_failed_auto_model_failure_and_settle_ordinals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        campaign = FailedAutoCampaign()
        anchors = await record_campaign(path, campaign)
        archive = Journal.open(path)
        records = archive.requests()
        archive.close()
        assert records[0].failure is not None and not records[0].delivered
        assert records[1].confirmed_operation_cursor == 0
        assert records[2].confirmed_operation_cursor == 0
        assert records[3].confirmed_operation_cursor == 1
        assert campaign.actions[1].operation_id == "recording:2"

        def forbidden(*args: object, **kwargs: object) -> Never:
            raise AssertionError("Replay must use recorded dependencies without IO or sleeps.")

        monkeypatch.setattr(asyncio, "sleep", forbidden)
        monkeypatch.setattr(ModClient, "__aenter__", forbidden)
        monkeypatch.setattr(Llm, "__aenter__", forbidden)
        monkeypatch.setattr(PackagedPrompts, "read", forbidden)
        monkeypatch.setattr(MaterialText, "read", forbidden)
        async with reach(path, anchors[3]) as result:
            assert result == Reached(anchors[3], 1, records[3].semantic_digest)

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


def make_pending(path: Path, sequence: int) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        summary = ModelSummary.model_validate_json(
            connection.execute(
                "SELECT value FROM summaries WHERE sequence=?", (sequence,)
            ).fetchone()[0]
        ).model_copy(update={"delivered": False, "failure": None, "physical_attempts": 0})
        connection.execute(
            "UPDATE model_requests SET delivered=0,failure=NULL WHERE sequence=?", (sequence,)
        )
        connection.execute("DELETE FROM model_attempts WHERE sequence=?", (sequence,))
        connection.execute(
            "UPDATE summaries SET is_error=1,value=? WHERE sequence=?",
            (summary.model_dump_json(), sequence),
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


def test_pending_target_and_unconsumed_suffix_remain_reachable(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, Campaign())
        journal = Journal.open(path)
        first = journal.request(anchors[0].model_sequence)
        suffix = next(
            record for record in journal.operations() if isinstance(record.request, Settle)
        )
        journal.close()
        make_pending(path, first.sequence)
        async with reach(path, anchors[1]) as result:
            assert result == Rejected(
                RejectReason.UNSUPPORTED_HISTORY,
                first.sequence,
                Unsupported(UnsupportedCause.INCOMPLETE_DEPENDENCY),
            )
        make_unknown(path, suffix)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO gaps VALUES (?,?,?)",
                (suffix.sequence + 1, suffix.sequence + 1, "storage_unavailable"),
            )
        source = path.read_bytes()
        async with reach(path, anchors[0]) as result:
            assert result == Reached(anchors[0], 0, first.semantic_digest)
        assert path.read_bytes() == source
        async with reach(path, anchors[2]) as result:
            assert isinstance(result, Rejected)
            assert result.reason is RejectReason.UNSUPPORTED_HISTORY

    asyncio.run(scenario())


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


def test_reach_rejects_game_material_order_swap(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, Campaign())
        # Both calls are before the first model request, at the same game cursor.
        with closing(sqlite3.connect(path)) as connection, connection:
            for table in ("records", "materials", "summaries"):
                connection.execute(f"UPDATE {table} SET sequence=-sequence WHERE sequence IN (2,7)")
                connection.execute(
                    f"UPDATE {table} SET sequence=CASE sequence WHEN -2 THEN 7 ELSE 2 END "
                    "WHERE sequence IN (-2,-7)"
                )
            connection.execute(
                "UPDATE summaries SET value=json_set(value,'$.sequence',sequence) "
                "WHERE sequence IN (2,7)"
            )
        async with reach(path, anchors[0]) as result:
            assert result == Rejected(
                RejectReason.ORDER_MISMATCH,
                2,
                OrderDifference(OperationKind.ADMIT, DependencyKind.MATERIAL),
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


def test_reach_reports_missing_target_and_unreadable_archive(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, Campaign())
        wrong = ModelAnchor("another-recording", anchors[0].model_sequence)
        async with reach(path, wrong) as result:
            assert result == Rejected(
                RejectReason.TARGET_NOT_FOUND, wrong.model_sequence, MissingTarget(wrong)
            )
        async with reach(tmp_path / "absent.sqlite", anchors[0]) as result:
            assert isinstance(result, Rejected)
            assert result.reason is RejectReason.UNSUPPORTED_HISTORY
            assert isinstance(result.detail, ArchiveUnreadable)

    asyncio.run(scenario())


def test_reach_rejects_stopped_prefix_without_leaking_tasks_or_changing_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        anchors = await record_campaign(path, Campaign())
        journal = Journal.open(path)
        first = journal.operations()[0]
        journal.close()
        replace_result(path, first.sequence, RecordedResult(outcome=Stopped(), observations=()))
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE summaries SET value=json_set(value,'$.result','stopped') WHERE sequence=?",
                (first.sequence,),
            )
        source = path.read_bytes()
        tasks_before = asyncio.all_tasks()
        try:
            async with reach(path, anchors[0]) as result:
                assert result == Rejected(
                    RejectReason.UNSUPPORTED_HISTORY,
                    None,
                    Unsupported(UnsupportedCause.UNEXPECTED_END),
                )
        finally:
            assert asyncio.all_tasks() == tasks_before
            assert path.read_bytes() == source

    asyncio.run(scenario())
