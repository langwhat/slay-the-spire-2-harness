import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_headless import game_fixture
from test_interactive import ready_run
from test_lifecycle import terminal
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.config import Configuration, Driver
from sts2_harness.entry_materials import EntryMaterials
from sts2_harness.headless import HeadlessOptions, StandardRunRecipe, run_headless
from sts2_harness.interactive import InteractiveOptions, run_interactive
from sts2_harness.journal import Journal, SessionMaterials, StandardCharacter
from sts2_harness.materials import DocumentSource, FileDocuments


@pytest.mark.parametrize(
    "headless",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.platform != "darwin", reason="The native launcher targets macOS."
            ),
        ),
    ],
)
def test_startup_records_supplied_material_settings_and_serves_documents(
    tmp_path: Path,
    headless: bool,
) -> None:
    path = tmp_path / "guide.txt"
    path.write_text("Applies to: CARD.TEST\nComplete synthetic rules.", encoding="utf-8")
    settings = SessionMaterials(
        entry_documents_enabled=False, act_guidance={1: ("guide",)}, battle_guidance=("guide",)
    )
    materials = EntryMaterials(
        FileDocuments((DocumentSource("guide", "Guide", "Rules", path),)), settings
    )
    inputs: list[bytes] = []
    replies = iter(
        (
            reply(("read", "run_skill", '{"mode":"read","id":"guide"}')),
            reply(("move", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
        )
    )

    class ModelServer(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_POST(self) -> None:
            inputs.append(self.rfile.read(int(self.headers["Content-Length"])))
            body = next(replies)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelServer)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    model = configuration(tmp_path, LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}")
    config = Configuration(Driver.BUILTIN, model)
    stop = asyncio.Event()
    observation = ready_run()

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal observation
        if request.url.path == "/status":
            return httpx.Response(
                200,
                content=native.Status(
                    location=observation.location,
                    input_readiness=observation.input_readiness,
                    control_intent=native.ControlIntent.RUNNING,
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
        if request.url.path == "/control":
            return httpx.Response(200, json={"mode": "paused"})
        assert request.url.path == "/action"
        step = native.StepRequest.model_validate_json(request.content)
        observation = terminal(observation)
        return httpx.Response(
            200,
            json={
                "operation_id": step.operation_id,
                "status": "completed",
                "boundary": "ready",
            },
        )

    async def wait(seconds: float) -> None:
        stop.set()

    try:
        if headless:
            executable, template = game_fixture(tmp_path)
            asyncio.run(
                run_headless(
                    HeadlessOptions(
                        executable,
                        template,
                        tmp_path / "runs",
                        recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="TEST"),
                    ),
                    config,
                    stop_requested=stop,
                    materials=materials,
                )
            )
        else:
            asyncio.run(
                run_interactive(
                    InteractiveOptions("http://mod", tmp_path / "runs"),
                    config,
                    stop_requested=stop,
                    materials=materials,
                    wait=wait,
                    client_factory=lambda: ModClient(
                        "http://mod", transport=httpx.MockTransport(mod)
                    ),
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    assert len(inputs) == 2
    result = next(
        item
        for item in json.loads(inputs[1])["input"]
        if item.get("type") == "function_call_output"
    )
    assert json.loads(result["output"])["data"] == {
        "id": "guide",
        "name": "Guide",
        "text": path.read_text(),
        "resources": [],
    }
    archives = list((tmp_path / "runs").glob("*/archive.sqlite"))
    assert len(archives) == 1
    journal = Journal.open(archives[0])
    try:
        assert journal.manifest.materials == settings
        assert journal.gaps == ()
    finally:
        journal.close()
