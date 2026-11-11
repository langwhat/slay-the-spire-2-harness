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
