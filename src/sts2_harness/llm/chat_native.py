"""Immutable Chat Completions output with concrete protocol-owned validation."""

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from openai.types.chat.chat_completion import Choice
from pydantic import BaseModel, ConfigDict, Field

from sts2_harness.config import Protocol


class CacheUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    cached_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_write_tokens: Annotated[int, Field(ge=0)] | None = None


class ChatUsage(BaseModel):
    model_config = ConfigDict(strict=True)
    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    prompt_tokens_details: CacheUsage | None = None


class NativeChatCompletion(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: tuple[Choice, ...]
    usage: ChatUsage | None = None
    service_tier: str | None = None


@dataclass(frozen=True, slots=True)
class ChatCompletionsReply:
    """Keep the entire native completion; callers only receive decoded copies."""

    _encoded: str

    @property
    def protocol(self) -> Protocol:
        return Protocol.CHAT_COMPLETIONS

    @property
    def completion(self) -> NativeChatCompletion:
        return NativeChatCompletion.model_validate_json(self._encoded, strict=True)

    def to_json(self) -> str:
        return self._encoded

    @classmethod
    def from_json(cls, payload: str) -> Self:
        completion = NativeChatCompletion.model_validate_json(payload, strict=True)
        if len(completion.choices) != 1 or completion.choices[0].index != 0:
            raise ValueError("Chat Completions requires exactly one choice.")
        choice = completion.choices[0]
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise ValueError("Chat Completions requires a complete supported response.")
        message = choice.message
        if message.audio is not None or message.function_call is not None or message.refusal:
            raise ValueError("Chat Completions returned unsupported output.")
        if choice.finish_reason == "tool_calls" and not message.tool_calls:
            raise ValueError("Chat Completions returned no calls for a tool response.")
        if any(call.type != "function" for call in message.tool_calls or ()):
            raise ValueError("Chat Completions returned unsupported tool calls.")
        return cls(payload)
