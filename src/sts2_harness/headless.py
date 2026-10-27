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
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Annotated, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sts2_harness.client import ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import ClientFailureKind, ControlResult, StateResult, StatusResult
from sts2_harness.config import Configuration, ConfigurationError, Driver
from sts2_harness.diagnostics import Diagnostics, Lifecycle
from sts2_harness.game import Endpoint, RunEnded, RunEndReason, SessionStopped
from sts2_harness.journal import (
    HeadlessInitialConditions,
    Manifest,
    NativeStartReceipt,
    StandardCharacter,
)
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelUnavailable
from sts2_harness.native_template import NativeTemplate, TemplateError, digest
from sts2_harness.session import RunSession


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
    quit()
"""


def _candidate_port(requested: int | None) -> int:
    with socket.socket() as candidate:
        try:
            candidate.bind(("127.0.0.1", requested or 0))
        except OSError as error:
            raise HeadlessError("The requested Mod port is unavailable.") from error
        return candidate.getsockname()[1]


class OwnedHeadlessInstance:
    def __init__(
        self, options: HeadlessOptions, *, stop_requested: asyncio.Event | None = None
    ) -> None:
        self.options = options
        self._stop_requested = stop_requested
        self.process: asyncio.subprocess.Process | None = None
        self.diagnostics: Diagnostics | None = None
        self._alias: Path | None = None
        self._stdout = None
        self._stderr = None

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
                "[application]\nconfig/use_custom_user_dir=true\n"
                f'config/custom_user_dir_name="{name}"\n'
                '\n[sentry]\nconfig/dsn=""\noptions/auto_init=false\n'
            )
            probe = engine / "probe.gd"
            probe.write_text(_PROBE)
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
                    env=self.environment,
                    cwd=game.parent,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self.process = probe_process
                probe_out, probe_err = await probe_process.communicate()
            (self.directory / "probe.stdout.log").write_bytes(probe_out)
            (self.directory / "probe.stderr.log").write_bytes(probe_err)
            self.process = None
            lines = probe_out.decode("utf-8", errors="replace").splitlines()
            paths = [
                line.removeprefix("HARNESS_USER_DATA=")
                for line in lines
                if line.startswith("HARNESS_USER_DATA=")
            ]
            if (
                probe_process.returncode != 0
                or len(paths) != 1
                or "HARNESS_SENTRY_PRELAUNCH_CONFIG_OK" not in lines
            ):
                raise HeadlessError(
                    "The native isolation probe did not verify user data and telemetry settings."
                )
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
            start = self.directory / "start.json"
            start.write_text(self.options.recipe.model_dump_json())
            self.environment.update(
                {
                    "STS2AGENT_START_RUN_FILE": str(start),
                    "STS2AGENT_GRACEFUL_HEADLESS_EXIT": "1",
                }
            )
            self._stdout = (self.directory / "game.stdout.log").open("wb")
            self._stderr = (self.directory / "game.stderr.log").open("wb")
            for attempt in range(3 if self.options.port is None else 1):
                self.port = initial_port if attempt == 0 else _candidate_port(None)
                self.environment["STS2AGENT_PORT"] = str(self.port)
                self.process = await asyncio.create_subprocess_exec(
                    *arguments,
                    env=self.environment,
                    cwd=game.parent,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    start_new_session=True,
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
        async with ModClient(f"http://127.0.0.1:{self.port}") as client:
            previous: native.Observation | None = None
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
                if not owners or not receipt.exists():
                    previous = None
                    await asyncio.sleep(0.1)
                    continue
                try:
                    self.receipt = NativeStartReceipt.model_validate_json(receipt.read_bytes())
                    status = await client.status()
                    self.bootstrap(Endpoint.STATUS, status)
                except ClientUnavailable as error:
                    self.bootstrap(Endpoint.STATUS, error=error.reason)
                    previous = None
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
                if (
                    status.control_intent is not native.ControlIntent.PAUSED
                    or status.location != observation.location
                    or observation.location.run_id is None
                    or not isinstance(observation.state, native.RunState)
                    or observation.state.player.definition_id
                    != f"CHARACTER.{self.options.recipe.character.value}"
                ):
                    raise HeadlessError(
                        "The controlled native start did not match the paused run recipe."
                    )
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
                if (
                    previous is not None
                    and previous.location == observation.location
                    and previous.state == observation.state
                ):
                    self.opening = observation
                    (self.directory / "initial-observation.json").write_text(observation.to_json())
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

    async def _stop_child(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        else:
            await self.process.wait()
        self.process = None

    async def aclose(self) -> None:
        await self._stop_child()
        if self._stdout is not None:
            self._stdout.close()
        if self._stderr is not None:
            self._stderr.close()
        if (
            self._alias is not None
            and self._alias.is_symlink()
            and self._alias.readlink() == self.user_directory
        ):
            self._alias.unlink()
        if self.diagnostics is not None:
            self.diagnostics.close()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


class _PortCollision(Exception):
    pass


type SessionFactory = Callable[[Manifest, Path, Diagnostics], RunSession]


async def run_headless(
    options: HeadlessOptions,
    configuration: Configuration,
    *,
    stop_requested: asyncio.Event,
    session_factory: SessionFactory | None = None,
) -> RunEnded | None:
    if configuration.driver is not Driver.BUILTIN or configuration.model is None:
        raise ConfigurationError(
            "Headless startup requires builtin driving and a configured model."
        )
    model = configuration.model
    try:
        Llm.validate_configuration(model)
    except ModelUnavailable:
        raise ConfigurationError("The configured model protocol is not implemented.") from None
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
                (instance.directory / "initial-control.json").write_text(initial_control.to_json())
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
                recording_id=uuid4().hex,
                core_version="0.1.0",
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
            )
            archive = instance.directory / "archive.sqlite"
            diagnostics.bind_recording(manifest.recording_id, instance.opening.location, archive)

            def new_session(
                manifest: Manifest, archive: Path, diagnostics: Diagnostics
            ) -> RunSession:
                return RunSession(
                    ModClient(f"http://127.0.0.1:{instance.port}"),
                    manifest,
                    archive,
                    model=Llm(model),
                    diagnostics=diagnostics,
                    stop_requested=stop_requested.is_set,
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
