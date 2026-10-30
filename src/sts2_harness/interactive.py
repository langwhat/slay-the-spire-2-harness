"""Attach a resident backend to independently started games, one owned session per run."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sts2_harness.client import ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import ClientFailureKind
from sts2_harness.config import Configuration, ConfigurationError, Driver, validate_mod_url
from sts2_harness.diagnostics import Diagnostics, Event, EventKind, Lifecycle
from sts2_harness.entry_materials import EntryMaterials, MaterialContext
from sts2_harness.game import (
    ControlWriteError,
    Endpoint,
    GameLifetimeEnded,
    RunEndReason,
    SessionStopped,
)
from sts2_harness.journal import Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelUnavailable
from sts2_harness.results import Reason, ReasonCode
from sts2_harness.session import RunSession


@dataclass(frozen=True)
class InteractiveOptions:
    mod_url: str
    output_directory: Path


type SessionFactory = Callable[
    [Manifest, Path, Diagnostics], AbstractAsyncContextManager[RunSession]
]


async def run_interactive(
    options: InteractiveOptions,
    configuration: Configuration,
    *,
    stop_requested: asyncio.Event,
    client_factory: Callable[[], ModClient] | None = None,
    session_factory: SessionFactory | None = None,
    materials: EntryMaterials | None = None,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if configuration.driver is not Driver.BUILTIN:
        raise ConfigurationError("Resident external driving is not implemented.")
    model = configuration.model
    if model is None:
        raise ConfigurationError("Resident startup requires a configured model.")
    try:
        Llm.validate_configuration(model)
    except ModelUnavailable as error:
        raise ConfigurationError(str(error)) from None
    port = validate_mod_url(options.mod_url)
    control_endpoint = options.mod_url.rstrip("/") + "/control"
    directory = options.output_directory.resolve()
    diagnostics = Diagnostics(None, directory=directory, control_endpoint=control_endpoint)
    diagnostics.configured(configuration, port)

    def new_client() -> ModClient:
        return ModClient(options.mod_url)

    make_client = client_factory or new_client

    paused_instance: str | None = None
    finished: tuple[str, str] | None = None
    discovery_failures = 0
    discovery_cause: ClientFailureKind | native.ErrorCode | None = None
    try:
        diagnostics.lifecycle(Lifecycle.WAITING_GAME)
        async with make_client() as discovery:
            while not stop_requested.is_set():
                try:
                    status = await discovery.status()
                except ClientUnavailable as error:
                    discovery_failures += 1
                    discovery_cause = error.reason
                    diagnostics.lifecycle(Lifecycle.WAITING_GAME)
                    diagnostics.emit(
                        Event(
                            event=EventKind.DISCOVERY_RETRY,
                            endpoint=Endpoint.STATUS,
                            error=error.reason.value,
                            failures=discovery_failures,
                        )
                    )
                    await wait(1.0)
                    continue
                if not isinstance(status, native.Status):
                    discovery_failures += 1
                    discovery_cause = status.error.code
                    diagnostics.emit(
                        Event(
                            event=EventKind.DISCOVERY_RETRY,
                            endpoint=Endpoint.STATUS,
                            error=discovery_cause.value,
                            failures=discovery_failures,
                        )
                    )
                    diagnostics.lifecycle(Lifecycle.WAITING_GAME)
                    await wait(1.0)
                    continue
                if discovery_failures:
                    diagnostics.emit(
                        Event(
                            event=EventKind.DISCOVERY_RECOVERED,
                            endpoint=Endpoint.STATUS,
                            failures=discovery_failures,
                            recovered_from=discovery_cause.value
                            if discovery_cause is not None
                            else None,
                        )
                    )
                    discovery_failures = 0
                    discovery_cause = None
                diagnostics.native_status(status, False)
                location = status.location
                run_id = location.run_id
                identity = (location.game_instance_id, run_id) if run_id is not None else None
                if run_id is None or (
                    status.input_readiness is native.InputReadiness.TERMINAL
                    and identity != finished
                ):
                    diagnostics.bind_recording(None, location, None)
                if (
                    run_id is None
                    or identity == finished
                    or status.input_readiness is native.InputReadiness.TERMINAL
                ):
                    if paused_instance != location.game_instance_id and (
                        identity is None or identity != finished
                    ):
                        try:
                            result = await discovery.control(native.ControlIntent.PAUSED)
                        except ClientUnavailable as error:
                            if error.reason not in (
                                ClientFailureKind.CONNECTION,
                                ClientFailureKind.TIMEOUT,
                            ):
                                raise ControlWriteError(
                                    Reason(
                                        code=ReasonCode.CONTROL_UNAVAILABLE,
                                        message="Initial Pause could not be confirmed.",
                                    )
                                ) from error
                            diagnostics.lifecycle(Lifecycle.WAITING_GAME)
                            await wait(0.1)
                            continue
                        if (
                            not isinstance(result, native.ControlReply)
                            or result.mode is not native.ControlIntent.PAUSED
                        ):
                            raise ControlWriteError(
                                Reason(
                                    code=ReasonCode.CONTROL_UNAVAILABLE,
                                    message="The Mod did not confirm initial Pause.",
                                )
                            )
                        diagnostics.native_status(
                            status.model_copy(update={"control_intent": result.mode}), False
                        )
                        paused_instance = location.game_instance_id
                    if status.input_readiness is native.InputReadiness.TERMINAL:
                        finished = identity
                    diagnostics.lifecycle(
                        Lifecycle.TERMINAL if identity is not None else Lifecycle.WAITING_RUN
                    )
                    await wait(1.0)
                    continue
                recording_id = uuid4().hex
                archive = directory / recording_id / "archive.sqlite"
                manifest = Manifest(
                    materials=materials.settings
                    if materials is not None
                    else SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    recording_id=recording_id,
                    core_version="0.1.0",
                    game_instance_id=location.game_instance_id,
                    run_id=run_id,
                    initial_notes="",
                )
                diagnostics.bind_recording(recording_id, location, archive)

                session_resource = (
                    session_factory(manifest, archive, diagnostics)
                    if session_factory is not None
                    else live_session(
                        make_client(),
                        manifest,
                        archive,
                        model=Llm(model),
                        documents=materials.documents(MaterialContext(status.ascension))
                        if materials is not None
                        else None,
                        diagnostics=diagnostics,
                        stop_requested=stop_requested.is_set,
                        wait=wait,
                    )
                )
                async with session_resource as session:
                    try:
                        await session.start()
                        paused_instance = location.game_instance_id
                        ended = await session.run()
                    except GameLifetimeEnded as error:
                        end_reason = session.execution.end_reason(error.location)
                    else:
                        end_reason = ended.reason
                    if end_reason is RunEndReason.TERMINAL:
                        diagnostics.lifecycle(Lifecycle.TERMINAL)
                finished = identity
        diagnostics.lifecycle(Lifecycle.STOPPING)
        diagnostics.lifecycle(Lifecycle.STOPPED)
    except SessionStopped:
        diagnostics.lifecycle(Lifecycle.STOPPING)
        diagnostics.lifecycle(Lifecycle.STOPPED)
    except asyncio.CancelledError:
        diagnostics.lifecycle(Lifecycle.STOPPING)
        raise
    except ControlWriteError:
        diagnostics.lifecycle(Lifecycle.FAILED)
        diagnostics.emit(Event(event=EventKind.LIFECYCLE_CHANGED, error="control_unavailable"))
        raise
    except Exception:
        diagnostics.lifecycle(Lifecycle.FAILED)
        diagnostics.emit(Event(event=EventKind.LIFECYCLE_CHANGED, error="internal_error"))
        raise
    finally:
        diagnostics.close()
