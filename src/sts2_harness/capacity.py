"""Recorded input accounting policy and deterministic request measurement."""

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from sts2_harness.client import models as native
from sts2_harness.config import ModelConfiguration, Protocol
from sts2_harness.tokenizer import encodings


class CompactionDecision(StrEnum):
    UNCHANGED = "unchanged"
    COMPACTED = "compacted"
    MINIMUM_EXCEEDS_WINDOW = "minimum_exceeds_window"
    INVALID_CANDIDATE = "invalid_candidate"


class CompactionValidation(StrEnum):
    NOT_NEEDED = "not_needed"
    PASSED = "passed"
    FAILED = "failed"


class RecoveryComparison(StrEnum):
    STRICTLY_SMALLER = "strictly_smaller"
    UNCHANGED = "unchanged"
    INVALID = "invalid"
    OVER_WINDOW = "over_window"


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
    estimator_id: Literal["input_tiktoken_max_v2"] = "input_tiktoken_max_v2"
    tokenizer_dependency: Literal["tiktoken==0.12.0"] = "tiktoken==0.12.0"
    cl100k_base_sha256: Literal[
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ] = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    o200k_base_sha256: Literal[
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    ] = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
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


def _input_structure(value: JsonValue, strings: list[str]) -> JsonValue:
    if isinstance(value, str):
        strings.append(value)
        return ""
    if isinstance(value, list):
        return [_input_structure(item, strings) for item in value]
    if isinstance(value, dict):
        return {key: _input_structure(item, strings) for key, item in value.items()}
    return value


def measure(body: dict[str, JsonValue], protocol: Protocol) -> Measurement:
    names = (
        ("messages", "tools")
        if protocol is Protocol.CHAT_COMPLETIONS
        else (
            ("input", "tools")
            if protocol is Protocol.RESPONSES
            else ("system", "messages", "tools")
        )
    )
    input_fields: dict[str, JsonValue] = {}
    for name in names:
        value = body.get(name)
        if not isinstance(value, list):
            raise ValueError("Expected an array in the encoded model request.")
        input_fields[name] = value
    strings: list[str] = []
    structure = json.dumps(
        _input_structure(input_fields, strings),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # Count decoded strings once, including opaque continuation, without HTTP escaping.
    tokens = max(
        len(encoding.encode_ordinary(structure))
        + sum(len(encoding.encode_ordinary(value)) for value in strings)
        for encoding in encodings()
    )
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = text.encode()
    return Measurement(sha256(encoded).hexdigest(), tokens + 1024, len(encoded))
