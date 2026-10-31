"""Recorded input accounting policy and deterministic request measurement."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from sts2_harness.client import models as native
from sts2_harness.config import Protocol


class CapacityClassifierConfig(native.WireModel):
    protocols: tuple[Protocol, ...] = (
        Protocol.RESPONSES,
        Protocol.CHAT_COMPLETIONS,
        Protocol.MESSAGES,
    )
    endpoint_category: Literal["unverified"] = "unverified"
    enabled_signals: tuple[str, ...] = Field(default=(), max_length=0)


class CapacityPolicy(native.WireModel):
    context_window: Annotated[int, Field(gt=0, strict=True)] = 300_000
    trigger_numerator: Literal[9] = 9
    trigger_denominator: Literal[10] = 10
    estimator_id: Literal["wire_utf8_conservative_v1"] = "wire_utf8_conservative_v1"
    compaction_policy_id: Literal["latest_complete_group_v1"] = "latest_complete_group_v1"
    capacity_classifier_id: Literal["no_verified_signals_v1"] = "no_verified_signals_v1"
    capacity_classifier_config: CapacityClassifierConfig = CapacityClassifierConfig()


@dataclass(frozen=True, slots=True)
class Measurement:
    digest: str
    estimated_input_tokens: int


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
    count = 0
    for name in names:
        value = body.get(name)
        if not isinstance(value, list):
            raise ValueError("Expected an array in the encoded model request.")
        count += len(value)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return Measurement(sha256(encoded).hexdigest(), len(encoded) + 1024 + 64 * count)
