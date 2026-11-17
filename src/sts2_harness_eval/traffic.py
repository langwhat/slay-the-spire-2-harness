"""Capture Responses HTTP attempts and terminal-event timing without changing the stream."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Annotated

import httpx2 as httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)


@dataclass(frozen=True, slots=True)
class ReceiptUsage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None

    @property
    def weighted_tokens(self) -> float | None:
        if (
            self.input_tokens is None
            or self.output_tokens is None
            or self.cache_read_tokens is None
        ):
            return None
        return self.input_tokens - self.cache_read_tokens * 0.95 + self.output_tokens * 5


def _absent_when_invalid(value: object, handler: ValidatorFunctionWrapHandler) -> object:
    try:
        return handler(value)
    except ValidationError:
        return None


# Each receipt field is read independently; a malformed field is treated as absent.
type _Lenient[T] = Annotated[T | None, WrapValidator(_absent_when_invalid)]


class _Receipt(BaseModel):
    model_config = ConfigDict(strict=True)


class _InputDetails(_Receipt):
    cached_tokens: _Lenient[Annotated[int, Field(ge=0)]] = None
    cache_write_tokens: _Lenient[Annotated[int, Field(ge=0)]] = None


class _OutputDetails(_Receipt):
    reasoning_tokens: _Lenient[Annotated[int, Field(ge=0)]] = None


class _Usage(_Receipt):
    input_tokens: _Lenient[Annotated[int, Field(ge=0)]] = None
    output_tokens: _Lenient[Annotated[int, Field(ge=0)]] = None
    input_tokens_details: _Lenient[_InputDetails] = None
    output_tokens_details: _Lenient[_OutputDetails] = None


class _UsageCarrier(_Receipt):
    usage: _Lenient[_Usage] = None


class _TerminalEvent(StrEnum):
    COMPLETED = "response.completed"
    FAILED = "response.failed"
    INCOMPLETE = "response.incomplete"


class _StreamEvent(_Receipt):
    type: _Lenient[_TerminalEvent] = None
    response: _Lenient[_UsageCarrier] = None


def _usage(value: _Usage | None) -> ReceiptUsage | None:
    if value is None:
        return None
    input_details = value.input_tokens_details or _InputDetails()
    output_details = value.output_tokens_details or _OutputDetails()
    return ReceiptUsage(
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        cache_read_tokens=input_details.cached_tokens,
        cache_write_tokens=input_details.cache_write_tokens,
        reasoning_tokens=output_details.reasoning_tokens,
    )


@dataclass(slots=True)
class HttpAttempt:
    body: bytes
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_clock: float = field(default_factory=monotonic)
    status: int | None = None
    error: str | None = None
    terminal_event: str | None = None
    completed_seconds: float | None = None
    closed_seconds: float | None = None
    usage: ReceiptUsage | None = None
    raw: bytearray = field(default_factory=bytearray)
    pending: bytes = b""

    def accept(self, chunk: bytes) -> None:
        self.raw.extend(chunk)
        self.pending = (self.pending + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in self.pending:
            frame, self.pending = self.pending.split(b"\n\n", 1)
            parts = [line[5:].lstrip() for line in frame.split(b"\n") if line.startswith(b"data:")]
            if not parts or parts == [b"[DONE]"]:
                continue
            try:
                event = _StreamEvent.model_validate_json(b"\n".join(parts))
            except ValidationError:
                continue
            if event.type is None or self.terminal_event is not None:
                continue
            self.terminal_event = event.type.value
            if event.type is _TerminalEvent.COMPLETED:
                self.completed_seconds = monotonic() - self.started_clock
            self.usage = _usage(event.response.usage if event.response is not None else None)

    def closed(self) -> None:
        if self.closed_seconds is None:
            self.closed_seconds = monotonic() - self.started_clock
        if self.usage is None and self.raw:
            try:
                payload = _UsageCarrier.model_validate_json(bytes(self.raw))
            except ValidationError:
                return
            self.usage = _usage(payload.usage)


class _AuditingStream(httpx.AsyncByteStream):
    def __init__(self, original: httpx.AsyncByteStream, attempt: HttpAttempt) -> None:
        self.original = original
        self.attempt = attempt

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.original:
                self.attempt.accept(chunk)
                yield chunk
        except Exception as error:
            self.attempt.error = type(error).__name__
            raise

    async def aclose(self) -> None:
        try:
            await self.original.aclose()
        finally:
            self.attempt.closed()


class AuditingTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.attempts: list[HttpAttempt] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        attempt = HttpAttempt(body=request.content)
        self.attempts.append(attempt)
        try:
            response = await self.delegate.handle_async_request(request)
        except Exception as error:
            attempt.error = type(error).__name__
            attempt.closed()
            raise
        attempt.status = response.status_code
        if response.is_stream_consumed:
            attempt.accept(response.content)
            attempt.closed()
        else:
            assert isinstance(response.stream, httpx.AsyncByteStream)
            response.stream = _AuditingStream(response.stream, attempt)
        return response

    async def aclose(self) -> None:
        await self.delegate.aclose()
