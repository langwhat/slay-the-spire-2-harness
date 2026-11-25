import asyncio
import json

import httpx2 as httpx
import pytest
from test_interactive import ready_run
from test_mod_client import COMBAT

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.external import ExternalBackend
from sts2_harness.external_http import create_app
from sts2_harness.note_values import NotesView


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
                    presentation=None,
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
                    assert value["location"] == mod.observation.location.model_dump(mode="json")
                    assert {"stamp", "game_language", "updates", "data"}.isdisjoint(value)
                assert mod.control is native.ControlIntent.PAUSED
                assert mod.actions == []

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
                    "/tools/run_notes", json={"mode": "write", "other": "Plan"}
                )
                assert json.loads(written.content)["data"] == {"updated": True}
                mod.control = native.ControlIntent.PAUSED
                denied = await caller.post(
                    "/tools/run_notes", json={"mode": "write", "other": "Wrong"}
                )
                assert json.loads(denied.content)["outcome"]["status"] == "rejected"
                notes = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(notes.content)["data"] == {
                    "notes": NotesView(other="Plan").model_dump(mode="json")
                }

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
                await caller.post("/tools/run_notes", json={"mode": "write", "other": "Old plan"})
                mod.observation = mod.observation.model_copy(
                    update={
                        "location": mod.observation.location.model_copy(
                            update={"run_id": "next-run"}
                        ),
                    }
                )
                old_write = await caller.post(
                    "/tools/run_notes", json={"mode": "write", "other": "Stale"}
                )
                assert (
                    json.loads(old_write.content)["outcome"]["reason"]["code"]
                    == "reference_expired"
                )
                notes = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(notes.content)["data"] == {
                    "notes": NotesView().model_dump(mode="json")
                }
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
                assert json.loads(fresh.content)["data"] == {
                    "notes": NotesView().model_dump(mode="json")
                }
        assert mod.actions == []

    asyncio.run(run())


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


@pytest.mark.parametrize("unavailable", [False, True])
def test_combined_action_refusal_returns_the_newly_observed_state(unavailable: bool) -> None:
    class NodeChangingMod(FakeMod):
        reads = 0
        change = False

        async def respond(self, request: httpx.Request) -> httpx.Response:
            if self.change and request.url.path == "/state":
                self.reads += 1
                if self.reads == 2:
                    if unavailable:
                        raise httpx.ConnectError("The current state is unavailable.")
                    self.observation = self.observation.model_copy(
                        update={
                            "location": self.observation.location.model_copy(
                                update={"node_id": "new-node"}
                            ),
                            "state": self.observation.state.model_copy(
                                update={
                                    "player": self.observation.state.player.model_copy(
                                        update={"hp": 17}
                                    )
                                }
                            ),
                        }
                    )
            return await super().respond(request)

    async def run() -> None:
        mod = NodeChangingMod()
        async with ExternalBackend(mod.client()) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                await caller.post("/tools/run_inspect", json={"area": "state"})
                mod.control = native.ControlIntent.RUNNING
                mod.change = True
                response = await caller.post(
                    "/tools/run_act",
                    json={
                        "steps": [{"kind": "interact", "action": "travel"}],
                        "notes": {"other": "Updated plan"},
                    },
                )
                value = json.loads(response.content)
                if unavailable:
                    assert value["outcome"]["reason"]["code"] == "busy"
                    assert value["state"] is None
                else:
                    assert value["outcome"]["reason"]["code"] == "role_changed"
                    assert value["state"]["player"]["hp"] == 17
                assert mod.actions == []
                notes = await caller.post("/tools/run_notes", json={"mode": "read"})
                assert json.loads(notes.content)["data"]["notes"]["other"] == ""

    asyncio.run(run())
