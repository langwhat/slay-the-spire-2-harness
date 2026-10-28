import asyncio
import json
from pathlib import Path

import httpx2 as httpx

from sts2_harness_eval.checkpoints import CheckpointClient, CheckpointUncertain, ReloadTarget


def test_unknown_checkpoint_never_resends_the_mutation(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        value: dict[str, object] = json.loads(request.content)
        calls.append(value)
        return httpx.Response(
            200,
            json={
                "status": "unknown",
                "command_id": value["command_id"],
                "native_dispatched": True,
                "error": {"code": "execution_uncertain", "message": "uncertain"},
            },
        )

    async def run() -> None:
        async with CheckpointClient(
            "http://fake", transport=httpx.MockTransport(respond)
        ) as client:
            try:
                await client.load(
                    tmp_path / "current_run.save",
                    "a" * 64,
                    ReloadTarget.model_validate(
                        {
                            "canonical_seed": "seed",
                            "game_mode": "standard",
                            "character": "CHARACTER.SILENT",
                            "ascension": 0,
                            "act": 1,
                            "floor": 0,
                        }
                    ),
                )
            except CheckpointUncertain:
                pass
            else:
                raise AssertionError("Unknown native work cannot become a loaded checkpoint.")

    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0]["kind"] == "load"


def test_capture_polls_only_the_original_command_until_its_proof(tmp_path: Path) -> None:
    from sts2_harness.client import models as native

    calls: list[dict[str, object]] = []
    route = native.RouteKey(
        game_instance_id="game", run_id="run", node_id="map", owner=native.DecisionOwner.RUN
    )

    def respond(request: httpx.Request) -> httpx.Response:
        value: dict[str, object] = json.loads(request.content)
        calls.append(value)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "status": "pending",
                    "command_id": value["command_id"],
                    "native_dispatched": True,
                },
            )
        assert value == {"kind": "status", "command_id": calls[0]["command_id"]}
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "command_id": value["command_id"],
                "proof": {
                    "kind": "capture",
                    "state": {
                        "route": route.model_dump(mode="json"),
                        "native_run_instance_id": 1,
                        "native_revision": 2,
                        "target": {
                            "canonical_seed": "SEED",
                            "game_mode": "standard",
                            "character": "IRONCLAD",
                            "ascension": 0,
                            "act": 1,
                            "floor": 0,
                        },
                    },
                    "file": {
                        "path": str(tmp_path / "current_run.save"),
                        "sha256": "a" * 64,
                        "bytes": 1,
                    },
                },
            },
        )

    async def run() -> None:
        async with CheckpointClient(
            "http://fake", transport=httpx.MockTransport(respond)
        ) as client:
            proof = await client.capture(route, tmp_path / "new")
            assert proof.state.route == route

    asyncio.run(run())
    assert [call["kind"] for call in calls] == ["capture", "status"]
