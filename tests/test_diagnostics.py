import json
from io import StringIO
from pathlib import Path

import pytest

from sts2_harness.diagnostics import Diagnostics, Event, EventKind
from sts2_harness.journal import SessionMaterials


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
