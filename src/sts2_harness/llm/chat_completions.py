"""Official Chat Completions SDK with complete native continuation."""

import json
from hashlib import sha256
from typing import Literal, assert_never

import httpx2 as httpx
from openai import Omit, omit
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletionChunk, ChatCompletionToolUnionParam
from openai.types.shared import ReasoningEffort
from pydantic import JsonValue, TypeAdapter

from sts2_harness.config import ModelConfiguration
from sts2_harness.llm.chat_native import ChatCompletionsReply, ChatUsage
from sts2_harness.llm.messages import (
    AssistantMessage,
    FailureKind,
    ModelAttemptSink,
    ModelInput,
    ModelMetadata,
    ModelReply,
    ModelUnavailable,
    TextMessage,
    ToolDefinition,
    ToolResultMessage,
    Usage,
)
from sts2_harness.llm.openai_boundary import openai_client

_OBJECT = TypeAdapter(dict[str, JsonValue])
_TOOLS = TypeAdapter(list[ChatCompletionToolUnionParam])
_REASONING = TypeAdapter(ReasoningEffort)
_TIER = TypeAdapter(Literal["auto", "default", "flex", "scale", "priority", "fast"])


def _encode(input: ModelInput) -> list[dict[str, JsonValue]]:
    encoded: list[dict[str, JsonValue]] = []
    for message in input:
        match message:
            case TextMessage():
                encoded.append({"role": message.role.value, "content": message.text})
            case ToolResultMessage(call_id, content):
                encoded.append(
                    {"role": "tool", "tool_call_id": call_id, "content": content.to_json()}
                )
            case AssistantMessage(native):
                if not isinstance(native, ChatCompletionsReply):
                    raise ModelUnavailable(FailureKind.CONFIGURATION)
                choice = native.completion.choices[0]
                encoded.append(
                    _OBJECT.validate_json(choice.message.model_dump_json(exclude_unset=True))
                )
            case _:
                assert_never(message)
    return encoded


class _CompletionStream:
    """Use SDK delta assembly while retaining only actual protocol fields."""

    def __init__(self) -> None:
        self._state: ChatCompletionStreamState[object] = ChatCompletionStreamState()
        self._envelope: dict[str, JsonValue] = {}
        self._choice_extras: dict[str, JsonValue] = {}
        self._finished = False
        self._seen = False
        self._message_fields: set[str] = set()
        self._tool_fields: dict[int, set[str]] = {}
        self._function_fields: dict[int, set[str]] = {}
        self._provider_parsed: dict[str, JsonValue] = {}
        self._provider_arguments: dict[int, JsonValue] = {}

    def receive(self, chunk: ChatCompletionChunk) -> ChatCompletionsReply | None:
        raw = _OBJECT.validate_json(chunk.model_dump_json(exclude_unset=True))
        # Usage is intentionally validated separately: proxies may omit individual counts.
        usage = raw.get("usage")
        if usage is not None:
            ChatUsage.model_validate(usage, strict=True)
        validation = {name: value for name, value in raw.items() if name != "usage"}
        validated = ChatCompletionChunk.model_validate(validation, strict=True)
        if len(validated.choices) > 1 or any(choice.index != 0 for choice in validated.choices):
            raise ModelUnavailable(FailureKind.PROTOCOL)
        if self._seen and any(
            raw.get(name) != self._envelope.get(name) for name in ("id", "created", "model")
        ):
            raise ModelUnavailable(FailureKind.PROTOCOL)
        for choice in validated.choices:
            if self._finished:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            raw_choice = _OBJECT.validate_json(choice.model_dump_json(exclude_unset=True))
            self._choice_extras.update(
                (name, raw_choice[name]) for name in choice.model_extra or {}
            )
            delta = choice.delta
            raw_delta = _OBJECT.validate_json(delta.model_dump_json(exclude_unset=True))
            if "parsed" in raw_delta:
                self._provider_parsed["parsed"] = raw_delta["parsed"]
            self._message_fields.update(delta.model_fields_set)
            self._message_fields.update(delta.model_extra or {})
            for call in delta.tool_calls or ():
                if call.index < 0 or call.index > len(self._tool_fields):
                    raise ModelUnavailable(FailureKind.PROTOCOL)
                if call.index not in self._tool_fields and (
                    call.type != "function" or call.function is None
                ):
                    raise ModelUnavailable(FailureKind.PROTOCOL)
                if call.index in self._tool_fields and (
                    call.id is not None
                    or (call.function is not None and call.function.name is not None)
                ):
                    raise ModelUnavailable(FailureKind.PROTOCOL)
                self._tool_fields.setdefault(call.index, set()).update(call.model_fields_set)
                self._tool_fields[call.index].update(call.model_extra or {})
                if call.function is not None:
                    raw_function = _OBJECT.validate_json(
                        call.function.model_dump_json(exclude_unset=True)
                    )
                    if "parsed_arguments" in raw_function:
                        self._provider_arguments[call.index] = raw_function["parsed_arguments"]
                    fields = self._function_fields.setdefault(call.index, set())
                    fields.update(call.function.model_fields_set)
                    fields.update(call.function.model_extra or {})
            reason = choice.finish_reason
            if reason == "length":
                raise ModelUnavailable(FailureKind.TRUNCATED)
            if reason not in {None, "stop", "tool_calls"}:
                raise ModelUnavailable(FailureKind.PROTOCOL)
            self._finished = reason is not None
        self._envelope.update(
            (name, value)
            for name, value in raw.items()
            if name not in {"choices", "object", "obfuscation"}
        )
        try:
            self._state.handle_chunk(validated)
        except (TypeError, RuntimeError):
            raise ModelUnavailable(FailureKind.PROTOCOL) from None
        self._seen = True
        return self.finish() if self._finished else None

    def finish(self) -> ChatCompletionsReply:
        if not self._finished:
            raise ModelUnavailable(FailureKind.INCOMPLETE_EOF)
        # Snapshot avoids get_final_completion's parsed/parsed_arguments synthesis.
        snapshot = _OBJECT.validate_json(
            self._state.current_completion_snapshot.model_dump_json(exclude_unset=True)
        )
        choices = snapshot["choices"]
        assert isinstance(choices, list)
        for choice in choices:
            assert isinstance(choice, dict)
            choice.update(self._choice_extras)
            message = choice["message"]
            assert isinstance(message, dict)
            for name in tuple(message):
                if name not in self._message_fields:
                    del message[name]
            message.update(self._provider_parsed)
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for index, call in enumerate(calls):
                    assert isinstance(call, dict)
                    for name in tuple(call):
                        # Streaming indices route deltas; the SDK also synthesizes fields.
                        if name == "index" or name not in self._tool_fields[index]:
                            del call[name]
                    function = call.get("function")
                    if isinstance(function, dict):
                        for name in tuple(function):
                            if name not in self._function_fields.get(index, set()):
                                del function[name]
                        if index in self._provider_arguments:
                            function["parsed_arguments"] = self._provider_arguments[index]
        return ChatCompletionsReply.from_json(
            json.dumps(
                {**self._envelope, "object": "chat.completion", "choices": choices},
                ensure_ascii=False,
            )
        )


class ChatCompletionsAdapter:
    def __init__(
        self, configuration: ModelConfiguration, transport: httpx.AsyncBaseTransport | None
    ) -> None:
        self._configuration = configuration
        self._sdk, self._headers = openai_client(configuration, transport)

    async def close(self) -> None:
        await self._sdk.close()

    async def complete(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        evidence: ModelAttemptSink | None = None,
    ) -> ModelReply:
        try:
            effort = (
                _REASONING.validate_python(self._configuration.reasoning_effort, strict=True)
                if self._configuration.reasoning_effort
                else omit
            )
            tier = (
                _TIER.validate_python(self._configuration.service_tier, strict=True)
                if self._configuration.service_tier
                else omit
            )
        except ValueError:
            raise ModelUnavailable(FailureKind.CONFIGURATION) from None
        messages = _encode(input)
        encoded_tools = _TOOLS.validate_json(
            json.dumps(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description.body,
                            "parameters": tool.parameters.model_json_schema(),
                            "strict": False,
                        },
                    }
                    for tool in tools
                ]
            ),
            strict=True,
        )
        if evidence is not None:
            encoding: dict[str, JsonValue] = {
                "model": self._configuration.model,
                "messages": TypeAdapter(JsonValue).validate_python(messages),
                "tools": TypeAdapter(JsonValue).validate_python(encoded_tools),
                "store": False,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if not isinstance(effort, Omit):
                encoding["reasoning_effort"] = effort
            if not isinstance(tier, Omit):
                encoding["service_tier"] = tier
            evidence.encoded(
                sha256(
                    json.dumps(
                        encoding, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
            )
        stream = await self._sdk.chat.completions.create(
            model=self._configuration.model,
            messages=[],
            # SDK input TypedDicts omit actual opaque continuation fields and nulls.
            extra_body={"messages": messages},
            tools=encoded_tools,
            store=False,
            stream=True,
            stream_options={"include_usage": True},
            reasoning_effort=effort,
            service_tier=tier,
            extra_headers=self._headers,
        )
        completed = _CompletionStream()
        reply: ModelReply | None = None
        async with stream:
            media_type = stream.response.headers.get("content-type", "").partition(";")[0]
            if media_type.strip().lower() != "text/event-stream":
                raise ModelUnavailable(FailureKind.PROTOCOL)
            async for chunk in stream:
                native = completed.receive(chunk)
                if native is not None:
                    reply = self._reply(native)
                    if evidence is not None:
                        evidence.received(reply)
        if reply is None:
            raise ModelUnavailable(FailureKind.INCOMPLETE_EOF)
        return reply

    def _reply(self, native: ChatCompletionsReply) -> ModelReply:
        completion = native.completion
        usage = completion.usage or ChatUsage()
        details = usage.prompt_tokens_details
        return ModelReply(
            AssistantMessage(native),
            Usage(
                usage.prompt_tokens,
                usage.completion_tokens,
                details.cached_tokens if details else None,
                details.cache_write_tokens if details else None,
            ),
            ModelMetadata(
                self._configuration.model,
                self._configuration.reasoning_effort,
                completion.model,
                completion.service_tier,
            ),
        )
