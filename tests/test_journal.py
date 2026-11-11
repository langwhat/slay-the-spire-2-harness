import asyncio
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from test_execution import Source
from test_mod_client import RUN

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActionDecision, ActionKind, ActionProposal, ActionStep, ActTask
from sts2_harness.execution import Executed, Execution, ToolRecoveryBudget
from sts2_harness.game import LiveGame, Observe, Settlement
from sts2_harness.journal import (
    Journal,
    Manifest,
    RecordedGame,
    SessionMaterials,
)
from sts2_harness.results import ActionUnknown, Reason, ReasonCode


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
                        presentation=None,
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
                        presentation=None,
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
        import json

        seal_failure = next(
            json.loads(line)
            for line in (tmp_path / "diagnostics.jsonl").read_text().splitlines()
            if json.loads(line).get("event") == "recording_seal_failed"
        )
        assert seal_failure["error"] == "Cannot seal a recording while database readers are active."
        assert seal_failure["exception"]["type"] == "ArchiveError"
        assert seal_failure["exception"]["frames"][-1]["function"] == "close"
        assert journal.gaps == ()
    journal.close()
    with closing(Journal.open(journal.path)) as reopened:
        assert len(reopened.operations()) == 2
        assert reopened.operation(2).reply is not None
        assert reopened.gaps == ()
