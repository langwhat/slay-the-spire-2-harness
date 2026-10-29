"""Shared official OpenAI SDK transport and structured error classification."""

import os
from base64 import b64encode
from typing import assert_never

import httpx2 as httpx
from openai import AsyncOpenAI, Omit, omit

from sts2_harness.config import AuthKind, ModelConfiguration
from sts2_harness.llm.messages import FailureKind


def openai_client(
    configuration: ModelConfiguration, transport: httpx.AsyncBaseTransport | None
) -> tuple[AsyncOpenAI, dict[str, str | Omit]]:
    # Neutralize the SDK's ambient custom headers before applying project configuration.
    headers: dict[str, str | Omit] = {
        line.partition(":")[0].strip().lower(): omit
        for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").splitlines()
        if ":" in line
    }
    headers.update(
        {
            "authorization": omit,
            "openai-organization": omit,
            "openai-project": omit,
        }
    )
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
    sdk = AsyncOpenAI(
        api_key="unused",
        admin_api_key="",
        organization="",
        project="",
        base_url=configuration.endpoint,
        max_retries=0,
        timeout=None,
        http_client=httpx.AsyncClient(transport=transport, timeout=None),
    )
    return sdk, headers


def classify_error(code: str | None, status: int | None = None) -> FailureKind:
    """Use structured provider codes and HTTP status, never free-form error text."""
    if status == 401:
        return FailureKind.AUTHENTICATION
    if status == 403:
        return FailureKind.PERMISSION
    if code in {"insufficient_quota", "quota_exceeded", "billing_hard_limit_reached"}:
        return FailureKind.QUOTA
    if code in {"model_not_found", "invalid_model"} or status == 404:
        return FailureKind.MODEL
    if status == 408:
        return FailureKind.TIMEOUT
    if status == 429 or code in {"rate_limit_exceeded", "rate_limit"}:
        return FailureKind.RATE_LIMIT
    if (status is not None and status >= 500) or code in {
        "server_error",
        "overloaded",
        "overloaded_error",
        "engine_overloaded",
    }:
        return FailureKind.OVERLOAD
    if status in {400, 413, 422} or code in {
        "invalid_request_error",
        "unsupported_parameter",
        "unsupported_value",
        "context_length_exceeded",
    }:
        return FailureKind.CONFIGURATION
    return FailureKind.PROVIDER
