import asyncio
import json
from pathlib import Path

import httpx2 as httpx
from test_interactive import ready_run
from test_mod_client import COMBAT

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.external import ExternalBackend
from sts2_harness.external_http import create_app


class FakeMod:
    def __init__(self) -> None:
        self.observation = ready_run()
        self.control = native.ControlIntent.RUNNING
        self.actions: list[native.StepRequest] = []
        self.pause_after_first = False
        self.state_entered: asyncio.Event | None = None
        self.state_release: asyncio.Event | None = None

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            self.control = native.ControlIntent.PAUSED
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status":
            return httpx.Response(
                200,
                content=native.Status(
                    location=self.observation.location,
                    control_intent=self.control,
                    input_readiness=self.observation.input_readiness,
                    receipt=None,
                ).to_json(),
            )
        if request.url.path == "/action":
            step = native.StepRequest.model_validate_json(request.content)
            self.actions.append(step)
            if self.pause_after_first:
                self.control = native.ControlIntent.PAUSED
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )
        assert request.url.path == "/state"
        if self.state_entered is not None and self.state_release is not None:
            self.state_entered.set()
            await self.state_release.wait()
        return httpx.Response(
            200,
            content=native.StateCompleted(
                status=native.ResultStatus.COMPLETED, observation=self.observation
            ).to_json(),
        )

    def client(self) -> ModClient:
        return ModClient("http://mod", transport=httpx.MockTransport(self.respond))


def test_http_state_reads_are_complete_while_paused_without_model() -> None:
    async def run() -> None:
        mod = FakeMod()
        async with ExternalBackend(mod.client()) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                for _ in range(2):
                    response = await caller.post("/tools/run_inspect", json={"area": "state"})
                    assert response.status_code == 200
                    value = json.loads(response.content)
                    assert value["outcome"] == {"status": "completed"}
                    assert value["state"] == mod.observation.state.model_dump(mode="json")
                    assert value["objects"] == mod.observation.model_dump(mode="json")["objects"]
                    assert (
                        value["descriptions"]
                        == mod.observation.model_dump(mode="json")["descriptions"]
                    )
                    assert {"stamp", "location", "game_language", "updates", "data"}.isdisjoint(
                        value
                    )
                assert mod.control is native.ControlIntent.PAUSED
                assert mod.actions == []

    asyncio.run(run())


def test_accepted_query_survives_waiter_cancellation_and_rejects_second_call() -> None:
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics

    console = StringIO()
    diagnostics = Diagnostics("external", console=console)

    async def run() -> None:
        mod = FakeMod()
        mod.state_entered = asyncio.Event()
        mod.state_release = asyncio.Event()
        async with ExternalBackend(mod.client(), diagnostics=diagnostics) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                first = asyncio.create_task(
                    caller.post("/tools/run_inspect", json={"area": "state"})
                )
                await asyncio.wait_for(mod.state_entered.wait(), 2)
                for name in ("run_inspect", "run_act"):
                    second = await asyncio.wait_for(caller.post("/tools/" + name, json={}), 2)
                    value = json.loads(second.content)
                    assert value["state"] is None
                    assert value["outcome"]["reason"]["code"] == "busy"
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                second = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(second.content)["outcome"]["reason"]["code"] == "busy"
                mod.state_release.set()
        assert mod.actions == []
        lines = [json.loads(line) for line in console.getvalue().splitlines()]
        tools = [line for line in lines if line["event"] == "tool_finished"]
        assert [(line["tool"], line["outcome"]) for line in tools] == [
            ("run_inspect", "rejected"),
            ("run_act", "rejected"),
            ("run_notes", "rejected"),
            ("run_inspect", "completed"),
        ]
        assert all(line["level"] == "INFO" for line in tools)
        diagnostics.close()

    asyncio.run(run())


def test_discovery_exposes_only_grouped_self_contained_tool_definitions() -> None:
    async def run() -> None:
        mod = FakeMod()
        async with ExternalBackend(mod.client()) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                response = await caller.get("/tools")
                assert response.status_code == 200
                value = json.loads(response.content)
                assert set(value) == {"roles"}
                assert set(value["roles"]) == {"run", "combat"}
                definitions = value["roles"]["run"] + value["roles"]["combat"]
                assert {item["name"] for item in definitions} == {
                    "run_act",
                    "run_inspect",
                    "run_knowledge",
                    "run_skill",
                    "run_notes",
                    "combat_act",
                    "combat_inspect",
                    "combat_knowledge",
                    "combat_skill",
                }
                for item in definitions:
                    assert set(item) == {"name", "description", "input_schema"}
                    variants = item["input_schema"].get("oneOf", [item["input_schema"]])
                    assert all(part["type"] == "object" for part in variants)
                missing = await caller.post("/tools/missing", json={})
                assert missing.status_code == 404
                bad_utf = await caller.post("/tools/run_inspect", content=b"\xff")
                assert bad_utf.status_code == 400
        assert mod.control is native.ControlIntent.RUNNING

    asyncio.run(run())


def test_writes_require_exposed_route_start_and_named_role_and_finish_batch() -> None:
    async def run() -> None:
        mod = FakeMod()
        action = {"steps": [{"kind": "interact", "action": "travel"}]}
        async with ExternalBackend(mod.client()) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                first = await caller.post("/tools/run_act", json=action)
                assert json.loads(first.content)["outcome"]["status"] == "rejected"
                assert mod.actions == []
                await caller.post("/tools/run_inspect", json={"area": "state"})
                paused = await caller.post("/tools/run_act", json=action)
                assert (
                    json.loads(paused.content)["outcome"]["reason"]["code"] == "control_unavailable"
                )
                mod.control = native.ControlIntent.RUNNING
                mod.pause_after_first = True
                completed = await caller.post("/tools/run_act", json={"steps": action["steps"] * 2})
                assert json.loads(completed.content)["outcome"] == {
                    "status": "completed",
                    "completed_steps": 2,
                }
                assert len(mod.actions) == 2
                mod.observation = mod.observation.model_copy(
                    update={
                        "location": mod.observation.location.model_copy(
                            update={"node_id": "next-node"}
                        ),
                    }
                )
                mod.control = native.ControlIntent.RUNNING
                stale = await caller.post("/tools/run_act", json=action)
                assert json.loads(stale.content)["outcome"]["reason"]["code"] == "reference_expired"
                wrong = await caller.post(
                    "/tools/combat_act", content='{"steps":[{"kind":"select","refs":[]}]}'
                )
                assert json.loads(wrong.content)["outcome"]["reason"]["code"] == "wrong_role"
                assert len(mod.actions) == 2
                mod.observation = native.Observation.model_validate_json(COMBAT)
                await caller.post("/tools/run_inspect", json={"area": "state"})
                written = await caller.post(
                    "/tools/run_notes", json={"mode": "replace", "text": "Plan"}
                )
                assert json.loads(written.content)["data"] == {"updated": True}
                mod.control = native.ControlIntent.PAUSED
                denied = await caller.post(
                    "/tools/run_notes", json={"mode": "replace", "text": "Wrong"}
                )
                assert json.loads(denied.content)["outcome"]["status"] == "rejected"
                notes = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(notes.content)["data"] == {"text": "Plan"}

    asyncio.run(run())


def test_new_run_never_receives_old_notes_or_old_write() -> None:
    async def run() -> None:
        mod = FakeMod()
        async with ExternalBackend(mod.client()) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                await caller.post("/tools/run_inspect", json={"area": "state"})
                mod.control = native.ControlIntent.RUNNING
                await caller.post("/tools/run_notes", json={"mode": "replace", "text": "Old plan"})
                mod.observation = mod.observation.model_copy(
                    update={
                        "location": mod.observation.location.model_copy(
                            update={"run_id": "next-run"}
                        ),
                    }
                )
                old_write = await caller.post(
                    "/tools/run_notes", json={"mode": "replace", "text": "Stale"}
                )
                assert (
                    json.loads(old_write.content)["outcome"]["reason"]["code"]
                    == "reference_expired"
                )
                notes = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(notes.content)["data"] == {"text": ""}
                read_gate = asyncio.Event()
                release = asyncio.Event()
                mod.state_entered, mod.state_release = read_gate, release
                old_read = asyncio.create_task(
                    caller.post("/tools/run_notes", json={"mode": "read"})
                )
                await asyncio.wait_for(read_gate.wait(), 2)
                mod.observation = mod.observation.model_copy(
                    update={
                        "location": mod.observation.location.model_copy(
                            update={"run_id": "third-run"}
                        ),
                    }
                )
                release.set()
                value = json.loads((await old_read).content)
                assert value["outcome"]["status"] == "rejected"
                assert "data" not in value
                fresh = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(fresh.content)["data"] == {"text": ""}
        assert mod.actions == []

    asyncio.run(run())


def test_external_reads_foreign_public_menus_without_changing_builtin_permissions() -> None:
    async def run() -> None:
        mod = FakeMod()

        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/inspect":
                return await mod.respond(request)
            return httpx.Response(
                200,
                content=native.InspectCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=mod.observation,
                    data=native.DetailData(
                        objects={},
                        descriptions={},
                        modifier_descriptions={},
                        generated={},
                        mechanisms=(),
                    ),
                ).to_json(),
            )

        async with ExternalBackend(
            ModClient("http://mod", transport=httpx.MockTransport(respond))
        ) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                response = await caller.post("/tools/combat_inspect", json={"area": "options"})
                assert json.loads(response.content)["outcome"]["status"] == "completed"
                assert "data" in json.loads(response.content)

    asyncio.run(run())


def test_resident_external_establishes_pause_when_game_appears_without_tool_request() -> None:
    async def run() -> None:
        mod = FakeMod()
        connected = False
        pauses = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal connected, pauses
            if not connected:
                connected = True
                raise httpx.ConnectError("not running", request=request)
            if request.url.path == "/control":
                pauses += 1
            return await mod.respond(request)

        async with ExternalBackend(
            ModClient("http://mod", transport=httpx.MockTransport(respond))
        ) as backend:
            watcher = asyncio.create_task(backend.maintain())
            try:
                async with asyncio.timeout(3):
                    while pauses == 0:
                        await asyncio.sleep(0.01)
                assert mod.control is native.ControlIntent.PAUSED
                assert mod.actions == []
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(run())


def test_tcp_disconnect_does_not_cancel_settlement_or_expose_unsent_route() -> None:
    import socket

    import uvicorn

    from sts2_harness.external_http import LocalServer

    async def run() -> None:
        mod = FakeMod()
        accepted = asyncio.Event()
        settled = asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content)
                mod.actions.append(step)
                accepted.set()
                return httpx.Response(
                    200, json={"operation_id": step.operation_id, "status": "pending"}
                )
            if request.url.path == "/status" and mod.actions:
                operation_id = mod.actions[-1].operation_id
                receipt = (
                    native.ReceiptCompleted(
                        operation_id=operation_id,
                        status=native.ResultStatus.COMPLETED,
                        boundary=native.InputReadiness.READY,
                    )
                    if settled.is_set()
                    else native.ReceiptPending(
                        operation_id=operation_id, status=native.ResultStatus.PENDING
                    )
                )
                return httpx.Response(
                    200,
                    content=native.Status(
                        location=mod.observation.location,
                        control_intent=mod.control,
                        input_readiness=native.InputReadiness.READY
                        if settled.is_set()
                        else native.InputReadiness.BUSY,
                        receipt=receipt,
                    ).to_json(),
                )
            return await mod.respond(request)

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        async with ExternalBackend(
            ModClient("http://mod", transport=httpx.MockTransport(respond))
        ) as backend:
            server = LocalServer(
                uvicorn.Config(
                    create_app(backend),
                    lifespan="off",
                    log_config=None,
                    access_log=False,
                    log_level="critical",
                )
            )
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(4):
                    while not server.started:
                        await asyncio.sleep(0.01)
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False
                ) as caller:
                    await caller.post("/tools/run_inspect", json={"area": "state"})
                    mod.control = native.ControlIntent.RUNNING
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    body = b'{"steps":[{"kind":"interact","action":"travel"}]}'
                    writer.write(
                        b"POST /tools/run_act HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
                    await writer.drain()
                    await asyncio.wait_for(accepted.wait(), 2)
                    writer.close()
                    await writer.wait_closed()
                    busy = await caller.post("/tools/run_inspect", json={"area": "state"})
                    assert json.loads(busy.content)["outcome"]["reason"]["code"] == "busy"
                    assert (await caller.get("/tools")).status_code == 200
                    mod.observation = mod.observation.model_copy(
                        update={
                            "location": mod.observation.location.model_copy(
                                update={"node_id": "after-action"}
                            ),
                        }
                    )
                    settled.set()
                    async with asyncio.timeout(3):
                        while True:
                            response = await caller.post("/tools/run_act", content=body)
                            value = json.loads(response.content)
                            if value["outcome"]["reason"]["code"] != "busy":
                                break
                            await asyncio.sleep(0.01)
                    assert value["outcome"]["reason"]["code"] == "reference_expired"
                    assert len(mod.actions) == 1
                    completed = await caller.post("/tools/run_act", content=body)
                    assert json.loads(completed.content)["outcome"]["status"] == "completed"
                    assert len(mod.actions) == 2
            finally:
                settled.set()
                server.should_exit = True
                await asyncio.wait_for(serving, 4)
                listener.close()

    asyncio.run(run())


def test_cli_external_serves_discovery_without_game_or_model_and_stops(tmp_path: Path) -> None:
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sts2_harness.cli",
            "start",
            "--set",
            "STS2_DRIVER=external",
            "--env-file",
            str(tmp_path / "missing"),
            "--tool-port",
            str(port),
            "--mod-url",
            "http://127.0.0.1:1",
            "--output",
            str(tmp_path / "output"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=0.2, trust_env=False
        ) as caller:
            deadline = time.monotonic() + 4
            while True:
                assert process.poll() is None
                try:
                    response = caller.get("/tools")
                    break
                except httpx.ConnectError:
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
            assert response.status_code == 200
            assert len(json.loads(response.content)["roles"]["run"]) == 5
        status = json.loads((tmp_path / "output" / "status.json").read_text())
        assert status["process_id"] == process.pid
        assert status["driver"] == "external"
        os.kill(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode == 0, stdout + stderr
        status = json.loads((tmp_path / "output" / "status.json").read_text())
        assert status["lifecycle"] == "stopped"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)


def test_shutdown_waits_for_accepted_call_after_http_waiter_is_cancelled() -> None:
    async def run() -> None:
        mod = FakeMod()
        mod.state_entered, mod.state_release = asyncio.Event(), asyncio.Event()
        stop = asyncio.Event()
        available: asyncio.Future[ExternalBackend] = asyncio.get_running_loop().create_future()

        async def own() -> None:
            async with ExternalBackend(mod.client()) as backend:
                available.set_result(backend)
                await stop.wait()

        owner = asyncio.create_task(own())
        backend = await available
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                waiting = asyncio.create_task(
                    caller.post("/tools/run_inspect", json={"area": "state"})
                )
                await asyncio.wait_for(mod.state_entered.wait(), 2)
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                stop.set()
                await asyncio.sleep(0)
                assert not owner.done()
                busy = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(busy.content)["outcome"]["reason"]["code"] == "busy"
        finally:
            mod.state_release.set()
            stop.set()
            await asyncio.wait_for(owner, 2)

    asyncio.run(run())
