"""Official Responses SDK and native conversation codec."""

import json
from typing import Annotated, Literal, assert_never

import httpx2 as httpx
from openai import Omit, omit
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputMessage,
    ToolParam,
)
from openai.types.responses.service_tier import ServiceTier
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from sts2_harness.config import ModelConfiguration
from sts2_harness.llm.messages import (
    AssistantMessage,
    FailureKind,
    ModelAttemptSink,
    ModelInput,
    ModelMetadata,
    ModelReply,
    ModelUnavailable,
    ResponsesReply,
    TextMessage,
    ToolDefinition,
    ToolResultMessage,
    Usage,
)
from sts2_harness.llm.openai_boundary import classify_error, openai_client

_NATIVE_WIRE = TypeAdapter(list[dict[str, JsonValue]])
_FUNCTION = TypeAdapter(FunctionToolParam)
_REASONING = TypeAdapter(Reasoning)
_SERVICE_TIER = TypeAdapter(ServiceTier)
type _TokenCount = Annotated[int, Field(ge=0)]


class _CacheUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    cached_tokens: _TokenCount | None = None
    cache_write_tokens: _TokenCount | None = None


class _OutputUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    reasoning_tokens: _TokenCount | None = None


class _WireUsage(BaseModel):
    # Preserve unknown counts when proxies omit fields declared required by the SDK.
    model_config = ConfigDict(strict=True)
    input_tokens: _TokenCount | None = None
    output_tokens: _TokenCount | None = None
    input_tokens_details: _CacheUsage | None = None
    output_tokens_details: _OutputUsage | None = None


class _CompletedReply(BaseModel):
    model_config = ConfigDict(strict=True)
    status: Literal["completed"]
    output: tuple[ResponseOutputItem, ...]
    model: str | None = None
    service_tier: str | None = None
    usage: _WireUsage | None = None


class _CompletedEvent(BaseModel):
    model_config = ConfigDict(strict=True)
    response: _CompletedReply


class _ErrorCode(BaseModel):
    model_config = ConfigDict(strict=True)
    code: str | None = None


class _FailedReply(BaseModel):
    model_config = ConfigDict(strict=True)
    status: Literal["failed"]
    error: _ErrorCode | None = None


class _FailedEvent(BaseModel):
    model_config = ConfigDict(strict=True)
    response: _FailedReply


def _encode(input: ModelInput) -> list[dict[str, JsonValue]]:
    encoded: list[dict[str, JsonValue]] = []
    for message in input:
        match message:
            case TextMessage():
                encoded.append({"role": message.role.value, "content": message.text})
            case ToolResultMessage(call_id):
                encoded.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": message.text,
                    }
                )
            case AssistantMessage(native):
                if not isinstance(native, ResponsesReply):
                    raise ModelUnavailable(FailureKind.CONFIGURATION)
                # Retain omitted/null distinctions and opaque continuation blocks exactly once.
                encoded.extend(_NATIVE_WIRE.validate_json(native.to_json(), strict=True))
            case _:
                assert_never(message)
    return encoded


def _tools(tools: tuple[ToolDefinition, ...]) -> list[ToolParam]:
    return [
        _FUNCTION.validate_json(
            json.dumps(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description.body,
                    "parameters": tool.parameters.model_json_schema(),
                    # Preserve optional fields; tools own strict local argument decoding.
                    "strict": False,
                }
            ),
            strict=True,
        )
        for tool in tools
    ]


class ResponsesAdapter:
    def __init__(
        self, configuration: ModelConfiguration, transport: httpx.AsyncBaseTransport | None
    ) -> None:
        self._configuration = configuration
        self._sdk, self._headers = openai_client(configuration, transport)

    async def close(self) -> None:
        await self._sdk.close()

    def prepare(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> dict[str, JsonValue]:
        try:
            reasoning = (
                _REASONING.validate_python(
                    {"effort": self._configuration.reasoning_effort}, strict=True
                )
                if self._configuration.reasoning_effort
                else omit
            )
            tier = (
                _SERVICE_TIER.validate_python(self._configuration.service_tier, strict=True)
                if self._configuration.service_tier
                else omit
            )
        except ValueError:
            raise ModelUnavailable(FailureKind.CONFIGURATION) from None
        encoded_input = _encode(input)
        encoded_tools = _tools(tools)
        encoding: dict[str, JsonValue] = {
            "model": self._configuration.model,
            "input": TypeAdapter(JsonValue).validate_python(encoded_input),
            "tools": TypeAdapter(JsonValue).validate_python(
                TypeAdapter(list[ToolParam]).dump_python(encoded_tools, mode="json")
            ),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "stream": True,
        }
        if not isinstance(reasoning, Omit):
            encoding["reasoning"] = TypeAdapter(JsonValue).validate_python(
                TypeAdapter(Reasoning).dump_python(reasoning, mode="json")
            )
        if not isinstance(tier, Omit):
            encoding["service_tier"] = tier

        return encoding

    async def complete(
        self,
        body: dict[str, JsonValue],
        *,
        evidence: ModelAttemptSink | None = None,
    ) -> ModelReply:
        stream = await self._sdk.responses.create(
            model=self._configuration.model,
            # Preserve opaque continuation fields and explicit nulls.
            extra_body=body,
            stream=True,
            extra_headers=self._headers,
        )
        async with stream:
            media_type = stream.response.headers.get("content-type", "").partition(";")[0]
            if media_type.strip().lower() != "text/event-stream":
                raise ModelUnavailable(FailureKind.PROTOCOL)
            async for event in stream:
                if event.type == "response.incomplete":
                    raise ModelUnavailable(FailureKind.TRUNCATED)
                if event.type == "response.failed":
                    failed = _FailedEvent.model_validate_json(
                        event.model_dump_json(
                            exclude_unset=True, by_alias=True, serialize_as_any=True
                        ),
                        strict=True,
                    )
                    code = failed.response.error.code if failed.response.error else None
                    raise ModelUnavailable(classify_error(code))
                if event.type == "error":
                    error = _ErrorCode.model_validate_json(
                        event.model_dump_json(
                            exclude_unset=True, by_alias=True, serialize_as_any=True
                        ),
                        strict=True,
                    )
                    raise ModelUnavailable(classify_error(error.code))
                if event.type == "response.completed":
                    completed = _CompletedEvent.model_validate_json(
                        event.model_dump_json(
                            exclude_unset=True, by_alias=True, serialize_as_any=True
                        ),
                        strict=True,
                    )
                    reply = self._reply(completed.response)
                    if evidence is not None:
                        evidence.received(reply)
                    return reply
        raise ModelUnavailable(FailureKind.INCOMPLETE_EOF)

    def _reply(self, completed: _CompletedReply) -> ModelReply:
        if any(
            isinstance(item, (ResponseOutputMessage, ResponseFunctionToolCall))
            and item.status in ("in_progress", "incomplete")
            for item in completed.output
        ):
            raise ModelUnavailable(FailureKind.PROTOCOL)
        usage = completed.usage or _WireUsage()
        details = usage.input_tokens_details
        return ModelReply(
            AssistantMessage(ResponsesReply.from_items(completed.output)),
            Usage(
                usage.input_tokens,
                usage.output_tokens,
                details.cached_tokens if details else None,
                details.cache_write_tokens if details else None,
                usage.output_tokens_details.reasoning_tokens
                if usage.output_tokens_details
                else None,
            ),
            ModelMetadata(
                self._configuration.model,
                self._configuration.reasoning_effort,
                completed.model,
                completed.service_tier,
            ),
        )
