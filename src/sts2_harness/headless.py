"""Own one isolated native process and one run, without a resident gameplay loop."""

import asyncio
import hashlib
import os
import platform
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.child_output import BoundedChildOutput, ChildStream, capture_probe
from sts2_harness.client import ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import ClientFailureKind, ControlResult, StateResult, StatusResult
from sts2_harness.config import Configuration, ConfigurationError, Driver
from sts2_harness.diagnostics import Diagnostics, Lifecycle
from sts2_harness.entry_materials import EntryMaterials, MaterialContext
from sts2_harness.game import Endpoint, RunEnded, RunEndReason, SessionStopped
from sts2_harness.headless_audio import configure_audio
from sts2_harness.journal import (
    HeadlessInitialConditions,
    Manifest,
    NativeStartReceipt,
    SessionMaterials,
    StandardCharacter,
    current_core_identity,
)
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelUnavailable
from sts2_harness.native_template import NativeTemplate, TemplateError, digest
from sts2_harness.session import RunSession
from sts2_harness.user_input_values import UserInputSource


class HeadlessError(Exception):
    """An isolated launch could not be verified; no other game may be adopted."""


class StandardRunRecipe(native.WireModel):
    character: StandardCharacter = StandardCharacter.SILENT
    ascension: Annotated[int, Field(ge=0, le=10)] = 0
    seed: native.Nonempty | None = None


@dataclass(frozen=True)
class HeadlessOptions:
    game: Path
    template_manifest: Path
    output: Path = Path("runs")
    instance_directory: Path | None = None
    port: int | None = None
    recipe: StandardRunRecipe = StandardRunRecipe()
    startup_timeout_seconds: float = 90.0


class BootstrapAttempt(native.WireModel):
    endpoint: Endpoint
    reply: StatusResult | StateResult | ControlResult | None
    error: ClientFailureKind | None


class _Version(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    version: native.Nonempty


_PROBE = """extends SceneTree
func _initialize():
    print("HARNESS_USER_DATA=" + OS.get_user_data_dir())
    var dsn = ProjectSettings.get_setting("sentry/config/dsn", null)
    var auto_init = ProjectSettings.get_setting("sentry/options/auto_init", null)
    var config_ok = typeof(dsn) == TYPE_STRING and dsn == ""
    if config_ok and typeof(auto_init) == TYPE_BOOL and not auto_init:
        print("HARNESS_SENTRY_PRELAUNCH_CONFIG_OK")
    var audio = root.get_node_or_null("FmodManager")
    if audio == null or not audio.call("prepare"):
        quit(2)
        return
    print("HARNESS_AUDIO_PRELAUNCH_OK")
    quit()
"""

_CLEANUP_TIMEOUT_SECONDS = 5.0
_TERMINATE_TIMEOUT_SECONDS = 10.0


def _candidate_port(requested: int | None) -> int:
    with socket.socket() as candidate:
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind(("127.0.0.1", requested or 0))
        except OSError as error:
            raise HeadlessError("The requested Mod port is unavailable.") from error
        return candidate.getsockname()[1]


class HeadlessLaunchMode(StrEnum):
    RECIPE = "recipe"
    FRESH_MENU = "fresh_menu"


class OwnedHeadlessInstance:
    def __init__(
        self,
        options: HeadlessOptions,
        *,
        stop_requested: asyncio.Event | None = None,
        mode: HeadlessLaunchMode = HeadlessLaunchMode.RECIPE,
        development_checkpoints: bool = False,
        development_replay: bool = False,
        replay_instant_presentation: bool = True,
        replay_time_scale: float | None = None,
    ) -> None:
        self.options = options
        self._mode = mode
        self._development_checkpoints = development_checkpoints
        self._development_replay = development_replay
        self._replay_instant_presentation = replay_instant_presentation
        self._replay_time_scale = replay_time_scale
        self._stop_requested = stop_requested
        self.process: asyncio.subprocess.Process | None = None
        self.diagnostics: Diagnostics | None = None
        self._alias: Path | None = None
        self._outputs: tuple[BoundedChildOutput, ...] = ()
        self._drains: tuple[asyncio.Task[None], ...] = ()
        self._captures: tuple[asyncio.Task[bytes], ...] = ()
        self._output_incomplete = False
        self._exit_unconfirmed = False

    async def __aenter__(self) -> Self:
        try:
            if sys.platform != "darwin":
                raise HeadlessError("Isolated native launch is currently verified only on macOS.")
            if self.options.port is not None and not 1 <= self.options.port <= 65535:
                raise HeadlessError("The Mod port must be between 1 and 65535.")
            if self.options.startup_timeout_seconds <= 0:
                raise HeadlessError("The native startup timeout must be positive.")
            game = self.options.game.resolve(strict=True)
            resources = game.parent.parent / "Resources"
            if game.parent.name != "MacOS" or not os.access(game, os.X_OK):
                raise HeadlessError("--game must identify the installed macOS game executable.")
            packs = list(resources.glob("*.pck"))
            if len(packs) != 1:
                raise HeadlessError("The game installation must contain one native resource pack.")
            assembly = resources / f"data_sts2_macos_{platform.machine()}" / "sts2.dll"
            self.game_version = _Version.model_validate_json(
                (resources / "release_info.json").read_bytes()
            ).version
            mod = game.parent / "mods/sts2agent"
            self.mod_version = _Version.model_validate_json(
                (mod / "mod_manifest.json").read_bytes()
            ).version
            self.mod_sha256 = digest(mod / "sts2agent.dll")
            template = NativeTemplate(self.options.template_manifest)
            if digest(assembly) != template.evidence.game_assembly_sha256:
                raise HeadlessError("The native template belongs to a different game assembly.")
            initial_port = _candidate_port(self.options.port)
            output = self.options.output.resolve()
            output.mkdir(parents=True, exist_ok=True)
            if self.options.instance_directory is None:
                self.directory = Path(tempfile.mkdtemp(prefix="headless-", dir=output))
            else:
                self.directory = self.options.instance_directory.resolve()
                self.directory.mkdir(parents=True)
            self.diagnostics = Diagnostics(
                None,
                directory=self.directory,
                control_endpoint=f"http://127.0.0.1:{initial_port}/control",
            )
            self.diagnostics.lifecycle(Lifecycle.WAITING_GAME)
            self.user_directory = self.directory / "appdata/SlayTheSpire2"
            self.template = template.copy_to(self.user_directory, output / "templates")
            home = self.directory / "home"
            home.mkdir()
            engine = self.directory / "engine"
            engine.mkdir()
            name = "STS2-Harness-" + uuid4().hex
            (engine / "game.pck").symlink_to(packs[0])
            (engine / "override.cfg").write_text(
                '[application]\nrun/main_scene="res://harness_audio/game.tscn"\nconfig/use_custom_user_dir=true\n'
                f'config/custom_user_dir_name="{name}"\n'
                '\n[sentry]\nconfig/dsn=""\noptions/auto_init=false\n' + configure_audio(engine),
                encoding="utf-8",
            )
            probe = engine / "probe.gd"
            probe.write_text(_PROBE, encoding="utf-8")
            # Deliberately exclude model configuration and credentials from the game environment.
            self.environment = {
                "PATH": os.defpath,
                "LANG": "en_US.UTF-8",
                "HOME": str(home),
                "XDG_DATA_HOME": str(self.directory / "appdata"),
                "XDG_CONFIG_HOME": str(self.directory / "config"),
                "XDG_CACHE_HOME": str(self.directory / "cache"),
            }
            arguments = (
                str(game),
                "--headless",
                "--audio-driver",
                "Dummy",
                "--force-steam=off",
                "--main-pack",
                str(engine / "game.pck"),
                "--path",
                str(engine),
            )
            async with asyncio.timeout(self.options.startup_timeout_seconds):
                probe_process = await asyncio.create_subprocess_exec(
                    *arguments,
                    "--script",
                    str(probe),
                    "--",
                    "--harness-prepare-audio",
                    env=self.environment,
                    cwd=game.parent,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self.process = probe_process
                probe_out = await self._probe_output(probe_process)
            lines = probe_out.decode("utf-8", errors="replace").splitlines()
            paths = [
                line.removeprefix("HARNESS_USER_DATA=")
                for line in lines
                if line.startswith("HARNESS_USER_DATA=")
            ]
            if len(paths) != 1:
                raise HeadlessError("The native probe did not return one owned user-data path.")
            alias = Path(paths[0])
            parents = (
                home / "Library/Application Support",
                Path.home() / "Library/Application Support",
            )
            if alias.name != name or alias.parent not in parents:
                raise HeadlessError(
                    "The native user-data path is outside the owned launch namespace."
                )
            if alias.is_symlink() or not alias.is_dir():
                raise HeadlessError("The native probe did not create an owned user-data directory.")
            shutil.move(str(alias), self.directory / "probe-user-data")
            alias.symlink_to(self.user_directory, target_is_directory=True)
            self._alias = alias
            self.alias = alias
            if probe_process.returncode != 0 or "HARNESS_SENTRY_PRELAUNCH_CONFIG_OK" not in lines:
                raise HeadlessError(
                    "The native isolation probe did not complete startup preparation."
                )
            audio_pack = engine / "audio/overlay.pck"
            if (
                "HARNESS_AUDIO_PRELAUNCH_OK" not in lines
                or not audio_pack.is_file()
                or audio_pack.stat().st_size == 0
            ):
                raise HeadlessError("The native probe did not prepare headless audio isolation.")
            start = self.directory / "start.json"
            if self._mode is HeadlessLaunchMode.RECIPE:
                start.write_text(self.options.recipe.model_dump_json(), encoding="utf-8")
                self.environment["STS2AGENT_START_RUN_FILE"] = str(start)
            if self._development_checkpoints:
                self.environment["STS2_ALLOW_CHECKPOINTS"] = "true"
            if self._development_replay:
                self.environment["STS2AGENT_REPLAY_ACTIVE"] = "1"
                if self._replay_instant_presentation:
                    self.environment["STS2AGENT_REPLAY_PRESENTATION"] = "instant"
                if self._replay_time_scale is not None:
                    self.environment["STS2AGENT_REPLAY_TIME_SCALE"] = str(self._replay_time_scale)
            self.environment["STS2AGENT_GRACEFUL_HEADLESS_EXIT"] = "1"
            self._outputs = tuple(
                BoundedChildOutput(self.directory, stream) for stream in ChildStream
            )
            for attempt in range(3 if self.options.port is None else 1):
                self.port = initial_port if attempt == 0 else _candidate_port(None)
                self.environment["STS2AGENT_PORT"] = str(self.port)
                self.process = await asyncio.create_subprocess_exec(
                    *arguments,
                    env=self.environment,
                    cwd=game.parent,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                assert self.process.stdout is not None and self.process.stderr is not None
                self._drains = tuple(
                    asyncio.create_task(output.drain(reader))
                    for output, reader in zip(
                        self._outputs, (self.process.stdout, self.process.stderr), strict=True
                    )
                )
                self.diagnostics.headless_instance(
                    self.directory, self.user_directory, self.alias, self.process.pid, self.port
                )
                try:
                    async with asyncio.timeout(self.options.startup_timeout_seconds):
                        await self._ready(start)
                    return self
                except _PortCollision:
                    await self._stop_child()
                    if self.options.port is not None or attempt == 2:
                        raise HeadlessError(
                            "The owned child did not acquire its Mod port."
                        ) from None
                    start.with_name(start.name + ".result.json").unlink(missing_ok=True)
                    shutil.rmtree(self.user_directory)
                    template.copy_to(self.user_directory, output / "templates")
            raise AssertionError("Native launch attempts exhausted.")
        except SessionStopped:
            if self.diagnostics is not None:
                self.diagnostics.lifecycle(Lifecycle.STOPPED)
            await self.aclose()
            raise
        except (OSError, ValidationError, TimeoutError, TemplateError) as error:
            if self.diagnostics is not None:
                self.diagnostics.lifecycle(Lifecycle.FAILED)
            await self.aclose()
            if isinstance(error, TemplateError):
                raise HeadlessError(str(error)) from error
            raise HeadlessError(
                "Isolated native startup failed; inspect the instance startup evidence."
            ) from error
        except BaseException:
            if self.diagnostics is not None:
                self.diagnostics.lifecycle(Lifecycle.FAILED)
            await self.aclose()
            raise

    async def _ready(self, start: Path) -> None:
        assert self.process is not None
        receipt = Path(str(start) + ".result.json")
        recipe = self.options.recipe
        seed = recipe.seed or ""
        # The Mod verifies Unicode casing using the game's .NET rules.
        expected_seed = (
            seed.strip(" \t\n\v\f\r").upper().replace("O", "0").replace("I", "1")
            if seed.isascii()
            else ""
        )
        async with ModClient(f"http://127.0.0.1:{self.port}") as client:
            previous: native.Observation | None = None
            previous_menu: native.Status | None = None
            while True:
                if self._stop_requested is not None and self._stop_requested.is_set():
                    raise SessionStopped()
                if self.process.returncode is not None:
                    raise HeadlessError("The owned native process exited before readiness.")
                listener = await asyncio.create_subprocess_exec(
                    "/usr/sbin/lsof",
                    "-nP",
                    f"-iTCP:{self.port}",
                    "-sTCP:LISTEN",
                    "-t",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await listener.communicate()
                owners = {int(value) for value in stdout.split()}
                if owners and owners != {self.process.pid}:
                    raise _PortCollision()
                if not owners or (self._mode is HeadlessLaunchMode.RECIPE and not receipt.exists()):
                    previous = None
                    previous_menu = None
                    await asyncio.sleep(0.1)
                    continue
                try:
                    if self._mode is HeadlessLaunchMode.RECIPE:
                        self.receipt = NativeStartReceipt.model_validate_json(receipt.read_bytes())
                    status = await client.status()
                    self.bootstrap(Endpoint.STATUS, status)
                except ClientUnavailable as error:
                    self.bootstrap(Endpoint.STATUS, error=error.reason)
                    previous = None
                    previous_menu = None
                    await asyncio.sleep(0.1)
                    continue
                if self._mode is HeadlessLaunchMode.FRESH_MENU:
                    if not isinstance(status, native.Status):
                        raise HeadlessError("The owned menu did not return public status.")
                    if status.input_readiness is native.InputReadiness.BUSY:
                        previous_menu = None
                    elif (
                        status.control_intent is native.ControlIntent.PAUSED
                        and status.location.run_id is None
                        and status.location.node_id is None
                        and status.location.owner is None
                        and status.receipt is None
                    ):
                        if previous_menu == status:
                            return
                        previous_menu = status
                    else:
                        raise HeadlessError(
                            "Checkpoint loading requires an owned fresh paused menu."
                        )
                    await asyncio.sleep(0.1)
                    continue
                try:
                    state = await client.state()
                    self.bootstrap(Endpoint.STATE, state)
                except ClientUnavailable as error:
                    self.bootstrap(Endpoint.STATE, error=error.reason)
                    previous = None
                    await asyncio.sleep(0.1)
                    continue
                if not isinstance(status, native.Status) or not isinstance(
                    state, native.StateCompleted
                ):
                    raise HeadlessError(
                        "The owned native process did not return ready public state."
                    )
                observation = state.observation
                readiness = (status.input_readiness, observation.input_readiness)
                if any(
                    value not in (native.InputReadiness.READY, native.InputReadiness.BUSY)
                    for value in readiness
                ):
                    raise HeadlessError("The controlled native start is not ready for run input.")
                if native.InputReadiness.BUSY in readiness:
                    previous = None
                    await asyncio.sleep(0.1)
                    continue
                assert self.receipt is not None
                if (
                    status.control_intent is not native.ControlIntent.PAUSED
                    or status.location != observation.location
                    or observation.location.run_id is None
                    or not isinstance(observation.state, native.RunState)
                    or observation.state.player.definition_id
                    != f"CHARACTER.{recipe.character.value}"
                    or observation.run_rules.ascension != recipe.ascension
                    or (expected_seed and self.receipt.seed != expected_seed)
                ):
                    raise HeadlessError(
                        "The controlled native start did not match the paused run recipe."
                    )
                if (
                    previous is not None
                    and previous.location == observation.location
                    and previous.state == observation.state
                ):
                    self.opening = observation
                    (self.directory / "initial-observation.json").write_text(
                        observation.to_json(), encoding="utf-8"
                    )
                    return
                previous = observation
                await asyncio.sleep(0.1)

    def bootstrap(
        self,
        endpoint: Endpoint,
        reply: StatusResult | StateResult | ControlResult | None = None,
        *,
        error: ClientFailureKind | None = None,
    ) -> None:
        with (self.directory / "bootstrap.jsonl").open("a", encoding="utf-8") as destination:
            destination.write(
                BootstrapAttempt(endpoint=endpoint, reply=reply, error=error).to_json() + "\n"
            )

    async def _probe_output(self, process: asyncio.subprocess.Process) -> bytes:
        assert process.stdout is not None and process.stderr is not None
        overflow = asyncio.Event()
        self._captures = tuple(
            asyncio.create_task(capture_probe(reader, overflow))
            for reader in (process.stdout, process.stderr)
        )
        exited = asyncio.create_task(process.wait())
        exceeded = asyncio.create_task(overflow.wait())
        captures = self._captures
        try:
            await asyncio.wait((exited, exceeded), return_when=asyncio.FIRST_COMPLETED)
        finally:
            exited.cancel()
            exceeded.cancel()
            await asyncio.gather(exited, exceeded, return_exceptions=True)
            await self._stop_child()
        if overflow.is_set():
            raise HeadlessError("The native isolation probe exceeded its output limit.")
        return captures[0].result()

    async def _stop_child(self) -> None:
        cleanup = asyncio.create_task(self._reap_child())
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        cleanup.result()
        if cancellation is not None:
            raise cancellation

    async def _reap_child(self) -> None:
        if self._exit_unconfirmed:
            raise HeadlessError("The owned native process exit could not be confirmed.")
        if self._output_incomplete:
            raise HeadlessError("The owned native output did not finish draining.")
        process = self.process
        if process is None:
            return
        exited = asyncio.create_task(process.wait())
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            await asyncio.wait((exited,), timeout=_TERMINATE_TIMEOUT_SECONDS)
        if not exited.done() and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        if not exited.done():
            await asyncio.wait((exited,), timeout=_CLEANUP_TIMEOUT_SECONDS)
        if not exited.done():
            exited.cancel()
            self._exit_unconfirmed = True
            raise HeadlessError("The owned native process exit could not be confirmed.")
        exited.result()
        drains = self._drains
        captures = self._captures
        output_tasks = (*drains, *captures)
        if output_tasks:
            _, pending = await asyncio.wait(output_tasks, timeout=_CLEANUP_TIMEOUT_SECONDS)
            if pending:
                self._output_incomplete = True
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending, timeout=_CLEANUP_TIMEOUT_SECONDS)
                raise HeadlessError("The owned native output did not finish draining.")
        drain_results = await asyncio.gather(*drains, return_exceptions=True)
        capture_results = await asyncio.gather(*captures, return_exceptions=True)
        for stream, capture in zip(ChildStream, captures, strict=False):
            if not capture.cancelled() and capture.exception() is None:
                (self.directory / f"probe.{stream.value}.log").write_bytes(capture.result())
        for result in (*drain_results, *capture_results):
            if isinstance(result, BaseException):
                raise result
        self.process = None
        self._drains = ()
        self._captures = ()

    async def _close(self) -> None:
        try:
            await self._stop_child()
            if self._alias is not None:
                if not self._alias.is_symlink() or self._alias.readlink() != self.user_directory:
                    raise HeadlessError("The owned native user-data alias no longer matches.")
                self._alias.unlink()
                self._alias = None
        finally:
            for output in self._outputs:
                output.close()
            if self.diagnostics is not None:
                self.diagnostics.close()

    async def aclose(self) -> None:
        cleanup = asyncio.create_task(self._close())
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        cleanup.result()
        if cancellation is not None:
            raise cancellation

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


class _PortCollision(Exception):
    pass


type SessionFactory = Callable[
    [Manifest, Path, Diagnostics], AbstractAsyncContextManager[RunSession]
]


async def run_headless(
    options: HeadlessOptions,
    configuration: Configuration,
    *,
    stop_requested: asyncio.Event,
    session_factory: SessionFactory | None = None,
    materials: EntryMaterials | None = None,
    user_input: UserInputSource | None = None,
) -> RunEnded | None:
    if configuration.driver is not Driver.BUILTIN or configuration.model is None:
        raise ConfigurationError(
            "Headless startup requires builtin driving and a configured model."
        )
    model = configuration.model
    try:
        Llm.validate_configuration(model)
    except ModelUnavailable as error:
        raise ConfigurationError(str(error)) from None
    if stop_requested.is_set():
        return None
    try:
        async with OwnedHeadlessInstance(options, stop_requested=stop_requested) as instance:
            assert instance.process is not None
            diagnostics = instance.diagnostics
            assert diagnostics is not None
            diagnostics.configured(configuration, instance.port)
            diagnostics.native_status(
                native.Status(
                    presentation=None,
                    location=instance.opening.location,
                    input_readiness=instance.opening.input_readiness,
                    control_intent=native.ControlIntent.PAUSED,
                    receipt=None,
                ),
                True,
            )
            try:
                if stop_requested.is_set():
                    diagnostics.lifecycle(Lifecycle.STOPPED)
                    return None
                # Initial control is a host bootstrap fact, before Journal and gameplay ownership.
                async with ModClient(f"http://127.0.0.1:{instance.port}") as control:
                    try:
                        initial_control = await control.control(native.ControlIntent.RUNNING)
                    except ClientUnavailable as error:
                        instance.bootstrap(Endpoint.CONTROL, error=error.reason)
                        raise HeadlessError(
                            "Initial Start could not be confirmed; it will not be resent."
                        ) from error
                    instance.bootstrap(Endpoint.CONTROL, initial_control)
                (instance.directory / "initial-control.json").write_text(
                    initial_control.to_json(), encoding="utf-8"
                )
                if (
                    not isinstance(initial_control, native.ControlReply)
                    or initial_control.mode is not native.ControlIntent.RUNNING
                ):
                    raise HeadlessError("The native process did not confirm initial Start.")
            except BaseException:
                diagnostics.lifecycle(Lifecycle.FAILED)
                raise
            run_id = instance.opening.location.run_id
            assert run_id is not None
            manifest = Manifest(
                minimal=configuration.minimal,
                capacity_policy=CapacityPolicy.for_configuration(model),
                materials=materials.settings
                if materials is not None
                else SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id=uuid4().hex,
                game_instance_id=instance.opening.location.game_instance_id,
                run_id=run_id,
                initial_notes="",
                seed=instance.receipt.seed,
                template=instance.template.manifest_sha256,
                game_version=instance.game_version,
                headless=HeadlessInitialConditions(
                    requested_character=options.recipe.character,
                    requested_ascension=options.recipe.ascension,
                    effective_settings_sha256=instance.template.settings_sha256,
                    game_assembly_sha256=instance.template.game_assembly_sha256,
                    mod_version=instance.mod_version,
                    mod_sha256=instance.mod_sha256,
                    start_receipt=instance.receipt,
                    initial_control=initial_control,
                    initial_observation_sha256=hashlib.sha256(
                        instance.opening.to_json().encode()
                    ).hexdigest(),
                ),
                **current_core_identity(),
            )
            archive = instance.directory / "archive.sqlite"
            diagnostics.bind_recording(manifest.recording_id, instance.opening.location, archive)

            def new_session(
                manifest: Manifest, archive: Path, diagnostics: Diagnostics
            ) -> AbstractAsyncContextManager[RunSession]:
                return live_session(
                    ModClient(f"http://127.0.0.1:{instance.port}"),
                    manifest,
                    archive,
                    model=Llm(model),
                    documents=materials.documents(
                        MaterialContext(options.recipe.ascension, configuration.minimal)
                    )
                    if materials is not None
                    else None,
                    diagnostics=diagnostics,
                    stop_requested=stop_requested.is_set,
                    user_input=user_input,
                )

            factory = session_factory or new_session

            async def watch_child() -> None:
                assert instance.process is not None
                await instance.process.wait()
                stop_requested.set()

            monitor = asyncio.create_task(watch_child())
            try:
                async with factory(manifest, archive, diagnostics) as session:
                    diagnostics.lifecycle(Lifecycle.RUNNING)
                    ended = await session.run()
                    diagnostics.lifecycle(
                        Lifecycle.TERMINAL
                        if ended.reason is RunEndReason.TERMINAL
                        else Lifecycle.STOPPED
                    )
                    return ended
            except SessionStopped:
                if instance.process.returncode is not None:
                    diagnostics.lifecycle(Lifecycle.FAILED)
                    raise HeadlessError("The owned game exited before the run completed.") from None
                diagnostics.lifecycle(Lifecycle.STOPPING)
                diagnostics.lifecycle(Lifecycle.STOPPED)
                return None
            except BaseException:
                diagnostics.lifecycle(Lifecycle.FAILED)
                raise
            finally:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
    except SessionStopped:
        return None
