"""Immutable conversation values; native continuation stays inside the LLM boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Self

from openai.types.responses import ResponseOutputItem
from pydantic import BaseModel, JsonValue, TypeAdapter

from sts2_harness.capacity import (
    CapacityPolicy,
    CompactionDecision,
    CompactionValidation,
    Measurement,
    ModelAttemptReference,
    RecoveryComparison,
    RecoveryDecision,
)
from sts2_harness.config import Protocol as ModelProtocol
from sts2_harness.knowledge import GuidanceContent
from sts2_harness.llm.chat_native import ChatCompletionsReply
from sts2_harness.llm.messages_native import MessagesReply
from sts2_harness.material_text import MaterialParts, MaterialText

_NATIVE_OUTPUT = TypeAdapter(tuple[ResponseOutputItem, ...])


class TextRole(StrEnum):
    SYSTEM = "system"
    USER = "user"


@dataclass(frozen=True, slots=True)
class TextMessage:
    role: TextRole
    value: str | MaterialText | MaterialParts | GuidanceContent

    @property
    def text(self) -> str:
        return self.value if isinstance(self.value, str) else self.value.body


class StructuredText(Protocol):
    def to_json(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    call_id: str
    content: StructuredText
    text: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: MaterialText
    parameters: type[BaseModel]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ResponsesReply:
    """Complete ordered native output, including opaque reasoning and assistant phases."""

    _encoded: str

    @property
    def protocol(self) -> ModelProtocol:
        return ModelProtocol.RESPONSES

    @property
    def items(self) -> tuple[ResponseOutputItem, ...]:
        """Return decoded copies without exposing mutable retained continuation state."""
        return _NATIVE_OUTPUT.validate_json(self._encoded, strict=True)

    def to_json(self) -> str:
        return self._encoded

    @classmethod
    def from_items(cls, items: tuple[ResponseOutputItem, ...]) -> Self:
        return cls(
            _NATIVE_OUTPUT.dump_json(
                items, exclude_unset=True, by_alias=True, serialize_as_any=True
            ).decode()
        )

    @classmethod
    def from_json(cls, payload: str) -> Self:
        _NATIVE_OUTPUT.validate_json(payload, strict=True)
        return cls(payload)


type NativeReply = ResponsesReply | ChatCompletionsReply | MessagesReply


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    native: NativeReply

    @property
    def text(self) -> str:
        if isinstance(self.native, ChatCompletionsReply):
            return self.native.completion.choices[0].message.content or ""
        if isinstance(self.native, MessagesReply):
            return "".join(
                block.text for block in self.native.message.content if block.type == "text"
            )
        return "".join(
            content.text
            for item in self.native.items
            if item.type == "message"
            for content in item.content
            if content.type == "output_text"
        )

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        if isinstance(self.native, ChatCompletionsReply):
            return tuple(
                ToolCall(call.id, call.function.name, call.function.arguments)
                for call in self.native.completion.choices[0].message.tool_calls or ()
                if call.type == "function"
            )
        if isinstance(self.native, MessagesReply):
            return tuple(
                ToolCall(
                    block.id,
                    block.name,
                    json.dumps(block.input, ensure_ascii=False, separators=(",", ":")),
                )
                for block in self.native.message.content
                if block.type == "tool_use"
            )
        return tuple(
            ToolCall(item.call_id, item.name, item.arguments)
            for item in self.native.items
            if item.type == "function_call"
        )


type ModelInput = tuple[TextMessage | AssistantMessage | ToolResultMessage, ...]


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    input: ModelInput
    tools: tuple[ToolDefinition, ...]
    measurement: Measurement
    body: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class RequestPreparation:
    group_id: int
    baseline: TextMessage | None
    input_before: ModelInput
    estimate_before: int
    decision: CompactionDecision
    input_after: ModelInput | None
    estimate_after: int | None
    retained_complete_group_id: int | None
    active_group_id: int | None
    validation: CompactionValidation
    recovery_of: ModelAttemptReference | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @property
    def cache_hit_ratio(self) -> float | None:
        if self.input_tokens is None or self.input_tokens == 0 or self.cache_read_tokens is None:
            return None
        return self.cache_read_tokens / self.input_tokens

    @staticmethod
    def summarize_cache(samples: tuple[Usage, ...]) -> CacheUsageSummary:
        comparable = tuple(
            sample
            for sample in samples
            if sample.input_tokens is not None and sample.cache_read_tokens is not None
        )
        return CacheUsageSummary(
            len(samples),
            len(comparable),
            sum(sample.input_tokens for sample in comparable if sample.input_tokens is not None),
            sum(
                sample.cache_read_tokens
                for sample in comparable
                if sample.cache_read_tokens is not None
            ),
        )


@dataclass(frozen=True, slots=True)
class CacheUsageSummary:
    request_count: int
    comparable_request_count: int
    input_tokens: int
    cache_read_tokens: int

    @property
    def complete_coverage(self) -> bool:
        return self.request_count == self.comparable_request_count

    @property
    def cache_hit_ratio(self) -> float | None:
        if not self.comparable_request_count or not self.input_tokens:
            return None
        return self.cache_read_tokens / self.input_tokens


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    requested_model: str
    requested_effort: str
    response_model: str | None
    response_service_tier: str | None


@dataclass(frozen=True, slots=True)
class ModelReply:
    assistant: AssistantMessage
    usage: Usage
    metadata: ModelMetadata


class FailureKind(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate-limit"
    OVERLOAD = "overload"
    INCOMPLETE_EOF = "incomplete-eof"
    TRUNCATED = "truncated"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    QUOTA = "quota"
    MODEL = "model"
    CONFIGURATION = "configuration"
    PROTOCOL = "protocol"
    PROVIDER = "provider"
    CAPACITY = "capacity"


class ModelUnavailable(Exception):
    """A safe-to-display failure after LLM recovery, without provider payloads."""

    def __init__(self, reason: FailureKind) -> None:
        self.reason = reason
        super().__init__(f"Model request unavailable: {reason.value}.")


class CapacityRejected(ModelUnavailable):
    """A request was rejected by a verified structured capacity signal."""

    def __init__(self, signal: str, *, attempt: int) -> None:
        self.signal = signal
        self.attempt = attempt
        self.sequence: int | None = None
        super().__init__(FailureKind.CAPACITY)

    @property
    def reference(self) -> ModelAttemptReference | None:
        if self.sequence is None:
            return None
        return ModelAttemptReference(sequence=self.sequence, attempt=self.attempt)


@dataclass(frozen=True, slots=True)
class CapacityRecovery:
    rejected: CapacityRejected
    candidate: PreparedRequest | None
    comparison: RecoveryComparison
    decision: RecoveryDecision


class ModelClient(Protocol):
    @property
    def capacity_policy(self) -> CapacityPolicy: ...

    def prepare(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> PreparedRequest: ...

    async def record_recovery(self, recovery: CapacityRecovery) -> None: ...

    async def complete_prepared(
        self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
    ) -> ModelReply: ...

    async def interrupt_preparation(
        self,
        preparation: RequestPreparation,
        tools: tuple[ToolDefinition, ...],
        failure: ModelUnavailable,
    ) -> None: ...

    async def complete(
        self, input: ModelInput, tools: tuple[ToolDefinition, ...]
    ) -> ModelReply: ...


class ModelAttemptSink(Protocol):
    def started(self, index: int) -> None: ...
    def encoded(self, measurement: Measurement) -> None: ...
    def received(self, reply: ModelReply) -> None: ...
    def failed(self, reason: FailureKind, *, capacity_signal: str | None = None) -> None: ...
