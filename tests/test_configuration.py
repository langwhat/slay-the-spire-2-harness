import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sts2_harness.config import AuthKind, Driver, Protocol, load_configuration


def cli(
    tmp_path: Path, *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    clean = {
        key: value for key, value in os.environ.items() if not key.startswith(("STS2_", "LLM_"))
    }
    clean.update(environment or {})
    return subprocess.run(
        [sys.executable, "-m", "sts2_harness.cli", *arguments],
        cwd=tmp_path,
        env=clean,
        capture_output=True,
        text=True,
        check=False,
    )


def test_precedence_and_snapshot(tmp_path: Path) -> None:
    defaults = tmp_path / ".env"
    defaults.write_text("LLM_MODEL=local\nLLM_API_KEY=local-key\nLLM_REQUEST_TIMEOUT_S=50\n")
    environment = {"LLM_REQUEST_TIMEOUT_S": "60"}
    config = load_configuration(defaults, environment, {"LLM_EXTRA_RETRIES": "3"})
    defaults.write_text("STS2_DRIVER=external\n")
    environment["LLM_REQUEST_TIMEOUT_S"] = "90"
    assert config.driver is Driver.BUILTIN
    assert config.model is not None
    assert config.model.model == "local"
    assert config.model.authentication.secret == "local-key"
    assert config.model.timeout_seconds == 60
    assert config.model.extra_retries == 3
    result = cli(
        tmp_path,
        "check-config",
        "--set",
        "STS2_DRIVER=builtin",
        "--set",
        "LLM_MODEL=override",
        "--set",
        "LLM_AUTH=none",
        "--set",
        "LLM_REQUEST_TIMEOUT_S=70",
        environment=environment,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[1])["timeout_seconds"] == 70


def test_external_without_credentials(tmp_path: Path) -> None:
    result = cli(tmp_path, "check-config", "--set", "STS2_DRIVER=external")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"driver": "external", "model_configured": False}


@pytest.mark.parametrize("protocol", list(Protocol))
@pytest.mark.parametrize("auth", list(AuthKind))
def test_protocol_authentication_and_defaults(
    tmp_path: Path, protocol: Protocol, auth: AuthKind
) -> None:
    result = cli(
        tmp_path,
        "check-config",
        environment={
            "LLM_PROTOCOL": protocol.value,
            "LLM_AUTH": auth.value,
            "LLM_MODEL": "private",
            "LLM_API_KEY": "secret",
            "LLM_PASSWORD": "secret",
            "LLM_USERNAME": "private",
            "LLM_AUTH_HEADER": "Private-Header",
            "LLM_HEADERS": "Other: secret\nother: newer",
            "LLM_BASE_URL": "https://example.invalid/private/secret",
        },
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.splitlines()[1])
    assert output["protocol"] == protocol.value
    assert output["authentication"] == auth.value
    assert output["timeout_seconds"] == 3600
    assert output["extra_retries"] == 25
    assert output["custom_header_count"] == 1
    assert not any(
        secret in result.stdout + result.stderr
        for secret in (
            "private",
            "secret",
            "example.invalid",
            "Private-Header",
            "newer",
        )
    )


@pytest.mark.parametrize(
    "setting",
    [
        "LLM_PROTOCOL=SECRET",
        "LLM_BASE_URL=https://SECRET:bad/",
        "LLM_AUTH=SECRET",
        "LLM_REQUEST_TIMEOUT_S=nan",
        "LLM_EXTRA_RETRIES=-1",
        "LLM_HEADERS=SECRET",
        "LLM_AUTH_HEADER=SECRET header",
        "STS2_UNKNOWN=SECRET",
    ],
)
def test_invalid_configuration_is_safe(tmp_path: Path, setting: str) -> None:
    result = cli(
        tmp_path,
        "check-config",
        "--set",
        setting,
        environment={
            "LLM_MODEL": "model",
            "LLM_API_KEY": "key",
            "LLM_AUTH": "api-key",
        },
    )
    assert result.returncode == 2
    assert "Configuration error:" in result.stderr
    assert "SECRET" not in result.stdout + result.stderr


def test_none_ignores_credentials(tmp_path: Path) -> None:
    config = load_configuration(
        tmp_path / "absent",
        {
            "LLM_MODEL": "model",
            "LLM_AUTH": "none",
            "LLM_API_KEY": "secret",
        },
        {},
    )
    assert config.model is not None
    assert config.model.authentication.secret == ""


def test_bad_defaults_and_cli_fail_without_input_echo(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('LLM_API_KEY="SECRET\n')
    result = cli(tmp_path, "check-config")
    assert result.returncode == 2
    assert "SECRET" not in result.stderr
    result = cli(tmp_path, "SECRET")
    assert result.returncode == 2
    assert "SECRET" not in result.stderr


def test_missing_builtin_configuration_fails(tmp_path: Path) -> None:
    result = cli(tmp_path, "check-config")
    assert result.returncode == 2


def test_messages_uses_sdk_base_and_auth_defaults(tmp_path: Path) -> None:
    config = load_configuration(
        tmp_path / "absent",
        {
            "LLM_PROTOCOL": "messages",
            "LLM_MODEL": "model",
            "LLM_API_KEY": "key",
        },
        {},
    )
    assert config.model is not None
    assert config.model.endpoint == "https://api.anthropic.com"
    assert config.model.authentication.kind is AuthKind.API_KEY


@pytest.mark.parametrize("suffix", ["@host/prefix", "/prefix?secret", "/prefix#secret"])
def test_base_url_rejects_implicit_auth_and_ambiguous_suffixes(tmp_path: Path, suffix: str) -> None:
    result = cli(
        tmp_path,
        "check-config",
        environment={
            "LLM_MODEL": "model",
            "LLM_AUTH": "none",
            "LLM_BASE_URL": f"https://secret{suffix}",
        },
    )
    assert result.returncode == 2
    assert "secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["LLM_TIMEOUT_S", "STS2_DRIVE"])
def test_local_default_typos_fail_but_unrelated_environment_is_allowed(
    tmp_path: Path,
    name: str,
) -> None:
    environment = {"STS2_DRIVER": "external", name: "secret"}
    assert cli(tmp_path, "check-config", environment=environment).returncode == 0
    (tmp_path / ".env").write_text(f"{name}=secret\nUNRELATED=allowed\n")
    result = cli(tmp_path, "check-config", environment=environment)
    assert result.returncode == 2
    assert "secret" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "setting",
    [
        "LLM_HEADERS=Name: secret\x01",
        "LLM_HEADERS=Name: secret\rOther: value",
        "LLM_HEADERS=Name: secret\u2028Other: value",
        "LLM_HEADERS=Name: secrét",
        "LLM_API_KEY=secrét",
        "LLM_API_KEY=secret\x7f",
        "LLM_API_KEY=secret\x01",
    ],
)
def test_unsendable_headers_and_token_credentials_fail_safely(tmp_path: Path, setting: str) -> None:
    result = cli(
        tmp_path,
        "check-config",
        "--set",
        setting,
        environment={
            "LLM_MODEL": "model",
            "LLM_API_KEY": "key",
        },
    )
    assert result.returncode == 2
    assert "secret" not in result.stdout + result.stderr
    assert "secrét" not in result.stdout + result.stderr


def test_basic_unicode_password_and_header_tabs_remain_valid(tmp_path: Path) -> None:
    result = cli(
        tmp_path,
        "check-config",
        environment={
            "LLM_MODEL": "model",
            "LLM_AUTH": "basic",
            "LLM_USERNAME": "user",
            "LLM_PASSWORD": "密碼:é",
            "LLM_HEADERS": "Name: first\tsecond\nOther: value\n",
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[1])["custom_header_count"] == 2
    assert "密碼" not in result.stdout + result.stderr


def test_resident_start_rejects_external_driver_without_connecting(tmp_path: Path) -> None:
    result = cli(tmp_path, "start", "--set", "STS2_DRIVER=external")
    assert result.returncode == 2
    assert "Resident external driving is not implemented." in result.stderr
    assert not (tmp_path / "runs").exists()


def test_resident_start_rejects_unimplemented_protocol_before_game_connection(
    tmp_path: Path,
) -> None:
    result = cli(
        tmp_path,
        "start",
        "--set",
        "LLM_MODEL=local",
        "--set",
        "LLM_AUTH=none",
        "--set",
        "LLM_PROTOCOL=messages",
    )
    assert result.returncode == 2
    assert "configured model protocol is not implemented" in result.stderr
    assert not (tmp_path / "runs").exists()
