import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue
from test_combat import reply
from test_lifecycle import terminal
from test_mod_client import RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.config import Configuration, Driver
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.journal import Journal, Manifest
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.note_values import NotesState, NotesView
from sts2_harness.session import RunSession


def ready_run() -> native.Observation:
    value = json.loads(RUN)
    value["state"]["actions"][0]["enabled"] = True
    value["state"]["actions"][0].pop("reason")
    return native.Observation.model_validate_json(json.dumps(value))


@pytest.mark.parametrize("minimal", [False, True])
def test_resident_process_waits_for_start_and_isolates_new_runs(
    tmp_path: Path, minimal: bool
) -> None:
    from sts2_harness.interactive import InteractiveOptions, run_interactive

    stop = asyncio.Event()
    observation = ready_run()
    control = native.ControlIntent.RUNNING
    connected = False
    no_run = True
    terminal_polls = 0
    waits = 0
    pause_writes = 0
    actions: list[native.StepRequest] = []
    inputs: list[dict[str, JsonValue]] = []
    manifests: list[Manifest] = []
    sessions: list[RunSession] = []
    process_configuration = Configuration(Driver.BUILTIN, configuration(tmp_path), minimal=minimal)

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal connected, control, pause_writes, observation
        if request.url.path == "/control":
            pause_writes += 1
            assert json.loads(request.content) == {"mode": "paused"}
            control = native.ControlIntent.PAUSED
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status":
            if not connected:
                connected = True
                raise httpx.ConnectError("not running", request=request)
            location = (
                observation.location.model_copy(
                    update={"run_id": None, "node_id": None, "owner": None}
                )
                if no_run
                else observation.location
            )
            return httpx.Response(
                200,
                content=native.Status(
                    presentation=None,
                    location=location,
                    control_intent=control,
                    input_readiness=observation.input_readiness,
                    receipt=None,
                ).to_json(),
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=observation,
                ).to_json(),
            )
        assert request.url.path == "/action"
        assert control is native.ControlIntent.RUNNING
        step = native.StepRequest.model_validate_json(request.content)
        actions.append(step)
        if len(actions) == 1:
            control = native.ControlIntent.PAUSED
        else:
            observation = terminal(observation)
        return httpx.Response(
            200,
            json={
                "operation_id": step.operation_id,
                "status": "completed",
                "boundary": "ready",
            },
        )

    def model(request: httpx.Request) -> httpx.Response:
        assert control is native.ControlIntent.RUNNING
        inputs.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=reply(
                (
                    "move",
                    "run_act",
                    '{"steps":[{"kind":"interact","action":"travel"}]}',
                )
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    async def wait(seconds: float) -> None:
        nonlocal no_run, observation, control, terminal_polls, waits
        waits += 1
        assert waits < 20, (
            no_run,
            control,
            pause_writes,
            len(inputs),
            len(actions),
            observation.state.phase,
        )
        if stop.is_set():
            return
        if no_run and pause_writes:
            current_status = json.loads((tmp_path / "resident" / "status.json").read_text())
            assert current_status["control_intent"] == "paused"
            no_run = False
            control = native.ControlIntent.RUNNING
        elif control is native.ControlIntent.PAUSED and not no_run:
            assert seconds == 1.0
            assert len(inputs) == (0 if len(manifests) == 1 and not actions else len(actions))
            control = native.ControlIntent.RUNNING
        elif observation.state.phase is native.Phase.TERMINAL:
            terminal_polls += 1
            if len(manifests) == 1 and terminal_polls == 2:
                observation = ready_run()
                observation = observation.model_copy(
                    update={
                        "location": observation.location.model_copy(update={"run_id": "next-run"}),
                    }
                )
                control = native.ControlIntent.RUNNING
            elif len(manifests) == 2:
                stop.set()

    def client() -> ModClient:
        return ModClient("http://mod", transport=httpx.MockTransport(mod))

    @asynccontextmanager
    async def session(
        manifest: Manifest, path: Path, diagnostics: Diagnostics
    ) -> AsyncGenerator[RunSession]:
        assert process_configuration.model is not None
        manifests.append(manifest)
        async with live_session(
            client(),
            manifest,
            path,
            model=Llm(process_configuration.model, transport=httpx.MockTransport(model)),
            diagnostics=diagnostics,
            stop_requested=stop.is_set,
            wait=wait,
        ) as current:
            sessions.append(current)
            assert current.notes.read().other == "" and current.recovery.failures == 0
            yield current

    asyncio.run(
        run_interactive(
            InteractiveOptions("http://mod", tmp_path / "resident"),
            process_configuration,
            stop_requested=stop,
            client_factory=client,
            session_factory=session,
            wait=wait,
        )
    )
    assert pause_writes == 3
    assert len(inputs) == len(actions) == 3
    assert [manifest.run_id for manifest in manifests] == ["run", "next-run"]
    assert sessions[0].notes is not sessions[1].notes
    assert sessions[0].recovery is not sessions[1].recovery
    archives = sorted((tmp_path / "resident").glob("*/archive.sqlite"))
    assert len(archives) == 2
    for path in archives:
        archive = Journal.open(path)
        assert archive.manifest.initial_notes == ""
        assert archive.manifest.schema_version == 19
        assert archive.manifest.minimal is minimal
        assert archive.operations()[0].request.kind.value == "pause"
        assert archive.gaps == ()
        archive.close()
    status = json.loads((tmp_path / "resident" / "status.json").read_text())
    assert status["lifecycle"] == "stopped"
    assert status["control_endpoint"] == "http://mod/control"
    assert status["run_id"] == "next-run"


def test_disconnect_keeps_session_and_shutdown_settles_an_admitted_unit(tmp_path: Path) -> None:
    from sts2_harness.decisions import (
        ActionDecision,
        ActionKind,
        ActionProposal,
        ActionStep,
        Decision,
        DecisionTask,
    )
    from sts2_harness.execution import Execution, ToolRecoveryBudget
    from sts2_harness.game import OperationKind, Settlement, Stopped
    from sts2_harness.interactive import InteractiveOptions, run_interactive
    from sts2_harness.notes import Notes
    from sts2_harness.results import ActionCompleted, ActionToolResult
    from sts2_harness.run import ActStart

    stop = asyncio.Event()
    observation = ready_run()
    control = native.ControlIntent.RUNNING
    writes = 0
    disconnected_read = False
    reconnect_waits: list[float] = []
    pending = False
    operation_id = ""
    status_polls = 0
    outcomes: list[ActionToolResult] = []
    sessions: list[RunSession] = []
    paths: list[Path] = []

    class CodeRun:
        async def enter_act(self, start: ActStart) -> None:
            assert sessions[0].notes.read().other == "Retain this session."

        async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
            return ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "travel"),)))

        async def accept(self, outcome: ActionToolResult) -> None:
            outcomes.append(outcome)
            assert stop.is_set()
            assert isinstance(outcome.outcome, ActionCompleted)

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal control, writes, disconnected_read, pending, operation_id, status_polls
        if request.url.path == "/control":
            writes += 1
            if writes == 1:
                raise httpx.ConnectError("connection lost", request=request)
            control = native.ControlIntent.PAUSED
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status":
            if writes == 1 and not disconnected_read:
                disconnected_read = True
                raise httpx.ConnectError("still disconnected", request=request)
            receipt = None
            if pending:
                status_polls += 1
                receipt = {"operation_id": operation_id, "status": "pending"}
                if status_polls == 2:
                    receipt = {
                        "operation_id": operation_id,
                        "status": "completed",
                        "boundary": "ready",
                    }
            return httpx.Response(
                200,
                json={
                    "location": observation.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": control.value,
                    "input_readiness": "busy" if pending and status_polls == 1 else "ready",
                    "receipt": receipt,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=observation,
                ).to_json(),
            )
        assert request.url.path == "/action"
        pending = True
        stop.set()
        operation_id = native.StepRequest.model_validate_json(request.content).operation_id
        return httpx.Response(200, json={"operation_id": operation_id, "status": "pending"})

    async def wait(seconds: float) -> None:
        nonlocal control
        if writes == 1:
            reconnect_waits.append(seconds)
            assert seconds == 1.0
            return
        if seconds == 1.0:
            assert control is native.ControlIntent.PAUSED
            control = native.ControlIntent.RUNNING

    def client() -> ModClient:
        return ModClient("http://mod", transport=httpx.MockTransport(mod))

    def run_factory(execution: Execution, notes: Notes, recovery: ToolRecoveryBudget) -> CodeRun:
        return CodeRun()

    def combat_factory(
        execution: Execution,
        recovery: ToolRecoveryBudget,
        opening: native.Observation,
        notes: NotesView,
    ) -> CodeRun:
        return CodeRun()

    @asynccontextmanager
    async def make_session(
        manifest: Manifest, path: Path, diagnostics: Diagnostics
    ) -> AsyncGenerator[RunSession]:
        async with live_session(
            client(),
            manifest,
            path,
            run_factory=run_factory,
            combat_factory=combat_factory,
            diagnostics=diagnostics,
            stop_requested=stop.is_set,
            wait=wait,
        ) as session:
            session.notes.write(NotesState(other="Retain this session."), {"other"})
            sessions.append(session)
            paths.append(path)
            yield session

    asyncio.run(
        run_interactive(
            InteractiveOptions("http://mod", tmp_path / "resident"),
            Configuration(Driver.BUILTIN, configuration(tmp_path)),
            stop_requested=stop,
            client_factory=client,
            session_factory=make_session,
            wait=wait,
        )
    )
    assert len(sessions) == 1 and len(outcomes) == 1
    assert reconnect_waits == [1.0, 1.0]
    assert writes == 2 and disconnected_read and status_polls == 2
    archive = Journal.open(paths[0])
    records = archive.operations()
    assert records[0].request.kind is OperationKind.PAUSE
    assert len(records[0].attempts) == 7
    settled = [record for record in records if record.request.kind is OperationKind.SETTLE]
    assert len(settled) == 1 and settled[0].reply is not None
    assert isinstance(settled[0].reply.outcome, Settlement)
    assert isinstance(settled[0].reply.outcome.receipt, native.ReceiptCompleted)
    assert records[-1].reply is not None and isinstance(records[-1].reply.outcome, Stopped)
    assert archive.gaps == ()
    archive.close()
