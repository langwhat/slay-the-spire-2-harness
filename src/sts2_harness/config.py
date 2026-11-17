"""Read one immutable process configuration without changing environment variables."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from sts2_harness.model_purpose import ModelPurpose


class ConfigurationError(ValueError):
    """A safe-to-display configuration failure with no input values."""


class Driver(StrEnum):
    BUILTIN = "builtin"
    EXTERNAL = "external"


class Protocol(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat-completions"
    MESSAGES = "messages"


class AuthKind(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY = "api-key"
    BASIC = "basic"


@dataclass(frozen=True)
class Authentication:
    kind: AuthKind
    secret: str = field(repr=False)
    header: str = field(repr=False)
    username: str = field(repr=False)


@dataclass(frozen=True)
class ModelConfiguration:
    protocol: Protocol
    model: str = field(repr=False)
    endpoint: str = field(repr=False)
    reasoning_effort: str = field(repr=False)
    service_tier: str = field(repr=False)
    authentication: Authentication
    custom_headers: tuple[tuple[str, str], ...] = field(repr=False)
    timeout_seconds: float
    extra_retries: int
    context_window: int = 300_000
    capacity_verified_endpoint: str = field(default="", repr=False)
    reasoning_effort_run: str = field(default="", repr=False)
    reasoning_effort_deck: str = field(default="", repr=False)
    reasoning_effort_combat: str = field(default="", repr=False)
    prompt_cache_key: str | None = field(default=None, repr=False)

    def for_purpose(self, purpose: ModelPurpose) -> "ModelConfiguration":
        effort = {
            ModelPurpose.RUN: self.reasoning_effort_run or self.reasoning_effort,
            ModelPurpose.DECK: self.reasoning_effort_deck or self.reasoning_effort,
            ModelPurpose.COMBAT: self.reasoning_effort_combat or self.reasoning_effort,
        }[purpose]
        return replace(self, reasoning_effort=effort)


@dataclass(frozen=True)
class Configuration:
    driver: Driver
    model: ModelConfiguration | None
    user_input: bool = False
    minimal: bool = False


_KEYS = frozenset(
    {
        "DRIVER",
        "PROTOCOL",
        "MODEL",
        "BASE_URL",
        "REASONING_EFFORT",
        "REASONING_EFFORT_RUN",
        "REASONING_EFFORT_DECK",
        "REASONING_EFFORT_COMBAT",
        "SERVICE_TIER",
        "AUTH",
        "API_KEY",
        "AUTH_HEADER",
        "USERNAME",
        "PASSWORD",
        "HEADERS",
        "REQUEST_TIMEOUT_S",
        "EXTRA_RETRIES",
        "CONTEXT_WINDOW",
        "CAPACITY_VERIFIED_ENDPOINT",
    }
)
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def _headers(raw: str) -> tuple[tuple[str, str], ...]:
    headers: dict[str, tuple[str, str]] = {}
    if not raw:
        return ()
    for line in raw.removesuffix("\n").split("\n"):
        name, separator, value = line.partition(":")
        if not separator or not _HEADER_NAME.fullmatch(name) or not _HEADER_VALUE.fullmatch(value):
            raise ConfigurationError("Invalid custom headers; use one Name: value per line.")
        headers[name.lower()] = (name, value.strip(" \t"))
    return tuple(headers.values())


def load_configuration(
    defaults_file: Path,
    environment: Mapping[str, str],
    overrides: Mapping[str, str],
) -> Configuration:
    """Return the startup snapshot shared by Run and Combat; callers retain it."""
    try:
        content = defaults_file.read_text(encoding="utf-8") if defaults_file.exists() else ""
    except (OSError, UnicodeError):
        raise ConfigurationError("Cannot read the local defaults file.") from None
    # Interpolation would introduce a hidden second environment precedence rule.
    if any(binding.error for binding in parse_stream(StringIO(content))):
        raise ConfigurationError("Invalid syntax in the local defaults file.")
    defaults = dotenv_values(stream=StringIO(content), interpolate=False)
    known_names = {
        "STS2_DRIVER",
        "STS2_USER_INPUT",
        "STS2_MINIMAL",
        *(f"LLM_{key}" for key in _KEYS if key != "DRIVER"),
    }
    if any(name.startswith(("LLM_", "STS2_")) and name not in known_names for name in defaults):
        raise ConfigurationError("Unknown setting name in the local defaults file.")
    values = {key: value for key, value in defaults.items() if value is not None}
    values.update(environment)
    for name, value in overrides.items():
        if name not in known_names:
            raise ConfigurationError("Unknown startup override name.")
        values[name] = value

    def get(name: str, default: str = "") -> str:
        key = "STS2_DRIVER" if name == "DRIVER" else f"LLM_{name}"
        return values.get(key, default)

    try:
        driver = Driver(get("DRIVER", Driver.BUILTIN))
    except ValueError:
        raise ConfigurationError("Invalid STS2_DRIVER; choose builtin or external.") from None
    raw_user_input = values.get("STS2_USER_INPUT", "")
    if raw_user_input not in ("", "true", "false"):
        raise ConfigurationError("Invalid STS2_USER_INPUT; choose true or false.")
    user_input = raw_user_input == "true"
    raw_minimal = values.get("STS2_MINIMAL", "")
    if raw_minimal not in ("", "true", "false"):
        raise ConfigurationError("Invalid STS2_MINIMAL; choose true or false.")
    minimal = raw_minimal == "true"
    if driver is Driver.EXTERNAL:
        if user_input:
            raise ConfigurationError("User input requires builtin driving.")
        if minimal:
            raise ConfigurationError("Minimal mode requires builtin driving.")
        return Configuration(driver, None)
    try:
        protocol = Protocol(get("PROTOCOL", Protocol.RESPONSES))
        auth_kind = AuthKind(
            get("AUTH", AuthKind.API_KEY if protocol is Protocol.MESSAGES else AuthKind.BEARER)
        )
        timeout = float(get("REQUEST_TIMEOUT_S", "3600"))
        retries = int(get("EXTRA_RETRIES", "25"))
        context_window = int(get("CONTEXT_WINDOW", "300000"))
    except ValueError:
        raise ConfigurationError(
            "Invalid protocol, authentication, timeout, retry, or context window setting."
        ) from None
    if not math.isfinite(timeout) or timeout <= 0 or retries < 0 or context_window <= 0:
        raise ConfigurationError(
            "Timeout and context window must be positive; extra retries nonnegative."
        )
    messages = protocol is Protocol.MESSAGES
    endpoint = get(
        "BASE_URL", "https://api.anthropic.com" if messages else "https://api.openai.com/v1"
    )
    try:
        url = urlsplit(endpoint)
        valid_url = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and bool(url.port or 1)
            and url.username is None
            and url.password is None
            and "?" not in endpoint
            and "#" not in endpoint
        )
    except ValueError:
        valid_url = False
    if not valid_url or any(character.isspace() for character in endpoint):
        raise ConfigurationError(
            "Invalid LLM_BASE_URL; use HTTP(S) without userinfo, query, or fragment."
        )
    model = get("MODEL")
    if not model.strip():
        raise ConfigurationError("LLM_MODEL must not be empty.")
    header = get("AUTH_HEADER", "x-api-key")
    secret = get("PASSWORD") if auth_kind is AuthKind.BASIC else get("API_KEY")
    username = get("USERNAME")
    if auth_kind is not AuthKind.NONE and not secret:
        raise ConfigurationError("Selected authentication requires a credential.")
    if auth_kind is AuthKind.BASIC and (
        not username or any(char in username for char in ":\r\n\0")
    ):
        raise ConfigurationError("Basic authentication requires a valid LLM_USERNAME.")
    if auth_kind is AuthKind.API_KEY and not _HEADER_NAME.fullmatch(header):
        raise ConfigurationError("Invalid LLM_AUTH_HEADER.")
    if auth_kind is AuthKind.NONE:
        secret = ""
        username = ""
    if auth_kind in {AuthKind.BEARER, AuthKind.API_KEY} and (
        not _HEADER_VALUE.fullmatch(secret) or secret != secret.strip(" \t")
    ):
        raise ConfigurationError("Authentication credential cannot be sent as an HTTP header.")
    if any(char in secret for char in "\r\n\0"):
        raise ConfigurationError("Authentication credential contains invalid characters.")
    authentication = Authentication(auth_kind, secret, header, username)
    return Configuration(
        driver,
        ModelConfiguration(
            protocol,
            model,
            endpoint,
            get("REASONING_EFFORT"),
            get("SERVICE_TIER"),
            authentication,
            _headers(get("HEADERS")),
            timeout,
            retries,
            context_window,
            get("CAPACITY_VERIFIED_ENDPOINT"),
            get("REASONING_EFFORT_RUN"),
            get("REASONING_EFFORT_DECK"),
            get("REASONING_EFFORT_COMBAT"),
        ),
        user_input,
        minimal,
    )


def validate_mod_url(value: str) -> int:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ConfigurationError("Invalid Mod URL.") from None
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ConfigurationError(
            "The Mod URL must be an HTTP origin without credentials or parameters."
        )
    return port or (443 if url.scheme == "https" else 80)
