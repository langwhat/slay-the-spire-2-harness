import asyncio

import httpx2 as httpx
import pytest

from sts2_harness.client import ModClient


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


@pytest.mark.parametrize("automatic", [False, True])
def test_uncertain_presentation_blocks_gameplay_until_same_operation_is_restored(
    automatic: bool,
) -> None:
    from test_auto import shop

    from sts2_harness.auto import choose_auto
    from sts2_harness.client import models as native
    from sts2_harness.decisions import ActionStep
    from sts2_harness.game import LiveGame, Presented, ReadFailure, Settlement

    calls: list[str] = []
    operation_id = ""
    state = "unknown"

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal operation_id, state
        calls.append(request.url.path)
        if request.url.path == "/map/present":
            operation_id = native.PresentRequest.model_validate_json(request.content).operation_id
            raise httpx.ReadTimeout("disconnected", request=request)
        if request.url.path == "/map/restore/verify":
            verification = native.VerifyPresentationRequest.model_validate_json(request.content)
            assert verification.operation_id == operation_id
            state = "restored"
            return httpx.Response(
                200,
                json={
                    "operation_id": operation_id,
                    "status": "completed",
                    "restoration": "restored",
                    "gate": "open",
                },
            )
        if request.url.path == "/status":
            presentation = None
            if state != "unknown":
                presentation = {
                    "operation_id": "other" if state == "other" else operation_id,
                    "phase": "restored" if state in ("restored", "other") else "settled_unverified",
                    "drawing": "shown",
                    "restoration": "restored" if state in ("restored", "other") else "unknown",
                    "gate": "open" if state in ("restored", "other") else "locked",
                }
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
                    "input_readiness": "unavailable" if state == "locked" else "ready",
                    "receipt": None,
                    "presentation": presentation,
                },
            )
        assert request.url.path == "/action"
        submission = native.StepRequest.model_validate_json(request.content)
        return httpx.Response(
            200,
            json={
                "operation_id": submission.operation_id,
                "status": "rejected",
                "error": {"code": "action_unavailable", "message": "Unavailable."},
            },
        )

    async def run() -> None:
        nonlocal state
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            game = LiveGame(client, namespace="test", game_instance_id="game", run_id="run")
            route = native.RouteKey(
                game_instance_id="game",
                run_id="run",
                node_id="node",
                owner=native.DecisionOwner.RUN,
            )
            edges = (native.PresentationEdge.model_validate({"from": "map:0:0", "to": "map:1:2"}),)
            first = await game.present(route, edges)
            assert isinstance(first.outcome, Presented) and first.outcome.receipt is None
            action = "manual-action"
            if automatic:
                auto = choose_auto(shop(opened=False, potion=False), shop_strategy=False)
                assert auto is not None
                auto_step = auto[0].steps[0]
                assert isinstance(auto_step, ActionStep)
                action = auto_step.ref
                assert action is not None
            step = native.Interact(kind=native.StepKind.INTERACT, action=action)
            blocked = await game.settle(route, step)
            assert isinstance(blocked.outcome, ReadFailure)
            assert "/action" not in calls
            state = "other"
            mismatched = await game.settle(route, step)
            assert isinstance(mismatched.outcome, ReadFailure)
            assert "/action" not in calls
            state = "locked"
            restored = await game.settle(route, step)
            assert isinstance(restored.outcome, Settlement)
            assert calls.count("/map/present") == 1
            assert calls.count("/map/restore/verify") == 1
            assert calls.count("/action") == 1

    asyncio.run(run())


def test_explicit_restart_retries_restoration_once_per_pause() -> None:
    from test_mod_client import RUN

    from sts2_harness.client import models as native
    from sts2_harness.game import LiveGame, ReadFailure, Resumed

    observation = native.Observation.model_validate_json(RUN)
    calls: list[str] = []
    verifications = 0
    pauses = 0
    status_after_pause = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal verifications, pauses, status_after_pause
        calls.append(request.url.path)
        if request.url.path == "/control":
            assert (
                native.ControlRequest.model_validate_json(request.content).mode
                is native.ControlIntent.PAUSED
            )
            pauses += 1
            status_after_pause = 0
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/map/restore/verify":
            body = native.VerifyPresentationRequest.model_validate_json(request.content)
            assert body.operation_id == "display"
            verifications += 1
            restored = verifications == 3
            return httpx.Response(
                200,
                json={
                    "operation_id": "display",
                    "status": "completed",
                    "restoration": "restored" if restored else "unknown",
                    "gate": "open" if restored else "locked",
                },
            )
        if request.url.path == "/status":
            status_after_pause += 1
            restored = verifications == 3
            return httpx.Response(
                200,
                json={
                    "location": observation.location.model_dump(mode="json"),
                    "control_intent": "paused" if pauses and status_after_pause == 1 else "running",
                    "input_readiness": "ready" if restored else "unavailable",
                    "receipt": None,
                    "presentation": {
                        "operation_id": "display",
                        "phase": "restored" if restored else "settled_unverified",
                        "drawing": "shown",
                        "restoration": "restored" if restored else "unknown",
                        "gate": "open" if restored else "locked",
                    },
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

            async def wait(_: float) -> None:
                return None

            game = LiveGame(
                client, namespace="test", game_instance_id="game", run_id="run", wait=wait
            )
            assert isinstance((await game.admit()).outcome, ReadFailure)
            assert isinstance((await game.admit()).outcome, ReadFailure)
            assert verifications == 1
            resumed = await game.pause_until_resumed(None)
            assert isinstance(resumed.outcome, Resumed)
            assert resumed.outcome.observation == observation
        assert verifications == 3
        assert pauses == 2
        assert calls == [
            "/status",
            "/map/restore/verify",
            "/status",
            "/status",
            "/control",
            "/status",
            "/status",
            "/map/restore/verify",
            "/status",
            "/control",
            "/status",
            "/status",
            "/map/restore/verify",
            "/status",
            "/state",
        ]

    asyncio.run(run())
