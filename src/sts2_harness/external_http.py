"""The local HTTP boundary for the fixed external gameplay tools."""

import asyncio
import socket
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from sts2_harness.client import ModClient
from sts2_harness.config import Configuration, ConfigurationError, validate_mod_url
from sts2_harness.diagnostics import Diagnostics, Lifecycle
from sts2_harness.external import ExternalBackend, ExternalReply, reject
from sts2_harness.interactive import InteractiveOptions
from sts2_harness.results import ReasonCode, SemanticValue
from sts2_harness.tools import ToolName
from sts2_harness.tools.inputs import DEFINITIONS, Definition


class RoleDefinitions(SemanticValue):
    run: tuple[Definition, ...]
    combat: tuple[Definition, ...]


class Discovery(SemanticValue):
    roles: RoleDefinitions


class _ReplyResponse(Response):
    def __init__(
        self,
        backend: ExternalBackend,
        reply: ExternalReply,
        disconnected: asyncio.Future[Message],
    ) -> None:
        super().__init__(reply.result.to_json(), media_type="application/json")
        self._backend = backend
        self._reply = reply
        self._disconnected = disconnected

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
            # ASGI send establishes exposure, not proof that the caller received it.
            if not self._disconnected.done():
                self._backend.exposed(self._reply)
        finally:
            self._disconnected.cancel()
            await asyncio.gather(self._disconnected, return_exceptions=True)


def create_app(backend: ExternalBackend) -> Starlette:
    external_run_act = Definition.model_validate_json(
        files("sts2_harness.tools").joinpath("external-run-act.json").read_text(), strict=True
    )
    model_only_tools = frozenset({ToolName.RUN_PLAN, ToolName.RUN_NEXT, ToolName.RUN_DECK_CHOICE})
    discovery = Discovery(
        roles=RoleDefinitions(
            run=tuple(
                external_run_act if item.name == ToolName.RUN_ACT else item
                for item in DEFINITIONS
                if item.name.startswith("run_") and item.name not in model_only_tools
            ),
            combat=tuple(item for item in DEFINITIONS if item.name.startswith("combat_")),
        )
    ).to_json()

    async def tools(request: Request) -> Response:
        return Response(discovery, media_type="application/json")

    async def call(request: Request) -> Response:
        try:
            name = ToolName(request.path_params["name"])
            if name in model_only_tools:
                raise ValueError("The tool is not exposed externally.")
        except ValueError:
            return Response(
                reject(ReasonCode.INVALID_ARGUMENTS, "Unknown tool name.").result.to_json(),
                status_code=404,
                media_type="application/json",
            )
        try:
            arguments = (await request.body()).decode("utf-8")
        except UnicodeDecodeError:
            return Response(
                reject(
                    ReasonCode.INVALID_ARGUMENTS, "Arguments must be UTF-8 JSON."
                ).result.to_json(),
                status_code=400,
                media_type="application/json",
            )
        disconnected = asyncio.ensure_future(request.receive())
        try:
            accepted = backend.accept(name, arguments)
            reply = (
                await asyncio.shield(accepted) if isinstance(accepted, asyncio.Task) else accepted
            )
            return _ReplyResponse(backend, reply, disconnected)
        except BaseException:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            raise

    return Starlette(
        routes=[
            Route("/tools", tools, methods=["GET"]),
            Route("/tools/{name}", call, methods=["POST"]),
        ]
    )


async def run_external(
    options: InteractiveOptions,
    configuration: Configuration,
    *,
    tool_port: int,
    stop_requested: asyncio.Event,
) -> None:
    if not 1 <= tool_port <= 65535:
        raise ConfigurationError("The tool port must be between 1 and 65535.")
    mod_port = validate_mod_url(options.mod_url)
    try:
        listener = socket.create_server(("127.0.0.1", tool_port))
    except OSError:
        raise ConfigurationError("The tool port could not be opened.") from None
    with listener:
        diagnostics = Diagnostics(
            None,
            directory=options.output_directory.resolve(),
            control_endpoint=options.mod_url.rstrip("/") + "/control",
        )
        diagnostics.configured(configuration, mod_port)
        try:
            async with ExternalBackend(
                ModClient(options.mod_url),
                diagnostics=diagnostics,
                display_reasons=True,
            ) as backend:
                server = LocalServer(
                    uvicorn.Config(
                        create_app(backend),
                        host="127.0.0.1",
                        port=tool_port,
                        workers=1,
                        proxy_headers=False,
                        access_log=False,
                        log_config=None,
                        timeout_graceful_shutdown=None,
                        lifespan="off",
                    )
                )
                monitor = asyncio.create_task(backend.maintain())

                async def stop() -> None:
                    await stop_requested.wait()
                    backend.stop_accepting()
                    diagnostics.lifecycle(Lifecycle.STOPPING)
                    server.should_exit = True

                stopping = asyncio.create_task(stop())
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    done, _ = await asyncio.wait(
                        (serving, monitor, stopping), return_when=asyncio.FIRST_COMPLETED
                    )
                    if monitor in done:
                        await monitor
                    if serving in done:
                        await serving
                finally:
                    server.should_exit = True
                    backend.stop_accepting()
                    monitor.cancel()
                    stopping.cancel()
                    await asyncio.gather(monitor, stopping, return_exceptions=True)
                    await serving
            diagnostics.lifecycle(Lifecycle.STOPPED)
        finally:
            diagnostics.close()


class LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        # The CLI owns signals so every driver uses the same stop boundary.
        yield
