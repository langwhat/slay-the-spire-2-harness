import asyncio

import httpx2 as httpx
import pytest

from sts2_harness.client import ClientFailureKind, ClientUnavailable, ModClient
from sts2_harness.client.models import Status


def test_status_accepts_a_running_presentation_and_rejects_inconsistent_gate() -> None:
    payload = {
        "location": {
            "game_instance_id": "game",
            "run_id": "run",
            "node_id": "node",
            "owner": "run",
        },
        "control_intent": "running",
        "input_readiness": "busy",
        "receipt": None,
        "presentation": {
            "operation_id": "display",
            "phase": "running",
            "drawing": "pending",
            "restoration": "pending",
            "gate": "locked",
        },
    }

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client:
            assert isinstance(await client.status(), Status)
            payload["input_readiness"] = "ready"
            with pytest.raises(ClientUnavailable) as error:
                await client.status()
            assert error.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())


@pytest.mark.parametrize(
    ("phase", "drawing", "restoration", "gate", "readiness", "valid"),
    [
        ("running", "shown", "pending", "locked", "busy", True),
        ("running", "pending", "unknown", "locked", "busy", False),
        ("settled_unverified", "shown", "unknown", "locked", "unavailable", True),
        ("settled_unverified", "pending", "unknown", "locked", "unavailable", False),
        ("verifying", "failed", "pending", "locked", "busy", True),
        ("restored", "unknown", "restored", "open", "selection", True),
        ("restored", "shown", "unknown", "open", "ready", False),
        ("restored", "shown", "restored", "open", "terminal", False),
    ],
)
def test_status_rejects_illegal_presentation_combinations(
    phase: str, drawing: str, restoration: str, gate: str, readiness: str, valid: bool
) -> None:
    payload = {
        "location": {
            "game_instance_id": "game",
            "run_id": "run",
            "node_id": "node",
            "owner": "run",
        },
        "control_intent": "running",
        "input_readiness": readiness,
        "receipt": None,
        "presentation": {
            "operation_id": "display",
            "phase": phase,
            "drawing": drawing,
            "restoration": restoration,
            "gate": gate,
        },
    }

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client:
            if valid:
                assert isinstance(await client.status(), Status)
            else:
                with pytest.raises(ClientUnavailable):
                    await client.status()

    asyncio.run(read())


def test_present_is_submitted_once_and_retains_a_domain_rejection() -> None:
    from sts2_harness.client import models as native

    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "operation_id": "display",
                "status": "rejected",
                "error": {"code": "action_unavailable", "message": "Surface unavailable."},
            },
        )

    async def run() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            result = await client.present(
                native.PresentRequest(
                    operation_id="display",
                    expected_route=native.RouteKey(
                        game_instance_id="game",
                        run_id="run",
                        node_id="node",
                        owner=native.DecisionOwner.RUN,
                    ),
                    edges=(
                        native.PresentationEdge.model_validate_json(
                            '{"from":"map:0:0","to":"map:1:2"}'
                        ),
                    ),
                )
            )
        assert isinstance(result, native.PresentationRejected)
        assert result.error.code is native.ErrorCode.ACTION_UNAVAILABLE
        assert len(calls) == 1
        assert calls[0].url.path == "/map/present"
        assert b'"from":"map:0:0"' in calls[0].content

    asyncio.run(run())


def test_restore_verification_accepts_only_consistent_gate_outcomes() -> None:
    from sts2_harness.client import models as native

    payload = {
        "operation_id": "display",
        "status": "completed",
        "restoration": "unknown",
        "gate": "locked",
    }
    route = native.RouteKey(
        game_instance_id="game", run_id="run", node_id="node", owner=native.DecisionOwner.RUN
    )
    request = native.VerifyPresentationRequest(operation_id="display", expected_route=route)

    async def run() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        ) as client:
            reply = await client.verify_presentation(request)
            assert isinstance(reply, native.PresentationVerificationCompleted)
            payload["gate"] = "open"
            with pytest.raises(ClientUnavailable):
                await client.verify_presentation(request)

    asyncio.run(run())


@pytest.mark.parametrize("restoration", ["restored", "unknown"])
def test_game_admission_verifies_a_settled_presentation_once(restoration: str) -> None:
    from test_mod_client import RUN

    from sts2_harness.client import models as native
    from sts2_harness.game import Admitted, LiveGame, ReadFailure

    observation = native.Observation.model_validate_json(RUN)
    calls: list[str] = []
    attempted = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempted
        calls.append(request.url.path)
        if request.url.path == "/status":
            restored = attempted and restoration == "restored"
            return httpx.Response(
                200,
                json={
                    "location": observation.location.model_dump(mode="json"),
                    "control_intent": "running",
                    "receipt": None,
                    "input_readiness": "ready" if restored else "unavailable",
                    "presentation": {
                        "operation_id": "display",
                        "phase": "restored" if restored else "settled_unverified",
                        "drawing": "shown",
                        "restoration": "restored" if restored else "unknown",
                        "gate": "open" if restored else "locked",
                    },
                },
            )
        if request.url.path == "/map/restore/verify":
            attempted = True
            return httpx.Response(
                200,
                json={
                    "operation_id": "display",
                    "status": "completed",
                    "restoration": restoration,
                    "gate": "open" if restoration == "restored" else "locked",
                },
            )
        assert request.url.path == "/state"
        return httpx.Response(
            200,
            content=native.StateCompleted(
                status=native.ResultStatus.COMPLETED, observation=observation
            ).to_json(),
        )

    async def run() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            game = LiveGame(client, namespace="test", game_instance_id="game", run_id="run")
            result = await game.admit()
            if restoration == "restored":
                assert isinstance(result.outcome, Admitted) and result.outcome.allowed
            else:
                assert isinstance(result.outcome, ReadFailure)
                assert isinstance((await game.admit()).outcome, ReadFailure)
        assert calls.count("/map/restore/verify") == 1
        assert ("/state" in calls) == (restoration == "restored")
        assert "/action" not in calls

    asyncio.run(run())


def test_presented_plan_keeps_entry_rejection_separate_and_never_retries() -> None:
    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame, Presented

    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/map/present"
        import json

        operation_id = json.loads(request.content)["operation_id"]
        return httpx.Response(
            200,
            json={
                "operation_id": operation_id,
                "status": "rejected",
                "error": {"code": "action_unavailable", "message": "Presentation unavailable."},
            },
        )

    async def run() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            game = LiveGame(client, namespace="test", game_instance_id="game", run_id="run")
            route = native.RouteKey(
                game_instance_id="game",
                run_id="run",
                node_id="node",
                owner=native.DecisionOwner.RUN,
            )
            edges = (
                native.PresentationEdge.model_validate_json('{"from":"map:0:0","to":"map:1:2"}'),
            )
            result = await game.present(route, edges)
            assert isinstance(result.outcome, Presented)
            assert isinstance(result.outcome.receipt, native.PresentationRejected)
        assert calls == ["/map/present"]

    asyncio.run(run())


def test_lost_presentation_response_uses_matching_status_without_replaying() -> None:
    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame, Presented

    calls: list[str] = []
    operation_id = ""

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal operation_id
        calls.append(request.url.path)
        if request.url.path == "/map/present":
            operation_id = native.PresentRequest.model_validate_json(request.content).operation_id
            raise httpx.ReadTimeout("disconnected", request=request)
        assert request.url.path == "/status"
        return httpx.Response(
            200,
            json={
                "location": {
                    "game_instance_id": "game",
                    "run_id": "run",
                    "node_id": "node",
                    "owner": "run",
                },
                "control_intent": "running",
                "input_readiness": "selection",
                "receipt": None,
                "presentation": {
                    "operation_id": operation_id,
                    "phase": "restored",
                    "drawing": "shown",
                    "restoration": "restored",
                    "gate": "open",
                },
            },
        )

    async def run() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            game = LiveGame(client, namespace="test", game_instance_id="game", run_id="run")
            route = native.RouteKey(
                game_instance_id="game",
                run_id="run",
                node_id="node",
                owner=native.DecisionOwner.RUN,
            )
            edges = (
                native.PresentationEdge.model_validate_json('{"from":"map:0:0","to":"map:1:2"}'),
            )
            result = await game.present(route, edges)
            assert isinstance(result.outcome, Presented)
            receipt = result.outcome.receipt
            assert isinstance(receipt, native.PresentationCompleted)
            assert receipt.drawing is native.PresentationDrawing.SHOWN
        assert calls == ["/map/present", "/status"]

    asyncio.run(run())


def test_presentation_completion_must_match_the_current_status() -> None:
    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame, Presented

    calls: list[str] = []
    operation_id = ""

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal operation_id
        calls.append(request.url.path)
        if request.url.path == "/map/present":
            operation_id = native.PresentRequest.model_validate_json(request.content).operation_id
            return httpx.Response(
                200,
                json={
                    "operation_id": operation_id,
                    "status": "completed",
                    "drawing": "shown",
                    "restoration": "restored",
                    "gate": "open",
                },
            )
        assert request.url.path == "/status"
        return httpx.Response(
            200,
            json={
                "location": {
                    "game_instance_id": "game",
                    "run_id": "run",
                    "node_id": "node",
                    "owner": "run",
                },
                "control_intent": "running",
                "input_readiness": "unavailable",
                "receipt": None,
                "presentation": {
                    "operation_id": operation_id,
                    "phase": "settled_unverified",
                    "drawing": "shown",
                    "restoration": "unknown",
                    "gate": "locked",
                },
            },
        )

    async def run() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            game = LiveGame(client, namespace="test", game_instance_id="game", run_id="run")
            route = native.RouteKey(
                game_instance_id="game",
                run_id="run",
                node_id="node",
                owner=native.DecisionOwner.RUN,
            )
            edges = (
                native.PresentationEdge.model_validate_json('{"from":"map:0:0","to":"map:1:2"}'),
            )
            result = await game.present(route, edges)
            assert isinstance(result.outcome, Presented) and result.outcome.receipt is None
            repeated = await game.present(route, edges)
            assert isinstance(repeated.outcome, Presented) and repeated.outcome.receipt is None
        assert calls == ["/map/present", "/status"]

    asyncio.run(run())
