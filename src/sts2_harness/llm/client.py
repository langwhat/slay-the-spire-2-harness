"""One logical model request, without conversation ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self

import anthropic
import httpx2 as httpx
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
)
from pydantic import ValidationError

from sts2_harness.capacity import CapacityPolicy, measure
from sts2_harness.config import ModelConfiguration, Protocol
from sts2_harness.llm.anthropic_boundary import classify_error as classify_anthropic_error
from sts2_harness.llm.anthropic_messages import MessagesAdapter
from sts2_harness.llm.chat_completions import ChatCompletionsAdapter
from sts2_harness.llm.messages import (
    FailureKind,
    ModelAttemptSink,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    ToolDefinition,
)
from sts2_harness.llm.openai_boundary import classify_error
from sts2_harness.llm.responses import ResponsesAdapter
from sts2_harness.tokenizer import encodings


class Llm:
    def __init__(
        self,
        configuration: ModelConfiguration,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.validate_configuration(configuration)
        self._adapter: ResponsesAdapter | ChatCompletionsAdapter | MessagesAdapter
        match configuration.protocol:
            case Protocol.RESPONSES:
                self._adapter = ResponsesAdapter(configuration, transport)
            case Protocol.CHAT_COMPLETIONS:
                self._adapter = ChatCompletionsAdapter(configuration, transport)
            case Protocol.MESSAGES:
                self._adapter = MessagesAdapter(configuration, transport)
        self._configuration = configuration
        self._sleep = sleep

    @staticmethod
    def validate_configuration(configuration: ModelConfiguration) -> None:
        encodings()
        if configuration.protocol not in {
            Protocol.RESPONSES,
            Protocol.CHAT_COMPLETIONS,
            Protocol.MESSAGES,
        }:
            raise ModelUnavailable(FailureKind.CONFIGURATION)
        if configuration.protocol is Protocol.MESSAGES:
            MessagesAdapter.request_options(configuration)

    @property
    def configuration(self) -> ModelConfiguration:
        return self._configuration

    @property
    def capacity_policy(self) -> CapacityPolicy:
        return CapacityPolicy(context_window=self._configuration.context_window)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._adapter.close()

    def prepare(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> PreparedRequest:
        body = self._adapter.prepare(input, tools)
        return PreparedRequest(input, tools, measure(body, self._configuration.protocol), body)

    async def complete(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        evidence: ModelAttemptSink | None = None,
    ) -> ModelReply:
        return await self._complete(lambda: self.prepare(input, tools), evidence=evidence)

    async def complete_prepared(
        self,
        prepared: PreparedRequest,
        *,
        evidence: ModelAttemptSink | None = None,
        preparation: RequestPreparation | None = None,
    ) -> ModelReply:
        return await self._complete(lambda: prepared, evidence=evidence)

    async def interrupt_preparation(
        self,
        preparation: RequestPreparation,
        tools: tuple[ToolDefinition, ...],
        failure: ModelUnavailable,
    ) -> None:
        return None

    async def _complete(
        self, prepare: Callable[[], PreparedRequest], *, evidence: ModelAttemptSink | None
    ) -> ModelReply:
        for attempt in range(self._configuration.extra_retries + 1):
            started = False
            try:
                prepared = prepare()
                if evidence is not None:
                    evidence.started(attempt + 1)
                    started = True
                    evidence.encoded(prepared.measurement)
                async with asyncio.timeout(self._configuration.timeout_seconds):
                    return await self._adapter.complete(prepared.body, evidence=evidence)
            except (
                TimeoutError,
                APITimeoutError,
                anthropic.APITimeoutError,
                httpx.TimeoutException,
            ):
                reason = FailureKind.TIMEOUT
            except (APIConnectionError, anthropic.APIConnectionError, httpx.TransportError):
                reason = FailureKind.CONNECTION
            except APIStatusError as error:
                reason = classify_error(error.code, error.status_code)
            except anthropic.APIStatusError as error:
                reason = classify_anthropic_error(error.body, error.status_code)
            except anthropic.APIResponseValidationError:
                reason = FailureKind.PROTOCOL
            except anthropic.APIError as error:
                reason = classify_anthropic_error(error.body)
            except APIResponseValidationError:
                reason = FailureKind.PROTOCOL
            except APIError as error:
                reason = classify_error(error.code)
            except (ValidationError, ValueError):
                reason = FailureKind.PROTOCOL
            except ModelUnavailable as error:
                reason = error.reason
            if evidence is not None and started:
                evidence.failed(reason)
            retryable = reason in {
                FailureKind.TIMEOUT,
                FailureKind.CONNECTION,
                FailureKind.RATE_LIMIT,
                FailureKind.OVERLOAD,
                FailureKind.INCOMPLETE_EOF,
                FailureKind.PROVIDER,
            }
            if not retryable or attempt == self._configuration.extra_retries:
                raise ModelUnavailable(reason) from None
            await self._sleep(min(2 ** (attempt + 1), 32))
        raise AssertionError("The attempt budget always includes an initial request.")
