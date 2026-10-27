"""One logical model request, without conversation ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self

import httpx2 as httpx
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
)
from pydantic import ValidationError

from sts2_harness.config import ModelConfiguration, Protocol
from sts2_harness.llm.messages import (
    FailureKind,
    ModelAttemptSink,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    ToolDefinition,
)
from sts2_harness.llm.responses import ResponsesAdapter, classify_error


class Llm:
    def __init__(
        self,
        configuration: ModelConfiguration,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if configuration.protocol is not Protocol.RESPONSES:
            raise ModelUnavailable(FailureKind.CONFIGURATION)
        self._adapter = ResponsesAdapter(configuration, transport)
        self._configuration = configuration
        self._sleep = sleep

    @property
    def configuration(self) -> ModelConfiguration:
        return self._configuration

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._adapter.close()

    async def complete(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        evidence: ModelAttemptSink | None = None,
    ) -> ModelReply:
        for attempt in range(self._configuration.extra_retries + 1):
            if evidence is not None:
                evidence.started(attempt + 1)
            try:
                async with asyncio.timeout(self._configuration.timeout_seconds):
                    return await self._adapter.complete(input, tools, evidence=evidence)
            except (TimeoutError, APITimeoutError, httpx.TimeoutException):
                reason = FailureKind.TIMEOUT
            except (APIConnectionError, httpx.TransportError):
                reason = FailureKind.CONNECTION
            except APIStatusError as error:
                reason = classify_error(error.code, error.status_code)
            except APIResponseValidationError:
                reason = FailureKind.PROTOCOL
            except APIError as error:
                reason = classify_error(error.code)
            except (ValidationError, ValueError):
                reason = FailureKind.PROTOCOL
            except ModelUnavailable as error:
                reason = error.reason
            if evidence is not None:
                evidence.failed(reason)
            retryable = reason in {
                FailureKind.TIMEOUT,
                FailureKind.CONNECTION,
                FailureKind.RATE_LIMIT,
                FailureKind.OVERLOAD,
                FailureKind.INCOMPLETE_EOF,
            }
            if not retryable or attempt == self._configuration.extra_retries:
                raise ModelUnavailable(reason) from None
            await self._sleep(min(2 ** (attempt + 1), 32))
        raise AssertionError("The attempt budget always includes an initial request.")
