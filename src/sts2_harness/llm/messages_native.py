"""Immutable Messages continuation with concrete protocol-owned validation."""

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from anthropic.types import DirectCaller, RedactedThinkingBlock, TextBlock, ThinkingBlock
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sts2_harness.config import Protocol


class NativeToolUse(BaseModel):
    model_config = ConfigDict(strict=True)
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, JsonValue]
    caller: DirectCaller | None = None


type NativeBlock = Annotated[
    TextBlock | ThinkingBlock | RedactedThinkingBlock | NativeToolUse, Field(discriminator="type")
]
type TokenCount = Annotated[int, Field(ge=0)]


class MessagesUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    cache_creation_input_tokens: TokenCount | None = None
    cache_read_input_tokens: TokenCount | None = None
    service_tier: str | None = None

    @property
    def total_input(self) -> int | None:
        counts = (self.input_tokens, self.cache_creation_input_tokens, self.cache_read_input_tokens)
        if any(count is None for count in counts):
            return None
        return sum(count for count in counts if count is not None)


class NativeMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    model: str
    content: tuple[NativeBlock, ...]
    stop_reason: Literal["end_turn", "tool_use"]
    usage: MessagesUsage


@dataclass(frozen=True, slots=True)
class MessagesReply:
    """Retain the entire native envelope and return only fresh decoded views."""

    _encoded: str

    @property
    def protocol(self) -> Protocol:
        return Protocol.MESSAGES

    @property
    def message(self) -> NativeMessage:
        return NativeMessage.model_validate_json(self._encoded, strict=True)

    def to_json(self) -> str:
        return self._encoded

    @classmethod
    def from_json(cls, payload: str) -> Self:
        NativeMessage.model_validate_json(payload, strict=True)
        return cls(payload)
