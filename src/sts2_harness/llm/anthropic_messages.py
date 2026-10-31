"""One Messages request with official stream assembly and explicit completion gates."""

import json
from typing import Literal, assert_never

import httpx2 as httpx
from anthropic.lib.streaming import ParsedMessageStreamEvent
from anthropic.types import MessageParam, RawContentBlockDelta, ToolUnionParam
from pydantic import JsonValue, TypeAdapter

from sts2_harness.config import ModelConfiguration
from sts2_harness.llm.anthropic_boundary import anthropic_client
from sts2_harness.llm.messages import (
    AssistantMessage,
    FailureKind,
    ModelAttemptSink,
    ModelInput,
    ModelMetadata,
    ModelReply,
    ModelUnavailable,
    TextMessage,
    TextRole,
    ToolDefinition,
    ToolResultMessage,
    Usage,
)
from sts2_harness.llm.messages_native import MessagesReply, MessagesUsage, NativeBlock

MAX_OUTPUT_TOKENS = 16000
_OBJECT = TypeAdapter(dict[str, JsonValue])
_VALUE = TypeAdapter(JsonValue)
_INDEX = TypeAdapter(int)
_BLOCK = TypeAdapter(NativeBlock)
_DELTA = TypeAdapter(RawContentBlockDelta)
_TOOLS = TypeAdapter(list[ToolUnionParam])
_EFFORT = TypeAdapter(Literal["low", "medium", "high", "xhigh", "max"])
_TIER = TypeAdapter(Literal["auto", "standard_only"])


def _unique_fields(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    fields: dict[str, JsonValue] = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError("Duplicate tool input fields are unsupported.")
        fields[name] = value
    return fields


def _object_input(schema: dict[str, JsonValue], definitions: dict[str, JsonValue]) -> bool:
    if "type" in schema:
        return schema["type"] == "object"
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target = definitions.get(reference.removeprefix("#/$defs/"))
        return isinstance(target, dict) and _object_input(target, definitions)
    branches = schema.get("oneOf", schema.get("anyOf"))
    return (
        isinstance(branches, list)
        and bool(branches)
        and all(
            isinstance(branch, dict) and _object_input(branch, definitions) for branch in branches
        )
    )


def _input_schema(tool: ToolDefinition) -> dict[str, JsonValue]:
    schema = _OBJECT.validate_python(tool.parameters.model_json_schema(), strict=True)
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict) or not _object_input(schema, definitions):
        raise ModelUnavailable(FailureKind.CONFIGURATION)
    # Messages requires the explicit root type even when all union branches are objects.
    schema["type"] = "object"
    return schema


def _encode(input: ModelInput) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    system: list[dict[str, JsonValue]] = []
    messages: list[dict[str, JsonValue]] = []
    results: list[JsonValue] | None = None
    for message in input:
        if isinstance(message, ToolResultMessage):
            if results is None:
                results = []
                messages.append({"role": "user", "content": results})
            results.append(
                {"type": "tool_result", "tool_use_id": message.call_id, "content": message.text}
            )
            continue
        results = None
        match message:
            case TextMessage(role, _):
                if role is TextRole.SYSTEM:
                    if messages:
                        raise ModelUnavailable(FailureKind.CONFIGURATION)
                    system.append({"type": "text", "text": message.text})
                else:
                    messages.append({"role": "user", "content": message.text})
            case AssistantMessage(native):
                if not isinstance(native, MessagesReply):
                    raise ModelUnavailable(FailureKind.CONFIGURATION)
                raw = _OBJECT.validate_json(native.to_json())
                if raw["content"] == []:
                    continue
                messages.append({"role": "assistant", "content": raw["content"]})
            case _:
                assert_never(message)
    return system, messages


class _MessageStream:
    """Validate this stream's order; the SDK owns native content accumulation."""

    def __init__(self) -> None:
        self._envelope: dict[str, JsonValue] | None = None
        self._blocks: list[dict[str, JsonValue]] = []
        self._open: int | None = None
        self._fields: set[str] = set()
        self._json: str = ""
        self._delta = False
        self._stopped = False

    def receive(self, event: ParsedMessageStreamEvent[None]) -> None:
        # These public events are SDK-derived views of already processed raw deltas.
        if event.type in {"text", "input_json", "thinking", "signature", "citation"}:
            return
        if self._stopped:
            raise ModelUnavailable(FailureKind.PROTOCOL)
        if event.type == "message_start":
            if self._envelope is not None or event.message.content:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            self._envelope = _OBJECT.validate_json(
                event.message.model_dump_json(exclude_unset=True)
            )
            MessagesUsage.model_validate(self._envelope.get("usage"), strict=True)
            return
        if self._envelope is None:
            raise ModelUnavailable(FailureKind.PROTOCOL)
        if (
            event.type == "content_block_start"
            or event.type == "content_block_delta"
            or event.type == "content_block_stop"
        ):
            _INDEX.validate_python(event.index, strict=True)
        if event.type == "content_block_start":
            if self._open is not None or self._delta or event.index != len(self._blocks):
                raise ModelUnavailable(FailureKind.PROTOCOL)
            block = _OBJECT.validate_json(event.content_block.model_dump_json(exclude_unset=True))
            if block.get("type") not in {"text", "thinking", "redacted_thinking", "tool_use"}:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            _BLOCK.validate_json(json.dumps(block), strict=True)
            self._blocks.append(block)
            self._open = event.index
            self._fields = set()
            self._json = ""
        elif event.type == "content_block_delta":
            if self._open != event.index or self._open is None:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            kind = self._blocks[self._open]["type"]
            delta = _DELTA.validate_json(
                event.delta.model_dump_json(exclude_unset=True), strict=True
            )
            match delta.type:
                case "text_delta":
                    expected, field = "text", "text"
                case "citations_delta":
                    expected, field = "text", "citations"
                case "thinking_delta":
                    expected, field = "thinking", "thinking"
                case "signature_delta":
                    expected, field = "thinking", "signature"
                case "input_json_delta":
                    expected, field = "tool_use", "input"
                    self._json += delta.partial_json
                case _:
                    assert_never(delta)
            if kind != expected:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            self._fields.add(field)
        elif event.type == "content_block_stop":
            if self._open is None or event.index != self._open:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            block = self._blocks[self._open]
            snapshot = _OBJECT.validate_json(
                event.content_block.model_dump_json(exclude_unset=True)
            )
            if "input" in self._fields:
                complete_input = _OBJECT.validate_python(
                    json.loads(self._json, object_pairs_hook=_unique_fields), strict=True
                )
                if snapshot.get("input") != complete_input:
                    raise ModelUnavailable(FailureKind.PROTOCOL)
            for field in self._fields:
                block[field] = snapshot[field]
            self._open = None
        elif event.type == "message_delta":
            if self._open is not None:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            delta = _OBJECT.validate_json(event.delta.model_dump_json(exclude_unset=True))
            if delta.get("stop_reason") in {"max_tokens", "model_context_window_exceeded"}:
                raise ModelUnavailable(FailureKind.TRUNCATED)
            usage = _OBJECT.validate_json(event.usage.model_dump_json(exclude_unset=True))
            MessagesUsage.model_validate(usage, strict=True)
            retained = self._envelope["usage"]
            assert isinstance(retained, dict)
            for name, value in usage.items():
                if value is None and name in {
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "service_tier",
                }:
                    continue
                retained[name] = value
            self._envelope.update(delta)
            self._delta = True
        elif event.type == "message_stop":
            if self._open is not None or not self._delta:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            self._stopped = True
        else:
            raise ModelUnavailable(FailureKind.PROTOCOL)

    def finish(self) -> MessagesReply:
        if not self._stopped or self._envelope is None:
            raise ModelUnavailable(FailureKind.INCOMPLETE_EOF)
        reason = self._envelope.get("stop_reason")
        if reason in {"max_tokens", "model_context_window_exceeded"}:
            raise ModelUnavailable(FailureKind.TRUNCATED)
        if reason not in {"end_turn", "tool_use"}:
            raise ModelUnavailable(FailureKind.PROTOCOL)
        self._envelope["content"] = _VALUE.validate_python(self._blocks, strict=True)
        return MessagesReply.from_json(json.dumps(self._envelope, ensure_ascii=False))


class MessagesAdapter:
    def __init__(
        self, configuration: ModelConfiguration, transport: httpx.AsyncBaseTransport | None
    ) -> None:
        self._configuration = configuration
        self._sdk, self._headers = anthropic_client(configuration, transport)

    @staticmethod
    def request_options(configuration: ModelConfiguration) -> dict[str, JsonValue]:
        options: dict[str, JsonValue] = {}
        try:
            if configuration.reasoning_effort:
                options["output_config"] = {
                    "effort": _EFFORT.validate_python(configuration.reasoning_effort, strict=True)
                }
            if configuration.service_tier:
                options["service_tier"] = _TIER.validate_python(
                    configuration.service_tier, strict=True
                )
        except ValueError:
            raise ModelUnavailable(FailureKind.CONFIGURATION) from None
        return options

    async def close(self) -> None:
        await self._sdk.close()

    def prepare(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> dict[str, JsonValue]:
        system, messages = _encode(input)
        body: dict[str, JsonValue] = {
            "model": self._configuration.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": _VALUE.validate_python(messages),
            "system": _VALUE.validate_python(system),
            "tools": _VALUE.validate_python(
                [
                    {
                        "name": tool.name,
                        "description": tool.description.body,
                        "input_schema": _input_schema(tool),
                        "strict": False,
                    }
                    for tool in tools
                ]
            ),
            "cache_control": {"type": "ephemeral"},
            "stream": True,
        }
        body.update(self.request_options(self._configuration))
        _TOOLS.validate_python(body["tools"], strict=True)
        return body

    async def complete(
        self,
        body: dict[str, JsonValue],
        *,
        evidence: ModelAttemptSink | None = None,
    ) -> ModelReply:
        if evidence is not None:
            evidence.encoded(body)
        completed = _MessageStream()
        reply: ModelReply | None = None
        # Raw continuation fields and explicit nulls must bypass input TypedDict filtering.
        typed_messages: list[MessageParam] = []
        async with self._sdk.messages.stream(
            model=self._configuration.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=typed_messages,
            extra_body=body,
            extra_headers=self._headers,
        ) as stream:
            media_type = stream.response.headers.get("content-type", "").partition(";")[0]
            if media_type.strip().lower() != "text/event-stream":
                raise ModelUnavailable(FailureKind.PROTOCOL)
            try:
                async for event in stream:
                    completed.receive(event)
                    if event.type == "message_stop":
                        reply = self._reply(completed.finish())
                        if evidence is not None:
                            evidence.received(reply)
            except (AttributeError, IndexError, KeyError, TypeError, RuntimeError):
                raise ModelUnavailable(FailureKind.PROTOCOL) from None
        if reply is None:
            raise ModelUnavailable(FailureKind.INCOMPLETE_EOF)
        return reply

    def _reply(self, native: MessagesReply) -> ModelReply:
        message = native.message
        usage = message.usage
        return ModelReply(
            AssistantMessage(native),
            Usage(
                usage.total_input,
                usage.output_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
            ),
            ModelMetadata(
                self._configuration.model,
                self._configuration.reasoning_effort,
                message.model,
                usage.service_tier,
            ),
        )
