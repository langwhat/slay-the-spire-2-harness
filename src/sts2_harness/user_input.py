"""Buffer live user input until a model request boundary delivers it to a role's Context."""

import asyncio
import socket
from collections import deque
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

import uvicorn
from pydantic import Field, StringConstraints, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from sts2_harness.client import models as native
from sts2_harness.config import ConfigurationError
from sts2_harness.external_http import LocalServer
from sts2_harness.results import SemanticValue
from sts2_harness.user_input_values import VIEWER_LINE_SEPARATOR, VIEWER_TEXT_LIMIT, UserInput


class UserInputQueue:
    """Memory-only FIFO owned by one backend process; undelivered input dies with the process."""

    def __init__(self) -> None:
        self.instance = uuid4().hex
        self._pending: deque[UserInput] = deque()
        self._seen: set[str] = set()
        self._delivered: list[str] = []

    def submit(self, inputs: Iterable[UserInput]) -> None:
        batch = tuple(inputs)
        if any(len(item.line) > VIEWER_TEXT_LIMIT for item in batch):
            raise ValueError(
                f"Each viewer line is limited to {VIEWER_TEXT_LIMIT:,} characters "
                "after whitespace normalization, including its prefix."
            )
        for item in batch:
            if item.id not in self._seen:
                self._seen.add(item.id)
                self._pending.append(item)

    def drain(self) -> tuple[UserInput, ...]:
        """Take the longest FIFO prefix that fits and record it as delivered."""
        batch: list[UserInput] = []
        length = 0
        while self._pending:
            line_length = len(self._pending[0].line)
            if batch:
                line_length += len(VIEWER_LINE_SEPARATOR)
            if length + line_length > VIEWER_TEXT_LIMIT:
                break
            batch.append(self._pending.popleft())
            length += line_length
        self._delivered.extend(item.id for item in batch)
        return tuple(batch)

    @property
    def delivered(self) -> tuple[str, ...]:
        return tuple(self._delivered)


class _SubmittedInput(SemanticValue):
    id: native.Nonempty
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Submission(SemanticValue):
    inputs: tuple[_SubmittedInput, ...] = Field(min_length=1)


def create_input_app(queue: UserInputQueue) -> Starlette:
    async def submit(request: Request) -> Response:
        # A JSON content type needs a CORS preflight, which keeps ordinary web pages out.
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            return JSONResponse({"error": "Content-Type must be application/json."}, 415)
        try:
            submission = _Submission.model_validate_json(await request.body())
        except ValidationError:
            return JSONResponse({"error": "Expected {inputs: [{id, text}]}."}, 400)
        try:
            queue.submit(UserInput(item.id, item.text) for item in submission.inputs)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, 400)
        return JSONResponse({"instance": queue.instance, "accepted": len(submission.inputs)})

    async def deliveries(request: Request) -> Response:
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            return JSONResponse({"error": "after must be an integer."}, 400)
        delivered = queue.delivered
        return JSONResponse(
            {
                "instance": queue.instance,
                "last": len(delivered),
                "deliveries": [
                    {"sequence": sequence, "id": item}
                    for sequence, item in enumerate(delivered, start=1)
                    if sequence > after
                ],
            }
        )

    return Starlette(
        routes=[
            Route("/inputs", submit, methods=["POST"]),
            Route("/inputs/deliveries", deliveries, methods=["GET"]),
        ]
    )


@asynccontextmanager
async def serving_user_input(port: int) -> AsyncGenerator[UserInputQueue]:
    """Serve one queue on the loopback interface for the lifetime of the block."""
    try:
        listener = socket.create_server(("127.0.0.1", port))
    except (OSError, OverflowError):
        raise ConfigurationError("The input port could not be opened.") from None
    queue = UserInputQueue()
    with listener:
        server = LocalServer(
            uvicorn.Config(
                create_input_app(queue),
                host="127.0.0.1",
                port=port,
                workers=1,
                proxy_headers=False,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=1,
                lifespan="off",
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            yield queue
        finally:
            server.should_exit = True
            await serving
