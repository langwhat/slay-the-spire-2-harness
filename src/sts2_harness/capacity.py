"""Recorded input accounting policy and deterministic request measurement."""

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from sts2_harness.client import models as native
from sts2_harness.config import ModelConfiguration, Protocol


class CompactionDecision(StrEnum):
    UNCHANGED = "unchanged"
    COMPACTED = "compacted"
    INVALID_CANDIDATE = "invalid_candidate"


class CompactionValidation(StrEnum):
    NOT_NEEDED = "not_needed"
    PASSED = "passed"
    FAILED = "failed"


class RecoveryComparison(StrEnum):
    STRICTLY_SMALLER = "strictly_smaller"
    UNCHANGED = "unchanged"
    INVALID = "invalid"


class RecoveryDecision(StrEnum):
    RETRY = "retry"
    INTERRUPT = "interrupt"


class ModelAttemptReference(native.WireModel):
    sequence: Annotated[int, Field(gt=0)]
    attempt: Annotated[int, Field(gt=0)]


class CapacitySignal(StrEnum):
    CONTEXT_TOO_LARGE = "context_too_large"


class CapacityEndpointCategory(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED_RESPONSES = "verified_responses"


class CapacityClassifierId(StrEnum):
    UNVERIFIED = "no_verified_signals_v1"
    VERIFIED_RESPONSES = "responses_context_too_large_v1"


class CapacityClassifierConfig(native.WireModel):
    protocols: tuple[Protocol, ...] = (
        Protocol.RESPONSES,
        Protocol.CHAT_COMPLETIONS,
        Protocol.MESSAGES,
    )
    endpoint_category: CapacityEndpointCategory = CapacityEndpointCategory.UNVERIFIED
    enabled_signals: tuple[CapacitySignal, ...] = ()

    @model_validator(mode="after")
    def verified_shape(self) -> Self:
        if self.endpoint_category is CapacityEndpointCategory.UNVERIFIED:
            if self.enabled_signals:
                raise ValueError("Unverified endpoints cannot enable capacity signals.")
        elif self.protocols != (Protocol.RESPONSES,) or self.enabled_signals != (
            CapacitySignal.CONTEXT_TOO_LARGE,
        ):
            raise ValueError(
                "Verified Responses capacity classification requires its observed signal."
            )
        return self

    def supports(self, protocol: Protocol, signal: str) -> bool:
        return protocol in self.protocols and signal in self.enabled_signals


class CapacityPolicy(native.WireModel):
    context_window: Annotated[int, Field(gt=0, strict=True)] = 300_000
    trigger_numerator: Literal[9] = 9
    trigger_denominator: Literal[10] = 10
    estimator_id: Literal["input_characters_v1", "reported_usage_bytes_v1"] = (
        "reported_usage_bytes_v1"
    )
    compaction_policy_id: Literal["latest_complete_group_v1"] = "latest_complete_group_v1"
    capacity_classifier_id: CapacityClassifierId = CapacityClassifierId.UNVERIFIED
    capacity_classifier_config: CapacityClassifierConfig = CapacityClassifierConfig()

    @model_validator(mode="after")
    def classifier_identity(self) -> Self:
        verified = (
            self.capacity_classifier_config.endpoint_category
            is CapacityEndpointCategory.VERIFIED_RESPONSES
        )
        expected = (
            CapacityClassifierId.VERIFIED_RESPONSES if verified else CapacityClassifierId.UNVERIFIED
        )
        if self.capacity_classifier_id is not expected:
            raise ValueError("Capacity classifier identity does not match its configuration.")
        return self

    @classmethod
    def for_configuration(cls, config: ModelConfiguration) -> Self:
        if (
            config.protocol is not Protocol.RESPONSES
            or not config.capacity_verified_endpoint
            or config.capacity_verified_endpoint != config.endpoint
        ):
            return cls(context_window=config.context_window)
        return cls(
            context_window=config.context_window,
            capacity_classifier_id=CapacityClassifierId.VERIFIED_RESPONSES,
            capacity_classifier_config=CapacityClassifierConfig(
                protocols=(Protocol.RESPONSES,),
                endpoint_category=CapacityEndpointCategory.VERIFIED_RESPONSES,
                enabled_signals=(CapacitySignal.CONTEXT_TOO_LARGE,),
            ),
        )


@dataclass(frozen=True, slots=True)
class Measurement:
    digest: str
    estimated_input_tokens: int
    encoded_bytes: int


def _request_field_names(protocol: Protocol) -> tuple[str, ...]:
    return (
        ("messages", "tools")
        if protocol is Protocol.CHAT_COMPLETIONS
        else (
            ("input", "tools")
            if protocol is Protocol.RESPONSES
            else ("system", "messages", "tools")
        )
    )


def _request_fields(
    body: dict[str, JsonValue], names: tuple[str, ...]
) -> dict[str, list[JsonValue]]:
    fields: dict[str, list[JsonValue]] = {}
    for name in names:
        value = body.get(name)
        if not isinstance(value, list):
            raise ValueError("Expected an array in the encoded model request.")
        fields[name] = value
    return fields


def _json_bytes(value: JsonValue) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _incremental_bytes(
    current: dict[str, list[JsonValue]],
    previous: dict[str, list[JsonValue]],
    assistant: dict[str, list[JsonValue]],
) -> int | None:
    if current.keys() != previous.keys() or current.keys() != assistant.keys():
        return None

    added_bytes = 0
    for name, current_items in current.items():
        previous_items = previous[name]
        if name == "tools":
            if current_items != previous_items:
                return None
            continue
        if (
            len(previous_items) > len(current_items)
            or current_items[: len(previous_items)] != previous_items
        ):
            return None

        appended = current_items[len(previous_items) :]
        assistant_items = assistant[name]
        if assistant_items and appended[: len(assistant_items)] == assistant_items:
            appended = appended[len(assistant_items) :]
            preceding_items = len(previous_items) + len(assistant_items)
        else:
            preceding_items = len(previous_items)
        if not appended:
            continue

        added_bytes += sum(len(_json_bytes(item)) for item in appended)
        added_bytes += len(appended) - 1
        if preceding_items:
            added_bytes += 1
    return added_bytes


def measure(
    body: dict[str, JsonValue],
    protocol: Protocol,
    *,
    previous_body: dict[str, JsonValue] | None = None,
    assistant_body: dict[str, JsonValue] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> Measurement:
    names = _request_field_names(protocol)
    input_fields = _request_fields(body, names)
    input_json: dict[str, JsonValue] = {name: items for name, items in input_fields.items()}
    if (
        previous_body is not None
        and assistant_body is not None
        and input_tokens is not None
        and output_tokens is not None
    ):
        previous_fields = _request_fields(previous_body, names)
        assistant_fields = _request_fields(assistant_body, names)
        additional_bytes = _incremental_bytes(input_fields, previous_fields, assistant_fields)
        if additional_bytes is not None:
            tokens = input_tokens + output_tokens + (additional_bytes + 3) // 4
        else:
            tokens = (len(_json_bytes(input_json)) + 3) // 4
    else:
        tokens = (len(_json_bytes(input_json)) + 3) // 4
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = text.encode()
    return Measurement(sha256(encoded).hexdigest(), tokens, len(encoded))
