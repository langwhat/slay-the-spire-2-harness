"""Recorded input accounting policy and deterministic request measurement."""

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from sts2_harness.client import models as native
from sts2_harness.config import Protocol
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
    estimator_id: Literal["wire_tiktoken_max_v1"] = "wire_tiktoken_max_v1"
    tokenizer_dependency: Literal["tiktoken==0.12.0"] = "tiktoken==0.12.0"
    cl100k_base_sha256: Literal[
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ] = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    o200k_base_sha256: Literal[
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    ] = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
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
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    tokens = max(len(encoding.encode_ordinary(text)) for encoding in encodings())
    return Measurement(sha256(text.encode()).hexdigest(), tokens + 1024 + 64 * count)
