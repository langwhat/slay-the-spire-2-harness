import asyncio
from pathlib import Path

import httpx2 as httpx
import pytest
from test_execution import Source
from test_mod_client import RUN

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActionDecision, ActionKind, ActionProposal, ActionStep, ActTask
from sts2_harness.execution import Executed, Execution, ToolRecoveryBudget
from sts2_harness.game import LiveGame, Settlement
from sts2_harness.journal import Journal, Manifest, Record, RecordedGame
from sts2_harness.results import ActionUnknown, Reason, ReasonCode


def test_execution_archive_reopens_with_partial_unknown_and_explicit_start(tmp_path: Path) -> None:
    async def scenario() -> None:
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
            recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
            recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
            recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="session", core_version="test", game_instance_id="game", run_id="run"
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
                recording_id="polling", core_version="test", game_instance_id="game", run_id="run"
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
        assert durations == [1.0, 0.1, 0.1, 0.1, 1.0, 0.1]
        assert [len(record.attempts) for record in archive.operations()] == [6, 3, 5]
        assert [
            attempt.endpoint for record in archive.operations() for attempt in record.attempts
        ].count(Endpoint.STATUS) == 9
        archive.close()

    asyncio.run(scenario())


def test_archive_open_rejects_version_two_and_reads_current_version(tmp_path: Path) -> None:
    import json
    import sqlite3

    from sts2_harness.journal import ArchiveError

    path = tmp_path / "archive.sqlite"
    journal = Journal.create(
        path,
        Manifest(recording_id="schema", core_version="test", game_instance_id="game", run_id="run"),
    )
    journal.close()
    with sqlite3.connect(path) as connection:
        manifest = json.loads(connection.execute("SELECT value FROM manifest").fetchone()[0])
        manifest["schema_version"] = 2
        connection.execute("UPDATE manifest SET value = ?", (json.dumps(manifest),))
    with pytest.raises(ArchiveError, match="schema is unsupported"):
        Journal.open(path)
    with sqlite3.connect(path) as connection:
        manifest["schema_version"] = 3
        connection.execute("UPDATE manifest SET value = ?", (json.dumps(manifest),))
    reopened = Journal.open(path)
    assert reopened.manifest.schema_version == 3
    assert reopened.operations() == ()
    reopened.close()
