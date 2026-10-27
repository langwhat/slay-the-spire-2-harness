"""Official Anthropic transport with project-owned authentication and errors."""

import os
from base64 import b64encode
from typing import assert_never

import httpx2 as httpx
from anthropic import AsyncAnthropic, Omit, omit
from pydantic import BaseModel, ConfigDict, ValidationError

from sts2_harness.config import AuthKind, ModelConfiguration
from sts2_harness.llm.messages import FailureKind


class ProviderError(BaseModel):
    model_config = ConfigDict(strict=True)
    type: str | None = None


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(strict=True)
    error: ProviderError | None = None


def classify_error(body: object, status: int | None = None) -> FailureKind:
    try:
        envelope = (
            ErrorEnvelope.model_validate(body, strict=True) if isinstance(body, dict) else None
        )
    except ValidationError:
        if status is None or status == 200:
            return FailureKind.PROTOCOL
        envelope = None
    code = envelope.error.type if envelope is not None and envelope.error is not None else None
    if code == "billing_error":
        return FailureKind.QUOTA
    if status == 401 or code == "authentication_error":
        return FailureKind.AUTHENTICATION
    if status == 403 or code == "permission_error":
        return FailureKind.PERMISSION
    if status == 404 or code == "not_found_error":
        return FailureKind.MODEL
    if status == 408:
        return FailureKind.TIMEOUT
    if status == 429 or code == "rate_limit_error":
        return FailureKind.RATE_LIMIT
    if (status is not None and status >= 500) or code in {"overloaded_error", "api_error"}:
        return FailureKind.OVERLOAD
    if status in {400, 413, 422} or code in {"invalid_request_error", "request_too_large"}:
        return FailureKind.CONFIGURATION
    return FailureKind.PROVIDER


def anthropic_client(
    configuration: ModelConfiguration, transport: httpx.AsyncBaseTransport | None
) -> tuple[AsyncAnthropic, dict[str, str | Omit]]:
    headers: dict[str, str | Omit] = {
        line.partition(":")[0].strip().lower(): omit
        for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
        if ":" in line
    }
    headers.update({"authorization": omit, "x-api-key": omit})
    auth = configuration.authentication
    match auth.kind:
        case AuthKind.BEARER:
            headers["authorization"] = f"Bearer {auth.secret}"
        case AuthKind.API_KEY:
            headers[auth.header.lower()] = auth.secret
        case AuthKind.BASIC:
            token = b64encode(f"{auth.username}:{auth.secret}".encode()).decode("ascii")
            headers["authorization"] = f"Basic {token}"
        case AuthKind.NONE:
            pass
        case _:
            assert_never(auth.kind)
    headers.update((name.lower(), value) for name, value in configuration.custom_headers)
    sdk = AsyncAnthropic(
        api_key="unused",
        webhook_key="",
        base_url=configuration.endpoint,
        timeout=None,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport, timeout=None),
    )
    return sdk, headers
