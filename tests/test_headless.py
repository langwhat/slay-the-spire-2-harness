import hashlib
import json
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

from sts2_harness.session import RunSession


def template_fixture(directory: Path) -> Path:
    source = directory / "template"
    files: list[dict[str, str]] = []
    for relative in (
        "default/1/settings.save",
        "default/1/modded/profile1/saves/progress.save",
        "default/1/modded/profile1/saves/prefs.save",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
        path.chmod(0o444)
        files.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = directory / "template.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "files": files,
                "game_assembly_sha256": "a" * 64,
                "generator_sha256": "b" * 64,
                "epochs": 1,
                "cards": 1,
                "relics": 1,
                "potions": 1,
                "characters": [
                    f"CHARACTER.{name}"
                    for name in ("IRONCLAD", "SILENT", "REGENT", "NECROBINDER", "DEFECT")
                ],
                "ascension_total": 50,
                "runs": 0,
                "history": 0,
                "has_run": False,
                "has_multiplayer_run": False,
                "tutorials_seen": True,
                "ascension_ftue_seen": True,
            }
        )
    )
    return manifest


def test_each_instance_copies_the_verified_template_without_reusing_progress(
    tmp_path: Path,
) -> None:
    from sts2_harness.native_template import NativeTemplate

    manifest = template_fixture(tmp_path)
    template = NativeTemplate(manifest)
    first = tmp_path / "first"
    second = tmp_path / "second"
    retained = tmp_path / "archive" / "templates"
    evidence = template.copy_to(first, retained)
    progress = Path("default/1/modded/profile1/saves/progress.save")
    (first / progress).write_text("mutated")
    assert template.copy_to(second, retained) == evidence
    assert (second / progress).read_text() == progress.as_posix()
    assert (tmp_path / "template" / progress).read_text() == progress.as_posix()
    assert len(list(retained.iterdir())) == 1
    assert evidence.settings_sha256 == hashlib.sha256(b"default/1/settings.save").hexdigest()
    with pytest.raises(FileExistsError):
        template.copy_to(first, retained)


def test_corrupt_template_is_rejected_before_creating_a_writable_profile(tmp_path: Path) -> None:
    from sts2_harness.native_template import NativeTemplate, TemplateError

    manifest = template_fixture(tmp_path)
    settings = tmp_path / "template/default/1/settings.save"
    settings.chmod(0o644)
    settings.write_text("changed")
    settings.chmod(0o444)
    with pytest.raises(TemplateError):
        NativeTemplate(manifest).copy_to(tmp_path / "instance", tmp_path / "archive")
    assert not (tmp_path / "instance").exists()


def game_fixture(tmp_path: Path) -> tuple[Path, Path]:
    import platform

    from test_interactive import ready_run
    from test_lifecycle import terminal

    observation = ready_run()
    observation = observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "player": observation.state.player.model_copy(
                        update={"definition_id": "CHARACTER.IRONCLAD"}
                    )
                }
            )
        }
    )
    script = """#!/usr/bin/env python3
import http.server, json, os, pathlib, sys
if "--script" in sys.argv:
    override = pathlib.Path(sys.argv[sys.argv.index("--path") + 1], "override.cfg").read_text()
    names = [line.split("=", 1)[1].strip('"') for line in override.splitlines()
             if line.startswith("config/custom_user_dir_name=")]
    name = names[0]
    user_directory = pathlib.Path(os.environ["HOME"], "Library/Application Support", name)
    (user_directory / "logs").mkdir(parents=True)
    (user_directory / "logs/godot.log").write_text("probe startup log")
    print("HARNESS_USER_DATA=" + str(user_directory))
    print("HARNESS_SENTRY_PRELAUNCH_CONFIG_OK")
    print("HARNESS_AUDIO_PRELAUNCH_OK")
    pathlib.Path(sys.argv[sys.argv.index("--path") + 1], "audio/overlay.pck").write_bytes(b"audio")
    sys.exit(0)
initial = json.loads(INITIAL)
ended = json.loads(ENDED)
initial["location"]["game_instance_id"] = str(os.getpid())
initial["stamp"]["game_instance_id"] = str(os.getpid())
ended["location"]["game_instance_id"] = str(os.getpid())
ended["stamp"]["game_instance_id"] = str(os.getpid())
mode = "paused"
state = initial
startup_reads = 0
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        global startup_reads
        busy = startup_reads in (0, 2)
        if self.path == "/status":
            value = {"location": state["location"],
                     "input_readiness": "busy" if busy else state["input_readiness"],
                     "control_intent": mode, "receipt": None, "presentation": None}
        else:
            observation = json.loads(json.dumps(state))
            if busy:
                observation["location"]["node_id"] = "transition-node"
                observation["input_readiness"] = "busy"
                observation["state"]["control"] = "busy"
                observation["state"]["screen"] = "transition"
                observation["state"]["actions"] = []
            value = {"status": "completed", "observation": observation}
            startup_reads += 1
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(value).encode())
    def do_POST(self):
        global mode, state
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/control":
            assert value["mode"] == "running"
            mode = "running"
            with (start.parent / "control.jsonl").open("a") as control_log:
                control_log.write(json.dumps(value) + "\\n")
            result = {"mode": mode}
        elif self.path in ("/map/present", "/map/restore/verify"):
            result = {"operation_id": value["operation_id"], "status": "rejected",
                      "error": {"code": "action_unavailable",
                                "message": "Presentation is unavailable."}}
        else:
            assert self.path == "/action"
            state = ended
            result = {"operation_id": value["operation_id"],
                      "status": "completed", "boundary": "ready"}
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(result).encode())
server = http.server.HTTPServer(("127.0.0.1", int(os.environ["STS2AGENT_PORT"])), Handler)
start = pathlib.Path(os.environ["STS2AGENT_START_RUN_FILE"])
recipe = json.loads(start.read_text())
receipt = {"status":"completed", "effect_verified":True,
           "reason_code":None, "seed":recipe["seed"]}
pathlib.Path(str(start) + ".result.json").write_text(json.dumps(receipt))
server.serve_forever()
"""
    script = script.replace("INITIAL", repr(observation.to_json())).replace(
        "ENDED", repr(terminal(observation).to_json())
    )
    executable = tmp_path / "Game.app/Contents/MacOS/Game"
    executable.parent.mkdir(parents=True)
    executable.write_text(script)
    executable.chmod(0o755)
    resources = executable.parent.parent / "Resources"
    resources.mkdir()
    (resources / "Game.pck").write_bytes(b"pack")
    (resources / "release_info.json").write_text('{"version":"test-version"}')

    assembly = resources / f"data_sts2_macos_{platform.machine()}" / "sts2.dll"
    assembly.parent.mkdir()
    assembly.write_bytes(b"assembly")
    mod = executable.parent / "mods/sts2agent"
    mod.mkdir(parents=True)
    (mod / "sts2agent.dll").write_bytes(b"mod")
    (mod / "mod_manifest.json").write_text('{"version":"test-mod"}')
    template = template_fixture(tmp_path)
    values = json.loads(template.read_text())
    values["game_assembly_sha256"] = hashlib.sha256(b"assembly").hexdigest()
    template.write_text(json.dumps(values))
    return executable, template


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
def test_headless_owns_two_concurrent_children_and_runs_one_session_each(tmp_path: Path) -> None:
    import asyncio

    import httpx2 as httpx
    from test_combat import reply
    from test_responses import configuration

    from sts2_harness.config import Configuration, Driver
    from sts2_harness.diagnostics import Diagnostics
    from sts2_harness.headless import HeadlessOptions, StandardRunRecipe, run_headless
    from sts2_harness.journal import Journal, Manifest, StandardCharacter
    from sts2_harness.live_session import live_session
    from sts2_harness.llm.client import Llm

    executable, template = game_fixture(tmp_path)
    paths: list[Path] = []

    def session(
        manifest: Manifest, archive: Path, diagnostics: Diagnostics
    ) -> AbstractAsyncContextManager[RunSession]:
        from sts2_harness.client import ModClient

        paths.append(archive)
        status = json.loads((archive.parent / "status.json").read_text())
        port = status["mod_port"]

        def provider(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(
                    ("move", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        return live_session(
            ModClient(f"http://127.0.0.1:{port}"),
            manifest,
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)),
            diagnostics=diagnostics,
        )

    async def scenario() -> None:
        outcomes = await asyncio.gather(
            *(
                run_headless(
                    HeadlessOptions(
                        executable,
                        template,
                        tmp_path / "runs",
                        recipe=StandardRunRecipe(
                            character=StandardCharacter.IRONCLAD,
                            ascension=index,
                            seed=f"SEED{index}",
                        ),
                    ),
                    Configuration(Driver.BUILTIN, configuration(tmp_path), minimal=bool(index)),
                    stop_requested=asyncio.Event(),
                    session_factory=session,
                )
                for index in range(2)
            )
        )
        assert all(
            outcome is not None and outcome.reason.value == "terminal" for outcome in outcomes
        )

    asyncio.run(scenario())
    assert len(paths) == 2 and paths[0] != paths[1]
    manifests: list[Manifest] = []
    for archive in paths:
        reader = Journal.open(archive)
        manifests.append(reader.manifest)
        assert reader.gaps == ()
        assert reader.manifest.initial_notes == ""
        assert reader.manifest.headless is not None
        assert reader.manifest.minimal is bool(reader.manifest.headless.requested_ascension)
        assert reader.manifest.headless.initial_control.mode.value == "running"
        opening = json.loads((archive.parent / "initial-observation.json").read_text())
        assert opening["input_readiness"] == "ready"
        bootstrap = [
            json.loads(line)
            for line in (archive.parent / "bootstrap.jsonl").read_text().splitlines()
        ]
        assert bootstrap[0]["reply"]["input_readiness"] == "busy"
        assert bootstrap[1]["reply"]["observation"]["state"]["screen"] == "transition"
        assert [
            attempt["reply"]["observation"]["input_readiness"]
            for attempt in bootstrap
            if attempt["endpoint"] == "state"
        ] == ["busy", "ready", "busy", "ready", "ready"]
        assert len((archive.parent / "control.jsonl").read_text().splitlines()) == 1
        reader.close()
        status = json.loads((archive.parent / "status.json").read_text())
        import configparser

        overrides = configparser.ConfigParser()
        overrides.read(archive.parent / "engine/override.cfg")
        assert overrides["application"]["run/main_scene"] == '"res://harness_audio/game.tscn"'
        assert overrides["Fmod"]["General/auto_initialize"] == "false"
        manager = json.loads(overrides["autoload"]["FmodManager"])
        assert manager == "*" + (archive.parent / "engine/audio/manager.gd").as_posix()
        assert all(
            (archive.parent / f"engine/audio/{name}.gd").is_file()
            for name in ("manager", "audio_proxy", "music_proxy")
        )
        assert status["lifecycle"] == "terminal"
        assert not Path(status["user_data_alias"]).exists()
        assert (
            archive.parent / "probe-user-data/logs/godot.log"
        ).read_text() == "probe startup log"
        import os

        with pytest.raises(ProcessLookupError):
            os.kill(status["game_process_id"], 0)
    assert {manifest.seed for manifest in manifests} == {"SEED0", "SEED1"}
    assert len({manifest.game_instance_id for manifest in manifests}) == 2
    assert len(list((tmp_path / "runs/templates").iterdir())) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
def test_explicit_port_in_time_wait_can_start_an_owned_instance(tmp_path: Path) -> None:
    import asyncio
    import errno
    import socket

    from sts2_harness.headless import HeadlessOptions, OwnedHeadlessInstance, StandardRunRecipe
    from sts2_harness.journal import StandardCharacter

    game, template = game_fixture(tmp_path)
    with socket.socket() as previous:
        previous.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        previous.bind(("127.0.0.1", 0))
        previous.listen()
        address = previous.getsockname()
        with socket.create_connection(address, timeout=2) as client:
            connection, _ = previous.accept()
            connection.close()
            assert client.recv(1) == b""
    with socket.socket() as unavailable:
        with pytest.raises(OSError) as error:
            unavailable.bind(address)
        assert error.value.errno == errno.EADDRINUSE

    async def scenario() -> None:
        async with OwnedHeadlessInstance(
            HeadlessOptions(
                game,
                template,
                tmp_path / "runs",
                tmp_path / "owned",
                address[1],
                recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="TIMEWAIT"),
            )
        ) as instance:
            assert instance.port == address[1]
            assert instance.process is not None and instance.process.returncode is None
            alias = instance.alias
        assert not alias.is_symlink()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
def test_explicit_port_conflict_preserves_the_other_listener_and_cleans_alias(
    tmp_path: Path,
) -> None:
    import asyncio
    import socket

    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    game, template = game_fixture(tmp_path)
    directory = tmp_path / "owned"
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]

        async def scenario() -> None:
            with pytest.raises(HeadlessError, match="The requested Mod port is unavailable"):
                async with OwnedHeadlessInstance(
                    HeadlessOptions(game, template, tmp_path / "runs", directory, port)
                ):
                    pytest.fail("An occupied listener must not be adopted.")

        asyncio.run(scenario())
        assert other.fileno() >= 0
    assert not directory.exists()
    assert not (tmp_path / "runs").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
def test_owned_game_exit_finishes_the_session_at_a_safe_boundary(tmp_path: Path) -> None:
    import asyncio
    import os
    import signal

    import httpx2 as httpx
    from test_combat import reply
    from test_responses import configuration

    from sts2_harness.client import ModClient
    from sts2_harness.config import Configuration, Driver
    from sts2_harness.diagnostics import Diagnostics
    from sts2_harness.headless import (
        HeadlessError,
        HeadlessOptions,
        StandardRunRecipe,
        run_headless,
    )
    from sts2_harness.journal import Journal, Manifest, StandardCharacter
    from sts2_harness.live_session import live_session
    from sts2_harness.llm.client import Llm

    game, template = game_fixture(tmp_path)
    archive_path: Path | None = None

    def session(
        manifest: Manifest, archive: Path, diagnostics: Diagnostics
    ) -> AbstractAsyncContextManager[RunSession]:
        nonlocal archive_path
        archive_path = archive
        status = json.loads((archive.parent / "status.json").read_text())

        async def provider(request: httpx.Request) -> httpx.Response:
            os.kill(status["game_process_id"], signal.SIGKILL)
            await asyncio.sleep(0.1)
            return httpx.Response(
                200,
                content=reply(
                    ("move", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        return live_session(
            ModClient(f"http://127.0.0.1:{status['mod_port']}"),
            manifest,
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)),
            diagnostics=diagnostics,
            stop_requested=stop.is_set,
        )

    stop = asyncio.Event()
    with pytest.raises(HeadlessError):
        asyncio.run(
            run_headless(
                HeadlessOptions(
                    game,
                    template,
                    tmp_path / "runs",
                    recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="EXIT"),
                ),
                Configuration(Driver.BUILTIN, configuration(tmp_path)),
                stop_requested=stop,
                session_factory=session,
            )
        )
    assert archive_path is not None
    archive = Journal.open(archive_path)
    assert archive.gaps == ()
    assert archive.operations()[-1].reply is not None
    archive.close()
    status = json.loads((archive_path.parent / "status.json").read_text())
    assert status["lifecycle"] == "failed"
    assert not Path(status["user_data_alias"]).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
@pytest.mark.parametrize("mismatch_route", [True, False], ids=["route", "character"])
def test_owned_listener_with_mismatched_ready_recipe_is_not_admitted(
    tmp_path: Path, mismatch_route: bool
) -> None:
    import asyncio
    import os

    from sts2_harness.headless import (
        HeadlessError,
        HeadlessOptions,
        OwnedHeadlessInstance,
        StandardRunRecipe,
    )
    from sts2_harness.journal import StandardCharacter

    game, template = game_fixture(tmp_path)
    if mismatch_route:
        game.write_text(
            game.read_text().replace(
                '"location": state["location"],',
                '"location": state["location"] | {"run_id": "different-run"},',
            )
        )
    directory = tmp_path / "instance"

    async def scenario() -> None:
        with pytest.raises(HeadlessError):
            async with OwnedHeadlessInstance(
                HeadlessOptions(
                    game,
                    template,
                    tmp_path / "runs",
                    directory,
                    recipe=StandardRunRecipe(
                        character=(
                            StandardCharacter.IRONCLAD
                            if mismatch_route
                            else StandardCharacter.SILENT
                        ),
                        seed="TEST",
                    ),
                )
            ):
                pytest.fail("Mismatched startup facts must not enter gameplay.")

    asyncio.run(scenario())
    status = json.loads((directory / "status.json").read_text())
    assert status["lifecycle"] == "failed"
    bootstrap = [
        json.loads(line) for line in (directory / "bootstrap.jsonl").read_text().splitlines()
    ]
    assert bootstrap[-2]["reply"]["input_readiness"] == "ready"
    assert bootstrap[-1]["reply"]["observation"]["input_readiness"] == "ready"
    assert not Path(status["user_data_alias"]).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(status["game_process_id"], 0)
    assert not (directory / "archive.sqlite").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
@pytest.mark.parametrize("descriptor", [1, 2])
def test_probe_overflow_reaps_child_even_after_valid_markers(
    tmp_path: Path, descriptor: int
) -> None:
    import asyncio
    import os

    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    game, template = game_fixture(tmp_path)
    game.write_text(
        game.read_text().replace(
            "    sys.exit(0)",
            '    pathlib.Path(os.environ["HOME"], "probe.pid").write_text(str(os.getpid()))\n'
            "    sys.stdout.flush()\n"
            f"    os.write({descriptor}, b'\\xff' * 65537)\n"
            "    import time; time.sleep(30)\n"
            "    sys.exit(0)",
        )
    )
    directory = tmp_path / "instance"

    async def exercise() -> None:
        with pytest.raises(HeadlessError, match="output limit"):
            async with OwnedHeadlessInstance(
                HeadlessOptions(
                    game, template, tmp_path / "runs", directory, startup_timeout_seconds=5
                )
            ):
                pytest.fail("An overflowing probe must not launch the game.")

    asyncio.run(exercise())
    pid = int((directory / "home/probe.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert all(
        (directory / f"probe.{stream}.log").stat().st_size <= 65536
        for stream in ("stdout", "stderr")
    )
    assert not (directory / "game.stdout.log").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
@pytest.mark.parametrize("during_cleanup", [False, True])
def test_cancelling_owned_context_finishes_pipes_and_removes_alias(
    tmp_path: Path, during_cleanup: bool
) -> None:
    import asyncio
    import os

    from sts2_harness.headless import HeadlessOptions, OwnedHeadlessInstance, StandardRunRecipe
    from sts2_harness.journal import StandardCharacter

    game, template = game_fixture(tmp_path)
    game.write_text(
        game.read_text().replace(
            "server = http.server.HTTPServer",
            "import signal, time\n"
            "def stop(signum, frame): time.sleep(0.3); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "data = b'\\xff' * 65536\n"
            "for _ in range(176):\n    os.write(1, data); os.write(2, data)\n"
            "server = http.server.HTTPServer",
        )
    )
    directory = tmp_path / "instance"

    async def exercise() -> tuple[int, Path]:
        entered = asyncio.Event()
        close_requested = asyncio.Event()
        instance = OwnedHeadlessInstance(
            HeadlessOptions(
                game,
                template,
                tmp_path / "runs",
                directory,
                recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="OUTPUT"),
            )
        )

        async def own() -> None:
            async with instance:
                entered.set()
                await close_requested.wait()

        owner = asyncio.create_task(own())
        async with asyncio.timeout(10):
            await entered.wait()
        assert instance.process is not None
        pid, alias = instance.process.pid, instance.alias
        if during_cleanup:
            close_requested.set()
            await asyncio.sleep(0.1)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert asyncio.all_tasks() == {asyncio.current_task()}
        return pid, alias

    pid, alias = asyncio.run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not alias.is_symlink()
    for stream in ("stdout", "stderr"):
        assert (directory / f"game.{stream}.log.1").stat().st_size == 10 * 1024**2
        assert (directory / f"game.{stream}.log").stat().st_size == 1024**2


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
def test_port_retry_reaps_previous_child_and_reuses_output_history(tmp_path: Path) -> None:
    import asyncio
    import os

    from sts2_harness.headless import HeadlessOptions, OwnedHeadlessInstance, StandardRunRecipe
    from sts2_harness.journal import StandardCharacter

    game, template = game_fixture(tmp_path)
    game.write_text(
        game.read_text().replace(
            "server = http.server.HTTPServer",
            "import signal, time\n"
            'attempt = pathlib.Path(os.environ["HOME"], "first-attempt.pid")\n'
            "data = b'\\xff' * 65536\n"
            "for _ in range(176):\n    os.write(1, data); os.write(2, data)\n"
            "if not attempt.exists():\n"
            "    attempt.write_text(str(os.getpid()))\n"
            "    listener = os.fork()\n"
            "    if listener:\n"
            "        def stop(signum, frame):\n"
            "            os.kill(listener, signal.SIGTERM)\n"
            "            os.waitpid(listener, 0)\n"
            "            sys.exit(0)\n"
            "        signal.signal(signal.SIGTERM, stop)\n"
            "        while True: time.sleep(0.01)\n"
            "server = http.server.HTTPServer",
        )
    )
    directory = tmp_path / "instance"

    async def exercise() -> None:
        async with OwnedHeadlessInstance(
            HeadlessOptions(
                game,
                template,
                tmp_path / "runs",
                directory,
                recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="OUTPUT"),
                startup_timeout_seconds=10,
            )
        ) as instance:
            assert instance.process is not None
            previous = int((directory / "home/first-attempt.pid").read_text())
            assert previous != instance.process.pid
            with pytest.raises(ProcessLookupError):
                os.kill(previous, 0)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(exercise())
    for stream in ("stdout", "stderr"):
        assert [
            (directory / f"game.{stream}.log{suffix}").stat().st_size for suffix in (".2", ".1", "")
        ] == [10 * 1024**2, 10 * 1024**2, 2 * 1024**2]


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
@pytest.mark.parametrize("external_cancel", [False, True])
def test_probe_termination_survives_timeout_or_cancellation(
    tmp_path: Path, external_cancel: bool
) -> None:
    import asyncio
    import os

    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    game, template = game_fixture(tmp_path)
    game.write_text(
        game.read_text().replace(
            "    sys.exit(0)",
            "    import signal, time\n"
            "    def ignore(signum, frame):\n"
            '        with pathlib.Path(os.environ["HOME"], "terminated").open("a") as signals:\n'
            '            signals.write("TERM\\n")\n'
            "    signal.signal(signal.SIGTERM, ignore)\n"
            "    sys.stdout.flush()\n"
            "    os.write(2, b'x' * 65537)\n"
            "    time.sleep(60)\n"
            "    sys.exit(0)",
        )
    )
    directory = tmp_path / "instance"

    async def exercise() -> None:
        instance = OwnedHeadlessInstance(
            HeadlessOptions(
                game,
                template,
                tmp_path / "runs",
                directory,
                startup_timeout_seconds=2,
            )
        )

        async def own() -> None:
            async with instance:
                pytest.fail("The overflowing probe must not be admitted.")

        owner = asyncio.create_task(own())
        child: asyncio.subprocess.Process | None = None
        try:
            async with asyncio.timeout(5):
                while not (directory / "home/terminated").exists():
                    await asyncio.sleep(0.01)
            child = instance.process
            assert child is not None
            if external_cancel:
                owner.cancel()
            done, _ = await asyncio.wait((owner,), timeout=13)
            assert owner in done, "Termination lost its KILL/reap budget after cancellation."
            with pytest.raises(asyncio.CancelledError if external_cancel else HeadlessError):
                await owner
            with pytest.raises(ProcessLookupError):
                os.kill(child.pid, 0)
            assert instance.process is None
            assert (directory / "home/terminated").read_text() == "TERM\n"
            assert asyncio.all_tasks() == {asyncio.current_task()}
            assert (directory / "probe.stderr.log").stat().st_size == 65536
        finally:
            # Rescue only this test's known child when the regression is present.
            if child is not None and child.returncode is None:
                child.kill()
                await child.wait()
            await asyncio.gather(owner, return_exceptions=True)
            await instance.aclose()

    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
@pytest.mark.parametrize("missing", ["marker", "pack", "preparation"])
def test_audio_isolation_must_be_prepared_before_game_launch(tmp_path: Path, missing: str) -> None:
    import asyncio

    from sts2_harness.headless import (
        HeadlessError,
        HeadlessOptions,
        OwnedHeadlessInstance,
        StandardRunRecipe,
    )
    from sts2_harness.journal import StandardCharacter

    game, template = game_fixture(tmp_path)
    if missing == "marker":
        game.write_text(game.read_text().replace('print("HARNESS_AUDIO_PRELAUNCH_OK")', "None"))
    elif missing == "pack":
        game.write_text(game.read_text().replace('.write_bytes(b"audio")', ".exists()"))
    else:
        game.write_text(game.read_text().replace("sys.exit(0)", "sys.exit(2)"))
    directory = tmp_path / "instance"

    async def exercise() -> None:
        with pytest.raises(HeadlessError, match="probe"):
            async with OwnedHeadlessInstance(
                HeadlessOptions(
                    game,
                    template,
                    tmp_path / "runs",
                    directory,
                    recipe=StandardRunRecipe(character=StandardCharacter.IRONCLAD, seed="AUDIO"),
                )
            ):
                pytest.fail("A child without verified audio isolation must not enter gameplay.")

    asyncio.run(exercise())
    assert not (directory / "game.stdout.log").exists()
    paths = [
        line.removeprefix("HARNESS_USER_DATA=")
        for line in (directory / "probe.stdout.log").read_text().splitlines()
        if line.startswith("HARNESS_USER_DATA=")
    ]
    assert len(paths) == 1 and not Path(paths[0]).exists()


def test_shutdown_bounds_stalled_output_and_preserves_owned_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    import sts2_harness.headless as headless
    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    monkeypatch.setattr(headless, "_CLEANUP_TIMEOUT_SECONDS", 0.05, raising=False)
    profile = tmp_path / "profile"
    profile.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(profile, target_is_directory=True)

    async def exercise() -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)"
        )
        instance = OwnedHeadlessInstance(HeadlessOptions(tmp_path, tmp_path))
        instance.process = child
        instance.user_directory = profile
        monkeypatch.setattr(instance, "_alias", alias)
        release = asyncio.Event()

        async def stalled_output() -> None:
            await release.wait()

        drain = asyncio.create_task(stalled_output())
        monkeypatch.setattr(instance, "_drains", (drain,))
        close = asyncio.create_task(instance.aclose())
        try:
            done, _ = await asyncio.wait((close,), timeout=1)
            assert close in done, "Shutdown did not bound the stalled output drain."
            with pytest.raises(HeadlessError, match="output"):
                close.result()
            assert child.returncode is not None
            assert alias.is_symlink() and alias.readlink() == profile
            assert profile.is_dir()
            with pytest.raises(HeadlessError, match="output"):
                await instance.aclose()
            assert alias.is_symlink()
        finally:
            release.set()
            await asyncio.gather(close, drain, return_exceptions=True)
            if child.returncode is None:
                child.kill()
                await child.wait()

    asyncio.run(exercise())


def test_shutdown_bounds_unconfirmed_exit_after_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import signal
    import sys

    import sts2_harness.headless as headless
    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    monkeypatch.setattr(headless, "_CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(headless, "_TERMINATE_TIMEOUT_SECONDS", 0.05)
    profile = tmp_path / "profile"
    profile.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(profile, target_is_directory=True)

    async def exercise() -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
        )
        assert child.stdout is not None
        assert await child.stdout.readline() == b"ready\n"
        wait_for_child = child.wait
        release = asyncio.Event()

        async def unconfirmed_exit() -> int:
            await release.wait()
            return await wait_for_child()

        monkeypatch.setattr(child, "wait", unconfirmed_exit)
        instance = OwnedHeadlessInstance(HeadlessOptions(tmp_path, tmp_path))
        instance.process = child
        instance.user_directory = profile
        monkeypatch.setattr(instance, "_alias", alias)
        close = asyncio.create_task(instance.aclose())
        try:
            done, _ = await asyncio.wait((close,), timeout=1)
            assert close in done, "Shutdown did not bound the unconfirmed exit."
            with pytest.raises(HeadlessError, match="exit could not be confirmed"):
                close.result()
            assert await wait_for_child() == -signal.SIGKILL
            assert instance.process is child
            assert alias.is_symlink() and alias.readlink() == profile
            retry = asyncio.create_task(instance.aclose())
            done, _ = await asyncio.wait((retry,), timeout=0.03)
            if retry not in done:
                await asyncio.gather(retry, return_exceptions=True)
            assert retry in done, "Repeated shutdown retried an already unconfirmed exit."
            with pytest.raises(HeadlessError, match="exit could not be confirmed"):
                retry.result()
        finally:
            release.set()
            if child.returncode is None:
                child.kill()
            await wait_for_child()
            await asyncio.gather(close, return_exceptions=True)

    asyncio.run(exercise())


def test_shutdown_refuses_changed_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    profile = tmp_path / "profile"
    other = tmp_path / "other"
    profile.mkdir()
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(other, target_is_directory=True)
    instance = OwnedHeadlessInstance(HeadlessOptions(tmp_path, tmp_path))
    instance.user_directory = profile
    monkeypatch.setattr(instance, "_alias", alias)
    with pytest.raises(HeadlessError, match="alias no longer matches"):
        asyncio.run(instance.aclose())
    assert alias.is_symlink() and alias.readlink() == other
    assert profile.is_dir() and other.is_dir()


def test_repeated_cancellation_waits_for_owned_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sys

    from sts2_harness.headless import HeadlessOptions, OwnedHeadlessInstance

    profile = tmp_path / "profile"
    profile.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(profile, target_is_directory=True)

    async def exercise() -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal, time, sys\n"
            "def stop(signum, frame):\n    time.sleep(0.2); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\nprint('ready', flush=True)\ntime.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
        )
        assert child.stdout is not None
        assert await child.stdout.readline() == b"ready\n"
        instance = OwnedHeadlessInstance(HeadlessOptions(tmp_path, tmp_path))
        instance.process = child
        instance.user_directory = profile
        monkeypatch.setattr(instance, "_alias", alias)
        close = asyncio.create_task(instance.aclose())
        try:
            await asyncio.sleep(0.03)
            close.cancel()
            await asyncio.sleep(0.03)
            close.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close, timeout=2)
            assert child.returncode == 0
            assert instance.process is None
            assert not alias.is_symlink()
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()
            await asyncio.gather(close, return_exceptions=True)

    asyncio.run(exercise())
