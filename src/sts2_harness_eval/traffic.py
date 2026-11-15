"""Capture Responses HTTP attempts and terminal-event timing without changing the stream."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic

import httpx2 as httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

_OBJECT = TypeAdapter(dict[str, JsonValue])


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


def _tokens(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _usage(value: JsonValue) -> ReceiptUsage | None:
    if not isinstance(value, dict):
        return None
    input_details = _object(value.get("input_tokens_details"))
    output_details = _object(value.get("output_tokens_details"))
    return ReceiptUsage(
        input_tokens=_tokens(value.get("input_tokens")),
        output_tokens=_tokens(value.get("output_tokens")),
        cache_read_tokens=_tokens(input_details.get("cached_tokens")),
        cache_write_tokens=_tokens(input_details.get("cache_write_tokens")),
        reasoning_tokens=_tokens(output_details.get("reasoning_tokens")),
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
                event = _OBJECT.validate_json(b"\n".join(parts))
            except ValidationError:
                continue
            kind = event.get("type")
            if kind not in ("response.completed", "response.failed", "response.incomplete"):
                continue
            if self.terminal_event is not None:
                continue
            self.terminal_event = str(kind)
            if kind == "response.completed":
                self.completed_seconds = monotonic() - self.started_clock
            response = _object(event.get("response"))
            self.usage = _usage(response.get("usage"))

    def closed(self) -> None:
        if self.closed_seconds is None:
            self.closed_seconds = monotonic() - self.started_clock
        if self.usage is None and self.raw:
            try:
                payload = _OBJECT.validate_json(bytes(self.raw))
            except ValidationError:
                return
            self.usage = _usage(payload.get("usage"))


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
