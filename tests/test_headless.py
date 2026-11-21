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
                "runs": 1,
                "bosses_seen": ["ENCOUNTER.VANTOM_BOSS"],
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


@pytest.mark.parametrize(
    "changes", [{"runs": 0}, {"runs": True}, {"bosses_seen": None}, {"bosses_seen": []}]
)
def test_template_requires_veteran_progress(tmp_path: Path, changes: dict[str, object]) -> None:
    from sts2_harness.native_template import NativeTemplate, TemplateError

    manifest = template_fixture(tmp_path)
    values = json.loads(manifest.read_text())
    for field, value in changes.items():
        if value is None:
            del values[field]
        else:
            values[field] = value
    manifest.write_text(json.dumps(values))

    with pytest.raises(TemplateError, match="Regenerate"):
        NativeTemplate(manifest)


def game_fixture(
    tmp_path: Path, *, actual_ascension: int | None = None, actual_seed: str | None = None
) -> tuple[Path, Path]:
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
initial["run_rules"]["ascension"] = ACTUAL_ASCENSION
receipt = {"status":"completed", "effect_verified":True,
           "reason_code":None, "seed":ACTUAL_SEED}
pathlib.Path(str(start) + ".result.json").write_text(json.dumps(receipt))
server.serve_forever()
"""
    script = script.replace("INITIAL", repr(observation.to_json())).replace(
        "ENDED", repr(terminal(observation).to_json())
    )
    script = script.replace(
        "ACTUAL_ASCENSION",
        "recipe['ascension']" if actual_ascension is None else repr(actual_ascension),
    ).replace(
        "ACTUAL_SEED",
        "(recipe['seed'] or 'RANDOM').replace('I', '1').replace('O', '0')"
        if actual_seed is None
        else repr(actual_seed),
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
def test_headless_start_reports_that_an_old_template_must_be_regenerated(tmp_path: Path) -> None:
    import asyncio

    from sts2_harness.headless import HeadlessError, HeadlessOptions, OwnedHeadlessInstance

    game, manifest = game_fixture(tmp_path)
    values = json.loads(manifest.read_text())
    values["runs"] = 0
    del values["bosses_seen"]
    manifest.write_text(json.dumps(values))
    instance = tmp_path / "instance"

    async def start() -> None:
        options = HeadlessOptions(
            game=game, template_manifest=manifest, instance_directory=instance
        )
        async with OwnedHeadlessInstance(options):
            pytest.fail("The old template was accepted.")

    with pytest.raises(HeadlessError, match="Regenerate"):
        asyncio.run(start())
    assert not instance.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="The native launcher targets macOS.")
@pytest.mark.parametrize(
    ("requested_seed", "ascension", "seed", "matches"),
    [
        ("FOOLI", 10, "F00L1", True),
        ("FOOLI", 9, "F00L1", False),
        ("FOOLI", 10, "F00L2", False),
        ("straßei", 10, "STRAßE1", True),
        ("\x1cfoi\x1c", 10, "\x1cF01\x1c", True),
    ],
)
def test_headless_ready_requires_the_requested_ascension_and_canonical_seed(
    tmp_path: Path, requested_seed: str, ascension: int, seed: str, matches: bool
) -> None:
    import asyncio

    from sts2_harness.headless import (
        HeadlessError,
        HeadlessOptions,
        OwnedHeadlessInstance,
        StandardRunRecipe,
    )
    from sts2_harness.journal import StandardCharacter

    executable, template = game_fixture(tmp_path, actual_ascension=ascension, actual_seed=seed)
    options = HeadlessOptions(
        executable,
        template,
        tmp_path / "runs",
        recipe=StandardRunRecipe(
            character=StandardCharacter.IRONCLAD, ascension=10, seed=requested_seed
        ),
    )

    async def launch() -> None:
        async with OwnedHeadlessInstance(options) as instance:
            assert instance.receipt is not None and instance.receipt.seed == seed

    if matches:
        asyncio.run(launch())
    else:
        with pytest.raises(HeadlessError, match="did not match the paused run recipe"):
            asyncio.run(launch())


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
