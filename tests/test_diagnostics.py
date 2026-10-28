import json
from io import StringIO
from pathlib import Path

import pytest

from sts2_harness.diagnostics import Diagnostics, Event, EventKind
from sts2_harness.game import Endpoint
from sts2_harness.journal import SessionMaterials


@pytest.mark.parametrize(
    ("event", "component", "level"),
    [
        (Event(event=EventKind.LIFECYCLE_CHANGED, result="failed"), "session", "ERROR"),
        (Event(event=EventKind.MODEL_REQUEST_COMPLETED), "model", "INFO"),
        (Event(event=EventKind.NATIVE_FAILED, error="timeout"), "game_control", "WARNING"),
    ],
)
def test_events_report_component_and_level(event: Event, component: str, level: str) -> None:
    console = StringIO()
    diagnostics = Diagnostics("recording", console=console)
    diagnostics.emit(event)
    diagnostics.close()
    line = json.loads(console.getvalue())
    assert (line["component"], line["level"]) == (component, level)


def test_results_and_retry_windows_preserve_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = StringIO()
    now = 0.0
    monkeypatch.setattr("sts2_harness.diagnostics.time.monotonic", lambda: now)
    diagnostics = Diagnostics("recording", console=console)
    diagnostics.emit(
        Event(event=EventKind.OPERATION_COMPLETED, result="rejected", error="invalid_arguments")
    )
    diagnostics.emit(
        Event(event=EventKind.OPERATION_COMPLETED, result="unknown", error="execution_unconfirmed")
    )
    for sequence, cause, failures in ((1, "timeout", 1), (1, "timeout", 2), (2, "timeout", 1)):
        diagnostics.emit(
            Event(
                event=EventKind.NATIVE_RETRY,
                sequence=sequence,
                endpoint=Endpoint.STATE,
                error=cause,
                failures=failures,
            )
        )
    now = 60
    diagnostics.emit(
        Event(
            event=EventKind.NATIVE_RETRY,
            sequence=1,
            endpoint=Endpoint.STATE,
            error="timeout",
            failures=3,
        )
    )
    diagnostics.emit(
        Event(
            event=EventKind.NATIVE_RETRY,
            sequence=1,
            endpoint=Endpoint.STATE,
            error="connection",
            failures=4,
        )
    )
    diagnostics.emit(
        Event(
            event=EventKind.NATIVE_RECOVERED,
            sequence=1,
            endpoint=Endpoint.STATE,
            failures=4,
            recovered_from="connection",
        )
    )
    diagnostics.close()
    lines = [json.loads(line) for line in console.getvalue().splitlines()]
    assert [line["level"] for line in lines[:2]] == ["INFO", "ERROR"]
    retries = [line for line in lines if line["event"] == "native_retry"]
    assert [(line["sequence"], line["failures"], line["suppressed"]) for line in retries] == [
        (1, 1, 0),
        (2, 1, 0),
        (1, 3, 1),
        (1, 4, 0),
    ]
    assert lines[-1]["failures"] == 4 and lines[-1]["level"] == "INFO"


def test_debug_metadata_and_utf8_events_share_three_bounded_segments(tmp_path: Path) -> None:
    import logging

    from sts2_harness.diagnostics import EncodingMetadata

    quiet = StringIO()
    with (tmp_path / "console").open("w") as console:
        diagnostics = Diagnostics(
            "多" * 1_000_000, directory=tmp_path, console=console, level=logging.DEBUG
        )
        for index in range(2300):
            diagnostics.emit(
                EncodingMetadata(sequence=index, digest="界" * (1_000_000 if index == 0 else 4000))
            )
        diagnostics.close()
    segments = tuple(tmp_path.glob("diagnostics.jsonl*"))
    assert len(segments) == 3
    assert all(0 < segment.stat().st_size <= 10 * 1024 * 1024 for segment in segments)
    for segment in segments:
        lines = segment.read_text().splitlines()
        assert lines and all(json.loads(line)["level"] == "DEBUG" for line in lines)
    diagnostics = Diagnostics("recording", console=quiet)
    diagnostics.emit(EncodingMetadata(sequence=1, digest="synthetic"))
    diagnostics.emit(Event(event=EventKind.OPERATION_COMPLETED, result="completed"))
    diagnostics.close()
    assert len(quiet.getvalue().splitlines()) == 1


def test_failed_destinations_disable_themselves_without_blocking_evidence(tmp_path: Path) -> None:
    from sts2_harness.diagnostics import Lifecycle
    from sts2_harness.journal import Journal, Manifest

    class BrokenConsole(StringIO):
        writes = 0

        def write(self, text: str) -> int:
            self.writes += 1
            raise OSError("Synthetic sink unavailable")

    console = BrokenConsole()
    broken_file = tmp_path / "diagnostics.jsonl"
    broken_file.mkdir()
    blocked = tmp_path / "status.json.tmp"
    blocked.mkdir()
    diagnostics = Diagnostics("recording", directory=tmp_path, console=console)
    diagnostics.lifecycle(Lifecycle.RUNNING)
    blocked.rmdir()
    broken_file.rmdir()
    diagnostics.lifecycle(Lifecycle.PAUSED)
    journal = Journal.create(
        tmp_path / "evidence.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="recording",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
        diagnostics=diagnostics,
    )
    journal.close()
    diagnostics.close()
    assert console.writes == 1
    assert not (tmp_path / "status.json").exists()
    assert not broken_file.exists()
    reader = Journal.open(journal.path)
    assert reader.operations() == ()
    reader.close()


def test_native_failures_finish_each_logical_operation_without_losing_attempts(
    tmp_path: Path,
) -> None:
    import asyncio

    import httpx2 as httpx
    from test_mod_client import RUN

    from sts2_harness.client import ModClient
    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame
    from sts2_harness.journal import Journal, Manifest, RecordedGame

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics("recording", console=console)
        journal = Journal.create(
            tmp_path / "evidence.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="recording",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
            diagnostics=diagnostics,
        )
        reads = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal reads
            reads += 1
            if reads <= 2:
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=native.Observation.model_validate_json(RUN),
                ).to_json(),
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="recording",
                    game_instance_id="game",
                    run_id="run",
                    audit=journal,
                ),
                journal,
            )
            for _ in range(3):
                await game.observe()
        journal.close()
        diagnostics.close()
        reader = Journal.open(journal.path)
        assert [len(record.attempts) for record in reader.operations()] == [1, 1, 1]
        reader.close()
        lines = [json.loads(line) for line in console.getvalue().splitlines()]
        retries = [line for line in lines if line["event"] == "native_retry"]
        exhausted = [line for line in lines if line["event"] == "native_failed"]
        assert [(line["sequence"], line["failures"]) for line in retries] == [(1, 1), (2, 1)]
        assert [(line["sequence"], line["failures"], line["level"]) for line in exhausted] == [
            (1, 1, "WARNING"),
            (2, 1, "WARNING"),
        ]

    asyncio.run(scenario())


def test_discovery_recovery_reports_owner_totals_without_a_journal_sequence() -> None:
    import asyncio

    import httpx2 as httpx
    from test_external import FakeMod

    from sts2_harness.client import ModClient
    from sts2_harness.external import ExternalBackend

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics(None, console=console)
        mod = FakeMod()
        failures = 0
        waits = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal failures
            if request.url.path == "/status" and failures < 2:
                failures += 1
                raise httpx.ConnectError("Synthetic unavailable")
            return await mod.respond(request)

        async def wait(seconds: float) -> None:
            nonlocal waits
            waits += 1
            if waits == 3:
                backend.stop_accepting()

        async with ExternalBackend(
            ModClient("http://mod", transport=httpx.MockTransport(respond)),
            diagnostics=diagnostics,
            wait=wait,
        ) as backend:
            await backend.maintain()
        diagnostics.close()
        lines = [json.loads(line) for line in console.getvalue().splitlines()]
        retries = [line for line in lines if line["event"] == "discovery_retry"]
        recovered = [line for line in lines if line["event"] == "discovery_recovered"]
        assert len(retries) == len(recovered) == 1
        assert retries[0]["failures"] == 1
        assert recovered[0]["failures"] == 2 and recovered[0]["suppressed"] == 1
        assert recovered[0]["recovered_from"] == "connection"
        assert "sequence" not in recovered[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["cancel", "observe_failure"])
def test_batch_summary_keeps_confirmed_steps_during_interruption(mode: str) -> None:
    import asyncio

    import httpx2 as httpx
    from test_execution import Source
    from test_mod_client import RUN

    from sts2_harness.client import ModClient
    from sts2_harness.client import models as native
    from sts2_harness.decisions import (
        ActionDecision,
        ActionKind,
        ActionProposal,
        ActionStep,
        ActTask,
    )
    from sts2_harness.execution import Executed, Execution
    from sts2_harness.game import LiveGame

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics("recording", console=console)
        observation = native.Observation.model_validate_json(RUN)
        pending = asyncio.Event()
        actions = 0
        reads = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal actions, reads
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
                reads += 1
                if reads == 2:
                    raise httpx.ReadTimeout("synthetic final read failure")
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED,
                        observation=observation,
                    ).to_json(),
                )
            actions += 1
            if mode == "cancel" and actions == 2:
                pending.set()
                await asyncio.Future[None]()
            step = native.StepRequest.model_validate_json(request.content)
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="synthetic", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                diagnostics=diagnostics,
            )
            source = Source(
                ActionDecision(
                    ActionProposal(
                        tuple(
                            ActionStep(ActionKind.INTERACT, name)
                            for name in ("first", "second", "third")
                        )
                    )
                )
            )
            task = asyncio.create_task(
                execution.run_unit(
                    source,
                    ActTask(native.RouteKey.model_validate(observation.location.model_dump())),
                )
            )
            if mode == "cancel":
                await asyncio.wait_for(pending.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert actions == 2
            else:
                result = await task
                assert isinstance(result, Executed) and result.outcome.observation is None
                assert actions == 3
        diagnostics.close()
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        settled = [event for event in events if event["event"] == "batch_settled"]
        assert len(settled) == 1 and settled[0]["planned"] == 3
        if mode == "cancel":
            assert (
                settled[0]["confirmed"],
                settled[0]["uncertain_step"],
                settled[0]["outcome"],
            ) == (1, 2, "interrupted")
            assert settled[0]["level"] == "ERROR"
        else:
            assert settled[0]["confirmed"] == 3 and settled[0]["outcome"] == "completed"
            assert "uncertain_step" not in settled[0]

    asyncio.run(scenario())


def test_native_recovery_totals_survive_a_second_failure_in_the_same_operation(
    tmp_path: Path,
) -> None:
    import asyncio

    import httpx2 as httpx
    from test_mod_client import RUN

    from sts2_harness.client import ModClient
    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame
    from sts2_harness.journal import Journal, Manifest, RecordedGame

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics("recording", console=console)
        journal = Journal.create(
            tmp_path / "evidence.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="recording",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
            diagnostics=diagnostics,
        )
        observation = native.Observation.model_validate_json(RUN)
        status_reads = 0
        state_reads = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal status_reads, state_reads
            if request.url.path == "/control":
                return httpx.Response(200, json={"mode": "paused"})
            if request.url.path == "/status":
                status_reads += 1
                if status_reads in (1, 3):
                    raise httpx.ReadTimeout("synthetic timeout")
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
                raise httpx.ReadTimeout("synthetic state timeout")
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=observation,
                ).to_json(),
            )

        async def wait(seconds: float) -> None:
            return

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="recording",
                    game_instance_id="game",
                    run_id="run",
                    audit=journal,
                    wait=wait,
                ),
                journal,
            )
            await game.pause_until_resumed(None)
        journal.close()
        diagnostics.close()
        reader = Journal.open(journal.path)
        assert len(reader.operation(1).attempts) == 7
        reader.close()
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        recovered = [
            line
            for line in events
            if line["event"] == "native_recovered" and line["endpoint"] == "status"
        ]
        assert [line["failures"] for line in recovered] == [1, 2]
        assert all(line["event"] != "native_failed" for line in events)

    asyncio.run(scenario())


def test_ordinary_native_request_rejection_remains_info(tmp_path: Path) -> None:
    import asyncio

    import httpx2 as httpx
    from test_mod_client import RUN

    from sts2_harness.client import ModClient
    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame
    from sts2_harness.journal import Journal, Manifest, RecordedGame

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics("recording", console=console)
        journal = Journal.create(
            tmp_path / "evidence.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="recording",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
            diagnostics=diagnostics,
        )

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "status": "rejected",
                    "error": {
                        "code": "invalid_request",
                        "message": "Synthetic invalid action",
                        "fields": ["step"],
                    },
                },
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            game = RecordedGame(
                LiveGame(
                    client,
                    namespace="recording",
                    game_instance_id="game",
                    run_id="run",
                    audit=journal,
                ),
                journal,
            )
            observation = native.Observation.model_validate_json(RUN)
            await game.settle(
                native.RouteKey.model_validate(observation.location.model_dump()),
                native.Interact(kind=native.StepKind.INTERACT, action="travel"),
            )
        journal.close()
        diagnostics.close()
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        assert all(event["level"] == "INFO" for event in events)
        assert events[-1]["result"] == "rejected"
        reader = Journal.open(journal.path)
        assert len(reader.operation(1).attempts) == 1
        reader.close()

    asyncio.run(scenario())


def test_control_transitions_report_observed_start_and_pause_once() -> None:
    from test_mod_client import RUN

    from sts2_harness.client import models as native

    console = StringIO()
    diagnostics = Diagnostics("recording", console=console)
    observation = native.Observation.model_validate_json(RUN)
    for intent in (
        native.ControlIntent.RUNNING,
        native.ControlIntent.PAUSED,
        native.ControlIntent.PAUSED,
    ):
        diagnostics.native_status(
            native.Status(
                location=observation.location,
                control_intent=intent,
                input_readiness=native.InputReadiness.READY,
                receipt=None,
            ),
            False,
        )
    diagnostics.close()
    events = [json.loads(line) for line in console.getvalue().splitlines()]
    assert [(event["intent"], event["level"]) for event in events] == [
        ("running", "INFO"),
        ("paused", "INFO"),
    ]


def test_tool_recovery_exhaustion_is_emitted_once_per_actual_budget() -> None:
    from sts2_harness.diagnostics import RecoveryExhausted
    from sts2_harness.execution.recovery import ToolRecoveryBudget

    console = StringIO()
    diagnostics = Diagnostics("recording", console=console)
    budget = ToolRecoveryBudget(
        on_exhausted=lambda failures: diagnostics.emit(RecoveryExhausted(failures=failures))
    )
    assert [budget.record_failure() for _ in range(5)] == [False, False, False, True, True]
    budget.reset()
    assert [budget.record_failure() for _ in range(4)] == [False, False, False, True]
    diagnostics.close()
    events = [json.loads(line) for line in console.getvalue().splitlines()]
    assert [(event["event"], event["failures"], event["level"]) for event in events] == [
        ("tool_recovery_exhausted", 4, "ERROR"),
        ("tool_recovery_exhausted", 4, "ERROR"),
    ]


@pytest.mark.parametrize("exit_kind", ["stop", "cancel", "unexpected"])
def test_battle_exit_distinguishes_controlled_stop_and_preserves_exception(exit_kind: str) -> None:
    import asyncio

    import httpx2 as httpx
    from test_mod_client import COMBAT

    from sts2_harness.client import ModClient
    from sts2_harness.client import models as native
    from sts2_harness.combat import CombatTask
    from sts2_harness.decisions import Decision, DecisionTask
    from sts2_harness.execution import Execution
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.game import LiveGame, SessionStopped
    from sts2_harness.results import ActionToolResult

    class UnusedRole:
        async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
            raise AssertionError("No decision is allowed before admission.")

        async def accept(self, outcome: ActionToolResult) -> None:
            raise AssertionError("No action was submitted.")

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics("synthetic-stop", console=console)
        original = (
            asyncio.CancelledError()
            if exit_kind == "cancel"
            else RuntimeError("Synthetic failure")
            if exit_kind == "unexpected"
            else None
        )

        def stop_requested() -> bool:
            if original is not None:
                raise original
            return True

        def unused(request: httpx.Request) -> httpx.Response:
            raise AssertionError("No HTTP is allowed before controlled stop.")

        async with ModClient("http://mod", transport=httpx.MockTransport(unused)) as client:
            game = LiveGame(
                client,
                namespace="synthetic-stop",
                game_instance_id="game",
                run_id="run",
                stop_requested=stop_requested,
            )
            task = CombatTask(
                Execution(game, game_instance_id="game", run_id="run"),
                UnusedRole(),
                native.Observation.model_validate_json(COMBAT),
                ToolRecoveryBudget(),
                diagnostics=diagnostics,
            )
            with pytest.raises(
                type(original) if original is not None else SessionStopped
            ) as caught:
                await task.run()
            if original is not None:
                assert caught.value is original
        diagnostics.close()
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        assert [event["event"] for event in events] == ["battle_started", "battle_finished"]
        assert (events[-1]["ending"], events[-1]["level"]) == (
            ("stopped", "INFO") if exit_kind == "stop" else ("interrupted", "ERROR")
        )

    asyncio.run(scenario())


def test_unexpected_external_failure_has_bounded_safe_location_once() -> None:
    import asyncio

    import httpx2 as httpx

    from sts2_harness.client import ModClient
    from sts2_harness.external import ExternalBackend
    from sts2_harness.results import Failed, ReasonCode
    from sts2_harness.tools import ToolName

    async def scenario() -> None:
        console = StringIO()
        diagnostics = Diagnostics("synthetic-external", console=console)

        def unexpected_transport(request: httpx.Request) -> httpx.Response:
            def recurse(depth: int) -> None:
                if depth:
                    recurse(depth - 1)
                else:
                    raise RuntimeError("token=SYNTHETIC_SECRET prompt=SYNTHETIC_PROMPT")

            recurse(20)
            raise AssertionError("Unreachable")

        async with ExternalBackend(
            ModClient("http://mod", transport=httpx.MockTransport(unexpected_transport)),
            diagnostics=diagnostics,
        ) as backend:
            pending = backend.accept(ToolName.RUN_INSPECT, '{"area":"state"}')
            assert isinstance(pending, asyncio.Task)
            reply = await pending
            assert reply.result.outcome.status.value == "failed"
            assert isinstance(reply.result.outcome, Failed)
            assert reply.result.outcome.reason.code is ReasonCode.QUERY_FAILED
        diagnostics.close()
        output = console.getvalue()
        events = [json.loads(line) for line in output.splitlines()]
        assert len(events) == 1
        event = events[0]
        assert event["event"] == "tool_finished" and event["level"] == "ERROR"
        assert event["exception"]["type"] == "RuntimeError"
        frames = event["exception"]["frames"]
        assert len(frames) == 8
        assert frames[-1]["file"] == "test_diagnostics.py"
        assert frames[-1]["function"] == "recurse" and frames[-1]["line"] > 0
        assert set(frames[-1]) == {"file", "function", "line"}
        assert len(output.encode()) < 16 * 1024
        assert "SYNTHETIC_SECRET" not in output and "SYNTHETIC_PROMPT" not in output

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["typed", "bad_sink", "long_type"])
def test_external_failure_details_stay_safe_with_typed_errors_and_sink_failure(
    failure: str,
) -> None:
    import asyncio

    import httpx2 as httpx

    from sts2_harness.client import ModClient
    from sts2_harness.external import ExternalBackend
    from sts2_harness.tools import ToolName

    class BrokenConsole(StringIO):
        def write(self, text: str) -> int:
            raise OSError("Synthetic unavailable sink")

    async def scenario() -> None:
        console = BrokenConsole() if failure == "bad_sink" else StringIO()
        diagnostics = Diagnostics("synthetic-boundary", console=console)
        long_error = type("界" * 5000, (RuntimeError,), {})

        def fail(request: httpx.Request) -> httpx.Response:
            if failure == "typed":
                raise httpx.ReadTimeout("token=SYNTHETIC_SECRET prompt=SYNTHETIC_PROMPT")
            raise long_error("token=SYNTHETIC_SECRET prompt=SYNTHETIC_PROMPT")

        async with ExternalBackend(
            ModClient("http://mod", transport=httpx.MockTransport(fail)), diagnostics=diagnostics
        ) as backend:
            pending = backend.accept(ToolName.RUN_INSPECT, '{"area":"state"}')
            assert isinstance(pending, asyncio.Task)
            reply = await pending
            assert reply.result.outcome.status.value == "failed"
        diagnostics.close()
        output = console.getvalue()
        assert "SYNTHETIC_SECRET" not in output and "SYNTHETIC_PROMPT" not in output
        if failure == "bad_sink":
            assert output == ""
            return
        events = [json.loads(line) for line in output.splitlines()]
        assert len(events) == 1
        if failure == "typed":
            assert events[0]["level"] == "WARNING" and "exception" not in events[0]
            return
        assert len(events[0]["exception"]["type"]) == 2048
        assert len(output.encode()) < 16 * 1024

    asyncio.run(scenario())
