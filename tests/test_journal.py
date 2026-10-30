import asyncio
import sqlite3
import time
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from threading import Event, Thread

import httpx2 as httpx
import pytest
from test_execution import Source
from test_mod_client import COMBAT, RUN

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActionDecision, ActionKind, ActionProposal, ActionStep, ActTask
from sts2_harness.execution import Executed, Execution, ToolRecoveryBudget
from sts2_harness.game import LiveGame, Observe, Settlement
from sts2_harness.journal import (
    Journal,
    Manifest,
    Record,
    RecordedGame,
    ResultKind,
    SessionMaterials,
)
from sts2_harness.results import ActionUnknown, Reason, ReasonCode


def test_branch_readers_join_the_parent_prefix_with_local_evidence(tmp_path: Path) -> None:
    from hashlib import sha256

    from test_responses import configuration

    from sts2_harness.game import GameReply, OperationKind, Settle
    from sts2_harness.journal import (
        ArchiveError,
        DependencyKind,
        Gap,
        ModelSummary,
        ParentPrefix,
        evidence,
    )
    from sts2_harness.journal_models import (
        ConfigurationSnapshot,
        MaterialFailed,
        MaterialFailure,
        PromptRead,
        PromptReadResult,
        ToolSet,
        archive_input,
    )
    from sts2_harness.llm.messages import TextMessage, TextRole
    from sts2_harness.model_journal import RecordedPrompts
    from sts2_harness.prompts import PackagedPrompts

    observation = native.Observation.model_validate_json(RUN)
    route = native.RouteKey.model_validate(observation.location.model_dump())
    step = native.Interact(kind=native.StepKind.INTERACT, action="travel")
    config = ConfigurationSnapshot.of(configuration(tmp_path))
    messages = archive_input((TextMessage(TextRole.USER, "Synthetic current task."),))

    def model(journal: Journal, stream: str) -> int:
        sequence = journal.begin_model(
            native.DecisionOwner.RUN, stream, messages, ToolSet(tools=()), config
        )
        journal.finish_model(sequence)
        journal.end_model()
        return sequence

    def settle(journal: Journal, operation_id: str) -> None:
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
                observations=(observation,),
            )
        )
        journal.end()

    source = Journal.create(
        tmp_path / "parent.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="parent",
            core_version="source",
            game_instance_id="game",
            run_id="run",
        ),
    )
    settle(source, "parent:1")
    assert model(source, "run:1") == 2
    RecordedPrompts(PackagedPrompts(), source).read("run-tasks")
    assert model(source, "run:1") == 4
    RecordedPrompts(PackagedPrompts(), source).read("combat-tasks")
    model(source, "run:1")
    source.close()
    prefix = ParentPrefix(
        archive=source.path,
        recording_id="parent",
        before_sequence=4,
        archive_sha256=sha256(source.path.read_bytes()).hexdigest(),
    )
    branch = Journal.create(
        tmp_path / "branch.sqlite",
        source.manifest.model_copy(
            update={"recording_id": "branch", "core_version": "current", "parent": prefix}
        ),
    )
    assert model(branch, "run:1") == 4
    material_sequence = branch.begin_material(PromptRead(name="branch-only"))
    branch.finish_material(
        material_sequence,
        MaterialFailed(failure=MaterialFailure.UNAVAILABLE, message="Synthetic branch failure."),
    )
    settle(branch, "actual-B:91")
    assert model(branch, "run:1") == 7
    assert model(branch, "run:2") == 8
    branch.close()
    with closing(Journal.open(branch.path)) as reader:
        requests = reader.requests()
        assert [
            (item.sequence, item.stream, item.position, item.base_sequence) for item in requests
        ] == [
            (2, "run:1", 1, None),
            (4, "run:1", 2, None),
            (7, "run:1", 3, 4),
            (8, "run:2", 1, None),
        ]
        assert reader.request(2) == requests[0] and reader.request(4) == requests[1]
        assert all(item.input == messages for item in requests)
        first = reader.summaries(kind=DependencyKind.MODEL, start=4, stop=4)[0]
        assert isinstance(first, ModelSummary)
        assert (first.role, first.node_id, first.confirmed_operation_cursor, first.position) == (
            native.DecisionOwner.RUN,
            route.node_id,
            1,
            2,
        )
        assert [item.sequence for item in reader.timeline()] == list(range(1, 9))
        assert [item.sequence for item in reader.operations(limit=1)] == [6]
        assert [item.operation_id for item in reader.operations()] == ["parent:1", "actual-B:91"]
        assert [item.sequence for item in reader.summaries(limit=6)] == list(range(3, 9))
        assert [item.sequence for item in reader.timeline(kind=DependencyKind.MODEL, limit=2)] == [
            7,
            8,
        ]
        assert [item.sequence for item in reader.materials(start=3, stop=5)] == [3, 5]
        assert isinstance(reader.materials()[0].reply, PromptReadResult)
        assert reader.materials()[1].request == PromptRead(name="branch-only")
        assert [item.sequence for item in reader.summaries(errors_only=True)] == [5]
        assert [item.sequence for item in reader.summaries(kind=OperationKind.SETTLE, limit=2)] == [
            1,
            6,
        ]
        assert [item.sequence for item in evidence(reader, expand=True, sequence=4).records] == [4]
        assert [
            item.sequence
            for item in evidence(
                reader, role=native.DecisionOwner.RUN, node_id=route.node_id
            ).records
        ] == [1, 2, 4, 6, 7, 8]
        with closing(sqlite3.connect(branch.path)) as connection:
            local_blocks = connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        assert reader.storage.unique_blocks == local_blocks
        assert reader.gaps == ()
        with pytest.raises(ArchiveError):
            reader.operation(9)
    with closing(sqlite3.connect(branch.path)) as connection:
        assert connection.execute("SELECT MIN(sequence) FROM summaries").fetchone() == (4,)
        assert connection.execute("SELECT MIN(sequence) FROM model_requests").fetchone() == (4,)
    with closing(sqlite3.connect(source.path)) as connection, connection:
        connection.execute("INSERT INTO gaps VALUES (3,6,'storage_unavailable')")
    with pytest.raises(ArchiveError, match="parent recording has changed"):
        Journal.open(branch.path)
    changed_prefix = prefix.model_copy(
        update={"archive_sha256": sha256(source.path.read_bytes()).hexdigest()}
    )
    gaps_branch = Journal.create(
        tmp_path / "gaps.sqlite",
        branch.manifest.model_copy(update={"parent": changed_prefix}),
    )
    assert gaps_branch.gaps == (Gap(first_sequence=3, last_sequence=3),)
    gaps_branch.close()


def test_parent_hash_is_checked_before_following_a_corrupt_parent_chain(tmp_path: Path) -> None:
    from sts2_harness.journal import ArchiveError, ParentPrefix

    path = tmp_path / "self-parent.sqlite"
    journal = Journal.create(
        path,
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="branch",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.close()
    changed = journal.manifest.model_copy(
        update={
            "parent": ParentPrefix(
                archive=path, recording_id="branch", before_sequence=1, archive_sha256="0" * 64
            )
        }
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE manifest SET value=?", (changed.model_dump_json(),))
    with pytest.raises(ArchiveError, match="parent recording has changed"):
        Journal.open(path)


def test_execution_archive_reopens_with_partial_unknown_and_explicit_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        import json
        from io import StringIO

        from sts2_harness.diagnostics import Diagnostics

        console = StringIO()
        diagnostics = Diagnostics("session", console=console)
        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        posts: list[native.StepRequest] = []
        controls: list[str] = []
        paused = False
        waits = asyncio.Queue[None]()
        start = asyncio.Event()

        async def wait(seconds: float) -> None:
            await waits.put(None)
            await start.wait()

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal paused
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                    },
                )
            if request.url.path == "/control":
                paused = True
                controls.append("paused")
                return httpx.Response(200, json={"mode": "paused"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    content=native.Status(
                        location=observation.location,
                        control_intent=native.ControlIntent.PAUSED
                        if paused
                        else native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            assert request.url.path == "/action"
            step = native.StepRequest.model_validate_json(request.content)
            posts.append(step)
            active = Journal.open(journal.path).operation(len(posts) + 1)
            assert active.operation_id == step.operation_id
            assert active.reply is None and active.confirmed_operation_cursor == len(posts) - 1
            if len(posts) == 2:
                raise httpx.ReadTimeout("lost", request=request)
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        manifest = Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="session",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        )
        journal = Journal.create(tmp_path / "archive.sqlite", manifest)
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            live = LiveGame(
                client,
                namespace="session",
                audit=journal,
                game_instance_id="game",
                run_id="run",
                wait_for_start=True,
                wait=wait,
            )
            execution = Execution(
                RecordedGame(live, journal),
                game_instance_id="game",
                run_id="run",
                recovery=ToolRecoveryBudget(),
                diagnostics=diagnostics,
            )
            source = Source(
                ActionDecision(
                    ActionProposal(
                        tuple(
                            ActionStep(ActionKind.INTERACT, ref)
                            for ref in ("first", "second", "tail")
                        )
                    )
                )
            )
            result = await execution.run_unit(source, ActTask(route))
            assert isinstance(result, Executed) and isinstance(
                result.outcome.outcome, ActionUnknown
            )
            assert result.outcome.outcome.confirmed_steps == 1
            assert len(source.outcomes) == 1 and len(posts) == 2
            pending = asyncio.create_task(
                execution.pause_until_resumed(
                    Reason(code=ReasonCode.QUERY_FAILED, message="Read unavailable.")
                )
            )
            await waits.get()
            assert not pending.done()
            paused = False
            start.set()
            await pending
        journal.close()
        reopened = Journal.open(tmp_path / "archive.sqlite")
        records = reopened.operations()
        assert [record.sequence for record in records] == [1, 2, 3, 4, 5]
        assert [record.confirmed_operation_cursor for record in records] == [0, 1, 1, 1, 1]
        settlements = [
            record.reply.outcome
            for record in records
            if record.reply is not None and isinstance(record.reply.outcome, Settlement)
        ]
        assert [item.request for item in settlements] == posts
        assert settlements[1].receipt is None
        assert len(records[2].attempts) == 2
        assert records[-1].reply is not None
        assert records[0].reply is not None
        assert records[-1].reply.observations == (observation,)
        assert records[0].reply.observations == (observation,)
        assert controls == ["paused"]
        assert reopened.manifest == manifest and reopened.gaps == ()
        assert [step.operation_id for step in posts] == ["session:1", "session:2"]
        lines = [json.loads(line) for line in console.getvalue().splitlines()]
        submitted = [line for line in lines if line["event"] == "batch_submitted"]
        settled = [line for line in lines if line["event"] == "batch_settled"]
        assert len(submitted) == len(settled) == 1
        assert submitted[0]["planned"] == 3
        assert (settled[0]["planned"], settled[0]["confirmed"], settled[0]["uncertain_step"]) == (
            3,
            1,
            2,
        )
        assert settled[0]["level"] == "ERROR"
        diagnostics.close()

    asyncio.run(scenario())


def test_repeated_observations_deduplicate_without_losing_sequences(tmp_path: Path) -> None:
    async def scenario() -> None:
        from pydantic import MISSING

        observation = native.Observation.model_validate_json(RUN)
        counters = {
            "integer": native.RelicEntry(
                kind=native.ObjectKind.RELIC, definition_id="RELIC.TEST", name="Test", counter=0
            ),
            "float": native.RelicEntry(
                kind=native.ObjectKind.RELIC, definition_id="RELIC.TEST", name="Test", counter=0.0
            ),
            "null": native.RelicEntry(
                kind=native.ObjectKind.RELIC, definition_id="RELIC.TEST", name="Test", counter=None
            ),
            "omitted": native.RelicEntry(
                kind=native.ObjectKind.RELIC, definition_id="RELIC.TEST", name="Test"
            ),
        }
        observation = observation.model_copy(
            update={
                "objects": counters,
                "state": observation.state.model_copy(update={"relics": tuple(counters)}),
            }
        )

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/state"
            return httpx.Response(
                200,
                json={"status": "completed", "observation": observation.model_dump(mode="json")},
            )

        path = tmp_path / "archive.sqlite"
        journal = Journal.create(
            path,
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="session",
                    audit=journal,
                    game_instance_id="game",
                    run_id="run",
                ),
                journal,
            )
            first = await game.observe()
            storage = journal.storage
            second = await game.observe()
        reopened = Journal.open(path)
        assert reopened.storage == storage
        assert first == second
        assert [item.sequence for item in reopened.operations()] == [1, 2]
        assert [item.confirmed_operation_cursor for item in reopened.operations()] == [0, 0]
        second_record = reopened.operation(2)
        assert second_record.reply is not None
        assert second_record.reply.observations == (observation,)
        restored = second_record.reply.observations[0].objects
        integer, decimal, null, omitted = (
            restored[key] for key in ("integer", "float", "null", "omitted")
        )
        assert isinstance(integer, native.RelicEntry) and type(integer.counter) is int
        assert isinstance(decimal, native.RelicEntry) and type(decimal.counter) is float
        assert isinstance(null, native.RelicEntry) and null.counter is None
        assert isinstance(omitted, native.RelicEntry) and omitted.counter is MISSING

    asyncio.run(scenario())


def test_archive_io_gap_does_not_repeat_game_action_and_persists_after_recovery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from io import StringIO

        from sts2_harness.diagnostics import Diagnostics
        from sts2_harness.journal import Gap

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        directory = tmp_path / "recording"
        relocated = tmp_path / "temporarily-unavailable"
        console = StringIO()
        diagnostics = Diagnostics("session", console=console)
        journal = Journal.create(
            directory / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
            diagnostics=diagnostics,
        )
        posts = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                    },
                )
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    content=native.Status(
                        location=observation.location,
                        control_intent=native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            posts += 1
            step = native.StepRequest.model_validate_json(request.content)
            directory.rename(relocated)
            directory.write_text("storage unavailable")
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        source = Source(ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "first"),))))
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="session",
                    audit=journal,
                    game_instance_id="game",
                    run_id="run",
                ),
                journal,
            )
            execution = Execution(game, game_instance_id="game", run_id="run")
            result = await execution.run_unit(source, ActTask(route))
            assert isinstance(result, Executed) and len(source.outcomes) == 1 and posts == 1
            assert journal.gaps == (Gap(first_sequence=2, last_sequence=3),)
            assert "recording_gap" in console.getvalue()
            directory.unlink()
            relocated.rename(directory)
            await execution.observe()
        reopened = Journal.open(directory / "archive.sqlite")
        assert reopened.gaps == (Gap(first_sequence=2, last_sequence=3),)
        assert [record.sequence for record in reopened.operations()] == [1, 2, 4]
        assert reopened.operation(2).reply is None
        assert reopened.operation(4).confirmed_operation_cursor == 1
        assert "recording_recovered" in console.getvalue()
        assert posts == 1

    asyncio.run(scenario())


def test_offline_evidence_cli_filters_expands_and_exports_recorded_facts(tmp_path: Path) -> None:
    import json

    from sts2_harness.cli import main

    # Use the same public archive producer as normal gameplay, without loading any config.
    async def record() -> None:
        observation = native.Observation.model_validate_json(RUN)

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "completed", "observation": observation.model_dump(mode="json")},
            )

        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="historical notes",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="session",
                    audit=journal,
                    game_instance_id="game",
                    run_id="run",
                ),
                journal,
            )
            await game.observe()
            await game.observe()
            await game.observe()

    asyncio.run(record())
    destination = tmp_path / "selected.json"
    assert (
        main(
            [
                "journal",
                "--archive",
                str(tmp_path / "archive.sqlite"),
                "--role",
                "run",
                "--node",
                "node",
                "--around",
                "2",
                "--window",
                "0",
                "--expand",
                "--export",
                str(destination),
                "--env-file",
                str(tmp_path / "missing.env"),
            ]
        )
        == 0
    )
    exported = json.loads(destination.read_text())
    assert [item["sequence"] for item in exported["records"]] == [2]
    assert exported["records"][0]["reply"]["observations"][0] == json.loads(RUN)
    assert exported["manifest"]["initial_notes"] == "historical notes"
    assert exported["gaps"] == []
    assert main(["journal", "--archive", str(tmp_path / "archive.sqlite"), "--view", "model"]) == 0


def test_diagnostics_redact_limit_repeats_and_rotate_without_touching_evidence(
    tmp_path: Path,
) -> None:
    import json
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics, Event, EventKind
    from sts2_harness.game import OperationKind

    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="session",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    before = (tmp_path / "archive.sqlite").read_bytes()
    console = StringIO()
    diagnostics = Diagnostics("session", directory=tmp_path, console=console)
    diagnostics.started(OperationKind.PAUSE)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["stage"] == "inflight" and status["operation"] == "pause" and status["started_at"]
    secret = (
        "token=syntheticsecret https://user:credential@mod/path?key=querysecret "
        "Authorization: Basic authsecret"
    )
    diagnostics.emit(Event(event=EventKind.RECORDING_GAP, error=secret))
    diagnostics.emit(Event(event=EventKind.RECORDING_GAP, error=secret))
    diagnostics.emit(Event(event=EventKind.RECORDING_GAP, error="new cause"))
    diagnostics.emit(Event(event=EventKind.RECORDING_RECOVERED))
    initial = console.getvalue()
    assert all(
        secret not in initial
        for secret in ("syntheticsecret", "credential", "querysecret", "authsecret")
    )
    initial_events = [json.loads(line) for line in initial.splitlines()]
    assert len(initial_events) == 4
    assert initial_events[1]["error"].count("[redacted]") == 4
    diagnostics.emit(Event(event=EventKind.RECORDING_GAP, error="storage_unavailable"))
    diagnostics.emit(Event(event=EventKind.RECORDING_GAP, error="storage_unavailable"))
    diagnostics.emit(
        Event(event=EventKind.OPERATION_COMPLETED, result="failed", error="catalog_unavailable")
    )
    diagnostics.emit(Event(event=EventKind.RECORDING_GAP, error="storage_unavailable"))
    diagnostics.emit(Event(event=EventKind.RECORDING_RECOVERED))
    events = [json.loads(line) for line in console.getvalue().splitlines()]
    assert events[-2]["error"] == "catalog_unavailable"
    assert events[-1]["recovered_from"] == "storage_unavailable"
    assert events[-1]["suppressed"] == 2
    for index in range(12000):
        diagnostics.emit(
            Event(event=EventKind.OPERATION_COMPLETED, sequence=index, result="x" * 4000)
        )
    diagnostics.finished()
    diagnostics.close()
    assert json.loads((tmp_path / "status.json").read_text())["stage"] == "idle"
    segments = sorted(tmp_path.glob("diagnostics.jsonl*"))
    assert len(segments) == 3
    assert all(path.stat().st_size <= 10 * 1024 * 1024 for path in segments)
    assert (tmp_path / "archive.sqlite").read_bytes() == before
    assert Journal.open(journal.path).operations() == ()
    # The active file and console are the very same structured event stream.
    assert segments[0].read_text().splitlines()[-1] == console.getvalue().splitlines()[-1]


def test_same_initial_conditions_and_interactions_have_identical_correlations(
    tmp_path: Path,
) -> None:
    async def record(path: Path) -> tuple[Record, ...]:
        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())

        def respond(request: httpx.Request) -> httpx.Response:
            step = native.StepRequest.model_validate_json(request.content)
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        journal = Journal.create(
            path,
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="same-session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                seed="supplied",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="same-session",
                    audit=journal,
                    game_instance_id="game",
                    run_id="run",
                ),
                journal,
            )
            await game.settle(route, native.Interact(kind=native.StepKind.INTERACT, action="first"))
            await game.settle(
                route, native.Interact(kind=native.StepKind.INTERACT, action="second")
            )
        return Journal.open(path).operations()

    assert asyncio.run(record(tmp_path / "one.sqlite")) == asyncio.run(
        record(tmp_path / "two.sqlite")
    )


def test_native_control_reply_is_durable_while_start_is_still_waiting(tmp_path: Path) -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(RUN)
        waiting = asyncio.Event()
        release = asyncio.Event()
        paused = True

        async def wait(seconds: float) -> None:
            waiting.set()
            await release.wait()

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/control":
                return httpx.Response(200, json={"mode": "paused"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    content=native.Status(
                        location=observation.location,
                        control_intent=native.ControlIntent.PAUSED
                        if paused
                        else native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            return httpx.Response(
                200,
                json={"status": "completed", "observation": observation.model_dump(mode="json")},
            )

        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="session",
                    audit=journal,
                    game_instance_id="game",
                    run_id="run",
                    wait=wait,
                ),
                journal,
            )
            pending = asyncio.create_task(
                game.pause_until_resumed(
                    Reason(code=ReasonCode.QUERY_FAILED, message="Read unavailable.")
                )
            )
            await waiting.wait()
            try:
                record = Journal.open(journal.path).operation(1)
                assert record.reply is None
                assert len(record.attempts) == 2
                assert record.attempts[0].index == 1
                assert len(record.attempts[0].encoding_digest) == 64
                assert isinstance(record.attempts[0].reply, native.ControlReply)
            finally:
                paused = False
                release.set()
                await pending
        assert Journal.open(journal.path).operation(1).reply is not None

    asyncio.run(scenario())


def test_native_audit_rejects_missing_or_overlapping_record_owner(tmp_path: Path) -> None:
    from sts2_harness.game import Admit, Endpoint, NativeAttempt
    from sts2_harness.journal import ArchiveError

    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="session",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    with pytest.raises(ArchiveError, match="already exists"):
        Journal.create(journal.path, journal.manifest)
    attempt = NativeAttempt(
        endpoint=Endpoint.CONTROL, reply=native.ControlReply(mode=native.ControlIntent.PAUSED)
    )
    with pytest.raises(RuntimeError, match="requires an active"):
        journal.received(attempt)
    journal.begin(Admit())
    try:
        with pytest.raises(RuntimeError, match="already active"):
            journal.begin(Admit())
        journal.received(attempt)
    finally:
        journal.end()
    with pytest.raises(RuntimeError, match="requires an active"):
        journal.received(attempt)
    assert len(Journal.open(journal.path).operation(1).attempts) == 1


def test_initial_archive_disk_failure_keeps_gameplay_and_recovers_recording(tmp_path: Path) -> None:
    async def scenario() -> None:
        from io import StringIO

        from sts2_harness.diagnostics import Diagnostics
        from sts2_harness.journal import Gap

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        directory = tmp_path / "unwritable"
        directory.mkdir()
        directory.chmod(0o500)
        console = StringIO()
        posts = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal posts
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    content=native.Status(
                        location=observation.location,
                        control_intent=native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                    },
                )
            posts += 1
            step = native.StepRequest.model_validate_json(request.content)
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        try:
            journal = Journal.create(
                directory / "archive.sqlite",
                Manifest(
                    materials=SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    recording_id="session",
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                ),
                diagnostics=Diagnostics("session", console=console),
            )
            assert not journal.availability.manifest_persisted
            assert journal.availability.error is not None
            async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
                execution = Execution(
                    RecordedGame(
                        LiveGame(
                            client,
                            namespace="session",
                            audit=journal,
                            game_instance_id="game",
                            run_id="run",
                        ),
                        journal,
                    ),
                    game_instance_id="game",
                    run_id="run",
                )
                source = Source(
                    ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "first"),)))
                )
                result = await execution.run_unit(source, ActTask(route))
                assert isinstance(result, Executed) and posts == 1 and len(source.outcomes) == 1
                assert journal.gaps == (Gap(first_sequence=1, last_sequence=3),)
                directory.chmod(0o700)
                await execution.observe()
                assert (
                    journal.availability.manifest_persisted and journal.availability.error is None
                )
            reopened = Journal.open(journal.path)
            assert reopened.manifest == journal.manifest
            assert reopened.gaps == (Gap(first_sequence=1, last_sequence=3),)
            assert [record.sequence for record in reopened.operations()] == [4]
            assert reopened.operation(4).confirmed_operation_cursor == 1
            assert (
                "recording_gap" in console.getvalue()
                and "recording_recovered" in console.getvalue()
            )
            assert console.getvalue().count('"event":"recording_gap"') == 1
            import json

            gap_event = next(
                json.loads(line)
                for line in console.getvalue().splitlines()
                if json.loads(line).get("event") == "recording_gap"
            )
            assert gap_event["exception"]["type"] == "PermissionError"
            assert gap_event["os_error_code"] == 13
            assert "sqlite_error_code" not in gap_event
        finally:
            directory.chmod(0o700)

    asyncio.run(scenario())


def test_knowledge_catalog_and_current_state_are_separate_recorded_reads(tmp_path: Path) -> None:
    async def scenario() -> None:
        from sts2_harness.game import Cataloged, IOFailure, OperationKind
        from sts2_harness.llm.messages import ToolCall
        from sts2_harness.notes import Notes
        from sts2_harness.results import Failed
        from sts2_harness.tools import ToolCatalog

        observation = native.Observation.model_validate_json(RUN)
        catalogs = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal catalogs
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                    },
                )
            assert request.url.path == "/catalog"
            catalogs += 1
            if catalogs == 2:
                raise httpx.ReadTimeout("temporary", request=request)
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "data": {
                        "categories": ["relic"],
                        "card_filters": {
                            "pools": [],
                            "types": [],
                            "rarities": [],
                            "energy_costs": [0, "X"],
                        },
                    },
                },
            )

        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                RecordedGame(
                    LiveGame(
                        client,
                        namespace="session",
                        audit=journal,
                        game_instance_id="game",
                        run_id="run",
                    ),
                    journal,
                ),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=Notes(""),
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            completed = await tools.dispatch(
                ToolCall("first", "run_knowledge", '{"mode":"catalog"}')
            )
            failed = await tools.dispatch(ToolCall("second", "run_knowledge", '{"mode":"catalog"}'))
            assert isinstance(completed.data, native.CatalogData)
            assert isinstance(failed.outcome, Failed) and failed.observation == observation
        records = Journal.open(journal.path).operations()
        assert [item.request.kind for item in records] == [
            OperationKind.CATALOG,
            OperationKind.OBSERVE,
            OperationKind.CATALOG,
            OperationKind.OBSERVE,
        ]
        assert all(item.confirmed_operation_cursor == 0 for item in records)
        assert records[0].reply is not None and isinstance(records[0].reply.outcome, Cataloged)
        assert records[2].reply is not None and isinstance(records[2].reply.outcome, IOFailure)
        assert all(len(item.attempts) == 1 for item in records)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("endpoint", "code", "level", "status"),
    [
        ("inspect", "read_unavailable", "WARNING", "failed"),
        ("catalog", "catalog_unavailable", "WARNING", "failed"),
        ("inspect", "identity_unregistered", "ERROR", "failed"),
        ("catalog", "catalog_identity_conflict", "ERROR", "failed"),
        ("inspect", "area_unavailable", "INFO", "rejected"),
        ("catalog", "unsupported_category", "INFO", "rejected"),
        ("inspect", "internal_error", "ERROR", "failed"),
        ("catalog", "internal_error", "ERROR", "failed"),
        ("catalog", "invalid_request", "INFO", "rejected"),
    ],
)
def test_native_query_failures_keep_typed_diagnostic_cause(
    tmp_path: Path, endpoint: str, code: str, level: str, status: str
) -> None:
    import io
    import json

    from sts2_harness.diagnostics import Diagnostics

    async def scenario() -> None:
        console = io.StringIO()
        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
            diagnostics=Diagnostics("session", console=console),
        )

        def respond(request: httpx.Request) -> httpx.Response:
            payload: dict[str, object] = {
                "status": status,
                "error": {"code": code, "message": "Unavailable."},
            }
            if code == "invalid_request":
                payload["error"] = {"code": code, "message": "Invalid.", "fields": ["query"]}
            if endpoint == "inspect" and code != "internal_error":
                payload["observation"] = None
            return httpx.Response(
                500 if code == "internal_error" else 400 if code == "invalid_request" else 200,
                json=payload,
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="session",
                    game_instance_id="game",
                    run_id="run",
                    audit=journal,
                ),
                journal,
            )
            if endpoint == "inspect":
                await game.inspect(native.CardQuery(area=native.InspectArea.HAND, refs=("c1",)))
            else:
                await game.catalog(native.CatalogQuery(mode=native.CatalogMode.CATALOG))
        completed = json.loads(console.getvalue().splitlines()[-1])
        assert completed["event"] == "operation_completed"
        assert completed["result"] == status
        assert completed.get("error") == (code if status == "failed" else None)
        assert completed["level"] == level

    asyncio.run(scenario())


def test_pause_retry_diagnostics_and_durable_children_without_reply_history(tmp_path: Path) -> None:
    import io
    import json

    from sts2_harness.diagnostics import Diagnostics

    async def scenario() -> None:
        console = io.StringIO()
        observation = native.Observation.model_validate_json(RUN)
        status_reads = 0
        state_reads = 0
        waits = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal status_reads, state_reads
            if request.url.path == "/control":
                return httpx.Response(200, json={"mode": "paused"})
            if request.url.path == "/status":
                status_reads += 1
                if status_reads <= 2:
                    raise httpx.ReadTimeout("status unavailable")
                if status_reads == 3:
                    return httpx.Response(
                        500,
                        json={
                            "status": "failed",
                            "error": {"code": "internal_error", "message": "status unavailable"},
                        },
                    )
                return httpx.Response(
                    200,
                    content=native.Status(
                        location=observation.location,
                        control_intent=native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            state_reads += 1
            if state_reads == 1:
                return httpx.Response(
                    400,
                    json={
                        "status": "rejected",
                        "error": {
                            "code": "invalid_request",
                            "message": "Invalid state read.",
                            "fields": ["state"],
                        },
                    },
                )
            if state_reads <= 2:
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "read_unavailable", "message": "state unavailable"},
                    },
                )
            return httpx.Response(
                200,
                json={"status": "completed", "observation": observation.model_dump(mode="json")},
            )

        async def wait(seconds: float) -> None:
            nonlocal waits
            waits += 1
            lines = [json.loads(line) for line in console.getvalue().splitlines()]
            retries = [line for line in lines if line["event"] == "native_retry"]
            assert retries, "The first failed read must warn before the next wait."

        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="session",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
            diagnostics=Diagnostics("session", console=console),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="session",
                    game_instance_id="game",
                    run_id="run",
                    audit=journal,
                    wait=wait,
                ),
                journal,
            )
            reply = await game.pause_until_resumed(
                Reason(code=ReasonCode.QUERY_FAILED, message="Read unavailable.")
            )
        assert waits == 5
        assert "attempts" not in reply.model_dump()
        assert len(reply.observations) == 1
        reader = Journal.open(journal.path)
        record = reader.operation(1)
        assert len(record.attempts) == 10
        assert record.reply is not None
        reader.close()
        lines = [json.loads(line) for line in console.getvalue().splitlines()]
        retries = [line for line in lines if line["event"] == "native_retry"]
        recovered = [line for line in lines if line["event"] == "native_recovered"]
        assert len(retries) == 4
        assert [line["endpoint"] for line in retries] == ["status", "status", "state", "state"]
        assert [line["failures"] for line in retries] == [1, 3, 1, 2]
        assert [line["level"] for line in retries] == ["WARNING", "ERROR", "WARNING", "WARNING"]
        assert [(line["endpoint"], line["failures"]) for line in recovered] == [
            ("status", 3),
            ("state", 2),
        ]
        assert all(line["event"] != "recording_recovered" for line in lines)

    asyncio.run(scenario())


def test_pause_polling_slows_without_changing_busy_settlement_or_native_audit(
    tmp_path: Path,
) -> None:
    from sts2_harness.game import Endpoint

    observation = native.Observation.model_validate_json(RUN)
    busy = observation.model_copy(
        update={
            "input_readiness": native.InputReadiness.BUSY,
            "state": observation.state.model_copy(update={"control": native.StateControl.BUSY}),
        }
    )
    durations: list[float] = []
    status_index = 0
    state_index = 0
    operation_id = ""

    async def wait(seconds: float) -> None:
        durations.append(seconds)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal status_index, state_index, operation_id
        if request.url.path == "/state":
            state_index += 1
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=busy if state_index == 1 else observation,
                ).to_json(),
            )
        if request.url.path == "/control":
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/action":
            operation_id = native.StepRequest.model_validate_json(request.content).operation_id
            return httpx.Response(200, json={"operation_id": operation_id, "status": "pending"})
        assert request.url.path == "/status"
        status_index += 1
        if status_index == 8:
            return httpx.Response(503)
        receipt = None
        if status_index in (5, 6):
            receipt = {
                "operation_id": operation_id,
                "status": "pending" if status_index == 5 else "completed",
            }
            if status_index == 6:
                receipt["boundary"] = "ready"
        return httpx.Response(
            200,
            json={
                "location": observation.location.model_dump(mode="json"),
                "control_intent": "paused" if status_index in (1, 5, 6, 7) else "running",
                "input_readiness": "busy" if status_index in (2, 5) else "ready",
                "receipt": receipt,
            },
        )

    async def scenario() -> None:
        archive = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="polling",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="polling",
                    audit=archive,
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                    wait=wait,
                ),
                archive,
            )
            await game.admit()
            await game.settle(
                native.RouteKey.model_validate(observation.location.model_dump()),
                native.Interact(kind=native.StepKind.INTERACT, action="travel"),
            )
            await game.pause_until_resumed(
                Reason(code=ReasonCode.QUERY_FAILED, message="Required read unavailable.")
            )
        assert durations == [1.0, 0.1, 0.1, 0.1, 1.0, 1.0]
        assert [len(record.attempts) for record in archive.operations()] == [6, 3, 5]
        assert [
            attempt.endpoint for record in archive.operations() for attempt in record.attempts
        ].count(Endpoint.STATUS) == 9
        archive.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [2, 3, 4, 5, 6, 7, 8, 9])
def test_archive_open_rejects_old_versions_and_reads_current_version(
    tmp_path: Path, version: int
) -> None:
    import json
    import sqlite3

    from sts2_harness.journal import ArchiveError

    path = tmp_path / "archive.sqlite"
    journal = Journal.create(
        path,
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="schema",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.close()
    with sqlite3.connect(path) as connection:
        manifest = json.loads(connection.execute("SELECT value FROM manifest").fetchone()[0])
        manifest["schema_version"] = version
        connection.execute("UPDATE manifest SET value = ?", (json.dumps(manifest),))
    with pytest.raises(ArchiveError, match="schema is unsupported"):
        Journal.open(path)
    with sqlite3.connect(path) as connection:
        manifest["schema_version"] = 10
        connection.execute("UPDATE manifest SET value = ?", (json.dumps(manifest),))
    reopened = Journal.open(path)
    assert reopened.manifest.schema_version == 10
    assert reopened.operations() == ()
    reopened.close()


def test_stopping_start_wait_finishes_the_recorded_operation(tmp_path: Path) -> None:
    from sts2_harness.game import SessionStopped, Stopped, unwrap
    from sts2_harness.journal import OperationSummary, ResultKind, summarize

    stop = asyncio.Event()
    observation = native.Observation.model_validate_json(RUN)

    async def wait(seconds: float) -> None:
        stop.set()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        return httpx.Response(
            200,
            json={
                "location": observation.location.model_dump(mode="json"),
                "control_intent": "paused",
                "input_readiness": "ready",
                "receipt": None,
            },
        )

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="stop",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="stop",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                    audit=journal,
                    wait=wait,
                    stop_requested=stop.is_set,
                ),
                journal,
            )
            reply = await game.admit()
        assert isinstance(reply.outcome, Stopped)
        with pytest.raises(SessionStopped):
            unwrap(reply)
        journal.close()
        reopened = Journal.open(journal.path)
        record = reopened.operation(1)
        summary = summarize(record)
        assert isinstance(summary, OperationSummary)
        assert summary.result is ResultKind.STOPPED
        assert record.reply is not None and isinstance(record.reply.outcome, Stopped)
        assert len(record.attempts) == 1
        assert reopened.gaps == ()
        reopened.close()

    asyncio.run(scenario())


def test_resident_diagnostics_preserve_lifecycle_and_control_during_inflight_io(
    tmp_path: Path,
) -> None:
    import json
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics, Lifecycle, Stage
    from sts2_harness.game import OperationKind

    console = StringIO()
    diagnostics = Diagnostics(None, directory=tmp_path, console=console)
    observation = native.Observation.model_validate_json(RUN)
    diagnostics.bind_recording("recording", observation.location, tmp_path / "archive.sqlite")
    diagnostics.lifecycle(Lifecycle.RUNNING)
    diagnostics.started(OperationKind.SETTLE, sequence=3)
    diagnostics.submitting("recording:1")
    diagnostics.native_status(
        native.Status(
            location=observation.location,
            control_intent=native.ControlIntent.PAUSED,
            input_readiness=native.InputReadiness.BUSY,
            receipt=None,
        ),
        False,
    )
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["lifecycle"] == "running"
    assert status["control_intent"] == "paused"
    assert status["stage"] == Stage.INFLIGHT.value
    assert status["operation"] == "settle" and status["sequence"] == 3
    assert status["operation_id"] == "recording:1"
    assert status["started_at"] is not None
    diagnostics.finished()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["stage"] == Stage.IDLE.value and status["lifecycle"] == "running"
    assert status["recording_id"] == "recording"
    diagnostics.close()


def test_evidence_filters_fixed_window_and_latest_matching_summaries(tmp_path: Path) -> None:
    from sts2_harness.client.http import ClientFailureKind
    from sts2_harness.game import GameReply, IOFailure, Observe, Observed
    from sts2_harness.journal import OperationSummary, evidence

    journal = Journal.create(
        tmp_path / "selection.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="selection",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    original = native.Observation.model_validate_json(RUN)
    for index in range(25):
        location = original.location.model_copy(
            update={"node_id": "even" if index % 2 == 0 else "odd"}
        )
        observation = original.model_copy(update={"location": location})
        journal.begin(Observe())
        journal.finish(
            GameReply(outcome=Observed(observation=observation), observations=(observation,))
        )
        journal.end()
    journal.begin(Observe())
    journal.finish(GameReply[Observed](outcome=IOFailure(reason=ClientFailureKind.CONNECTION)))
    journal.end()
    journal.begin(Observe())
    journal.end()
    journal.close()
    reader = Journal.open(journal.path)
    latest = evidence(reader, role=native.DecisionOwner.RUN, node_id="even")
    assert [row.sequence for row in latest.records] == list(range(1, 26, 2))
    assert all(isinstance(row, OperationSummary) for row in latest.records)
    window = evidence(reader, around=12, window=2, node_id="even", role=native.DecisionOwner.RUN)
    assert [row.sequence for row in window.records] == [11, 13]
    failures = evidence(reader, errors_only=True)
    assert [row.sequence for row in failures.records] == [26, 27]
    assert [row.sequence for row in evidence(reader).records] == list(range(8, 28))
    reader.close()


def test_summary_queries_do_not_expand_bodies_and_detect_hidden_index_damage(
    tmp_path: Path,
) -> None:
    import sqlite3

    from sts2_harness.game import GameReply, Observe, Observed
    from sts2_harness.journal import ArchiveError, OperationSummary, evidence

    journal = Journal.create(
        tmp_path / "damage.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="damage",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    observation = native.Observation.model_validate_json(RUN)
    journal.begin(Observe())
    journal.finish(
        GameReply(outcome=Observed(observation=observation), observations=(observation,))
    )
    journal.end()
    journal.close()
    reader = Journal.open(journal.path)
    # Missing body blocks must not force the summary view to decode historical input/state.
    with sqlite3.connect(journal.path) as connection:
        connection.execute("DELETE FROM blobs")
    assert isinstance(evidence(reader).records[0], OperationSummary)
    with pytest.raises(ArchiveError, match="missing"):
        evidence(reader, expand=True)
    # A damaged filter column must not quietly turn matching evidence into an empty result.
    with sqlite3.connect(journal.path) as connection:
        connection.execute("UPDATE summaries SET node_id='invented'")
    with pytest.raises(ArchiveError, match="summary"):
        evidence(reader, node_id="node")
    reader.close()


def test_error_filter_excludes_normal_pause_stop_and_rejected_receipt(tmp_path: Path) -> None:
    from sts2_harness.game import Admit, Admitted, GameReply, Pause, Settle, Stopped
    from sts2_harness.journal import evidence

    journal = Journal.create(
        tmp_path / "normal.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="normal",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.begin(Admit())
    journal.finish(GameReply(outcome=Admitted(allowed=False)))
    journal.end()
    journal.begin(Pause())
    journal.finish(GameReply[Admitted](outcome=Stopped()))
    journal.end()
    observation = native.Observation.model_validate_json(RUN)
    request = native.StepRequest(
        operation_id="normal:1",
        expected_route=native.RouteKey.model_validate(observation.location.model_dump()),
        step=native.Interact(kind=native.StepKind.INTERACT, action="travel"),
    )
    journal.begin(Settle(route=request.expected_route, step=request.step))
    journal.finish(
        GameReply(
            outcome=Settlement(
                request=request,
                receipt=native.ReceiptRejected(
                    operation_id="normal:1",
                    status=native.ResultStatus.REJECTED,
                    error=native.ActionUnavailable(
                        code=native.ErrorCode.NATIVE_BUSY, message="Still settling."
                    ),
                ),
            )
        )
    )
    journal.end()
    journal.close()
    reader = Journal.open(journal.path)
    assert len(evidence(reader).records) == 3
    assert evidence(reader, errors_only=True).records == ()
    reader.close()


@pytest.mark.parametrize("result", [ResultKind.UNKNOWN, ResultKind.FAILED, ResultKind.INCOMPLETE])
def test_settlement_errors_reopen_with_request_role_and_node(
    tmp_path: Path, result: ResultKind
) -> None:
    from sts2_harness.game import GameReply, Settle
    from sts2_harness.journal import OperationSummary, evidence

    journal = Journal.create(
        tmp_path / "settlement.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="settle",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    route = native.RouteKey(
        game_instance_id="game", run_id="run", node_id="node", owner=native.DecisionOwner.RUN
    )
    for index, requested_route in enumerate(
        (
            route,
            route.model_copy(update={"node_id": "other"}),
            route.model_copy(update={"owner": native.DecisionOwner.COMBAT}),
        ),
        start=1,
    ):
        request = native.StepRequest(
            operation_id=f"settle:{index}",
            expected_route=requested_route,
            step=native.Interact(kind=native.StepKind.INTERACT, action="travel"),
        )
        journal.begin(Settle(route=requested_route, step=request.step))
        if result is not ResultKind.INCOMPLETE:
            receipt = (
                native.ReceiptFailed(
                    operation_id=request.operation_id,
                    status=native.ResultStatus.FAILED,
                    error=native.ActionFailure(
                        code=native.ErrorCode.DISPATCH_FAILED, message="Dispatch failed."
                    ),
                )
                if result is ResultKind.FAILED
                else native.ReceiptUnknown(
                    operation_id=request.operation_id,
                    status=native.ResultStatus.UNKNOWN,
                    error=native.UnknownExecution(
                        code=native.ErrorCode.EXECUTION_UNCERTAIN, message="Execution unconfirmed."
                    ),
                )
            )
            journal.finish(GameReply(outcome=Settlement(request=request, receipt=receipt)))
        journal.end()
    journal.close()

    reader = Journal.open(journal.path)
    selected = evidence(reader, role=native.DecisionOwner.RUN, node_id="node", errors_only=True)
    assert len(selected.records) == 1
    summary = selected.records[0]
    assert isinstance(summary, OperationSummary)
    assert (summary.sequence, summary.role, summary.node_id, summary.result) == (
        1,
        native.DecisionOwner.RUN,
        "node",
        result,
    )
    assert [
        row.sequence
        for row in evidence(
            reader, role=native.DecisionOwner.RUN, node_id="other", errors_only=True
        ).records
    ] == [2]
    assert [
        row.sequence
        for row in evidence(
            reader, role=native.DecisionOwner.COMBAT, node_id="node", errors_only=True
        ).records
    ] == [3]
    reader.close()


def test_settlement_summary_discards_explicit_observation_route_conflicts(tmp_path: Path) -> None:
    from sts2_harness.game import GameReply, Settle
    from sts2_harness.journal import OperationSummary, evidence

    journal = Journal.create(
        tmp_path / "conflicts.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="settle",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    route = native.RouteKey.model_validate(observation.location.model_dump())
    terminal = observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"owner": None}),
            "input_readiness": native.InputReadiness.TERMINAL,
            "state": native.TerminalState(
                phase=native.Phase.TERMINAL,
                screen=native.TerminalScreen.RUN_COMPLETE,
                control=native.StateControl.UNAVAILABLE,
                progress=observation.state.progress,
                player=native.Player.model_validate(observation.state.player.model_dump()),
                relics=observation.state.relics,
                potions=observation.state.potions,
                actions=(),
                selection=None,
                result=native.TerminalResult.VICTORY,
                deck=observation.state.deck,
            ),
        }
    )
    for index, observed in enumerate(
        (
            observation,
            terminal,
            observation.model_copy(
                update={
                    "location": observation.location.model_copy(
                        update={"game_instance_id": "other-game"}
                    ),
                    "stamp": observation.stamp.model_copy(
                        update={"game_instance_id": "other-game"}
                    ),
                }
            ),
            observation.model_copy(
                update={"location": observation.location.model_copy(update={"run_id": "other-run"})}
            ),
            observation.model_copy(
                update={
                    "location": observation.location.model_copy(update={"node_id": "other-node"})
                }
            ),
            native.Observation.model_validate_json(COMBAT),
        ),
        start=1,
    ):
        request = native.StepRequest(
            operation_id=f"settle:{index}",
            expected_route=route,
            step=native.Interact(kind=native.StepKind.INTERACT, action="travel"),
        )
        journal.begin(Settle(route=route, step=request.step))
        journal.finish(
            GameReply(
                outcome=Settlement(request=request, receipt=None),
                observations=(observation, observed),
            )
        )
        journal.end()
    journal.close()

    reader = Journal.open(journal.path)
    summaries = evidence(reader, errors_only=True).records
    assert len(summaries) == 6
    for index, summary in enumerate(summaries, start=1):
        assert isinstance(summary, OperationSummary)
        assert (summary.role, summary.node_id) == (
            (native.DecisionOwner.RUN, "node") if index <= 2 else (None, None)
        )
    assert [
        row.sequence
        for row in evidence(
            reader, role=native.DecisionOwner.RUN, node_id="node", errors_only=True
        ).records
    ] == [1, 2]
    reader.close()


def test_short_read_transaction_preserves_the_contending_write(tmp_path: Path) -> None:
    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="reader",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    started = Event()
    written = Future[None]()

    def append() -> None:
        started.set()
        try:
            journal.begin(Observe())
            journal.end()
        except BaseException as error:
            written.set_exception(error)
        else:
            written.set_result(None)

    journal.begin(Observe())
    journal.end()
    with closing(sqlite3.connect(f"{journal.path.as_uri()}?mode=ro", uri=True)) as lock:
        lock.execute("BEGIN")
        lock.execute("SELECT COUNT(*) FROM records").fetchone()
        writer = Thread(target=append)
        writer.start()
        try:
            assert started.wait(1), "Writer did not start."
            time.sleep(0.05)
        finally:
            lock.rollback()
            writer.join(timeout=2)
        assert not writer.is_alive(), "Writer did not finish after releasing the read lock."
        written.result(timeout=0)
    journal.begin(Observe())
    journal.end()
    journal.close()

    reader = Journal.open(journal.path)
    try:
        assert [item.sequence for item in reader.summaries()] == [1, 2, 3]
        assert reader.gaps == ()
    finally:
        reader.close()


def test_public_reader_does_not_drop_game_evidence(tmp_path: Path) -> None:
    import shutil

    from sts2_harness.game import GameReply, Observed

    observation = native.Observation.model_validate_json(RUN)
    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="reader",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    reply = GameReply(outcome=Observed(observation=observation), observations=(observation,))
    journal.begin(Observe())
    journal.finish(reply)
    journal.end()
    with closing(sqlite3.connect(f"{journal.path.as_uri()}?mode=ro", uri=True)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM records").fetchone()
        started = time.monotonic()
        journal.begin(Observe())
        journal.finish(reply)
        journal.end()
        assert time.monotonic() - started < 1
        assert journal.gaps == ()
    journal.close()

    retained = tmp_path / "retained" / "archive.sqlite"
    retained.parent.mkdir()
    shutil.copyfile(journal.path, retained)
    retained.chmod(0o444)
    retained.parent.chmod(0o555)
    try:
        with closing(Journal.open(retained)) as reopened:
            records = reopened.operations()
            assert [record.sequence for record in records] == [1, 2]
            assert all(record.reply is not None for record in records)
            assert all(
                record.reply.observations == (observation,) for record in records if record.reply
            )
            assert reopened.gaps == ()
        assert tuple(retained.parent.iterdir()) == (retained,)
    finally:
        retained.parent.chmod(0o700)
        retained.chmod(0o600)


def test_recording_cannot_claim_single_file_seal_while_reader_holds_wal(tmp_path: Path) -> None:
    from sts2_harness.game import GameReply, Observed
    from sts2_harness.journal import ArchiveError

    observation = native.Observation.model_validate_json(RUN)
    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="sealing",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.begin(Observe())
    journal.end()
    with closing(sqlite3.connect(f"{journal.path.as_uri()}?mode=ro", uri=True)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM records").fetchone()
        journal.begin(Observe())
        journal.finish(
            GameReply(outcome=Observed(observation=observation), observations=(observation,))
        )
        journal.end()
        with pytest.raises(ArchiveError, match="Cannot seal"):
            journal.close()
    journal.close()
    with closing(Journal.open(journal.path)) as reopened:
        assert len(reopened.operations()) == 2
        assert reopened.operation(2).reply is not None
        assert reopened.gaps == ()


def test_sealing_after_storage_recovers_retains_unwritten_gap_evidence(tmp_path: Path) -> None:
    from sts2_harness.journal import ArchiveError

    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="seal-recovery",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.begin(Observe())
    journal.end()
    retained = tmp_path / "retained.sqlite"
    journal.path.rename(retained)
    journal.path.mkdir()
    try:
        journal.begin(Observe())
        journal.end()
        with pytest.raises(ArchiveError, match="Cannot seal"):
            journal.close()
    finally:
        journal.path.rmdir()
        retained.rename(journal.path)
    journal.close()
    with closing(Journal.open(journal.path)) as reopened:
        assert [(gap.first_sequence, gap.last_sequence) for gap in reopened.gaps] == [(2, 2)]
        assert [record.sequence for record in reopened.operations()] == [1]


def test_uninitialized_recording_cannot_seal_until_storage_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sts2_harness.journal import ArchiveError

    connect = sqlite3.connect

    def unavailable(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("Synthetic initial storage failure")

    monkeypatch.setattr(sqlite3, "connect", unavailable)
    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="initial-recovery",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    journal.begin(Observe())
    journal.end()
    assert not journal.availability.manifest_persisted
    with pytest.raises(ArchiveError, match="Cannot seal"):
        journal.close()
    monkeypatch.setattr(sqlite3, "connect", connect)
    journal.begin(Observe())
    journal.end()
    journal.close()
    with closing(Journal.open(journal.path)) as reopened:
        assert reopened.manifest.recording_id == "initial-recovery"
        assert [record.sequence for record in reopened.operations()] == [2]
        assert [(gap.first_sequence, gap.last_sequence) for gap in reopened.gaps] == [(1, 1)]
