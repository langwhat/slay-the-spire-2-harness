import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sts2_harness.config import Driver, load_configuration


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
        timeout=10,
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
    assert json.loads(result.stdout) == {
        "driver": "external",
        "model_configured": False,
        "minimal": False,
    }


def test_minimal_environment_precedence_and_cli_override(tmp_path: Path) -> None:
    defaults = tmp_path / ".env"
    defaults.write_text("STS2_MINIMAL=true\nLLM_MODEL=fake\nLLM_AUTH=none\n")
    assert load_configuration(defaults, {}, {}).minimal
    assert not load_configuration(defaults, {"STS2_MINIMAL": "false"}, {}).minimal
    assert load_configuration(defaults, {"STS2_MINIMAL": "false"}, {"STS2_MINIMAL": "true"}).minimal
    assert not load_configuration(
        defaults, {"STS2_MINIMAL": "true"}, {"STS2_MINIMAL": "false"}
    ).minimal
    configured = cli(tmp_path, "check-config")
    assert configured.returncode == 0, configured.stderr
    assert json.loads(configured.stdout.splitlines()[0])["minimal"] is True
    disabled = cli(tmp_path, "check-config", "--set", "STS2_MINIMAL=false")
    assert disabled.returncode == 0, disabled.stderr
    assert json.loads(disabled.stdout.splitlines()[0])["minimal"] is False
    forced = cli(
        tmp_path,
        "start",
        "--minimal",
        "--set",
        "STS2_MINIMAL=false",
        "--set",
        "STS2_DRIVER=external",
        environment={"STS2_MINIMAL": "invalid"},
    )
    assert forced.returncode == 2
    assert "Minimal mode requires builtin driving." in forced.stderr


@pytest.mark.parametrize("setting", ["LLM_PROTOCOL=SECRET", "LLM_HEADERS=SECRET"])
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


def test_headless_rejects_missing_launch_inputs_without_creating_an_instance(
    tmp_path: Path,
) -> None:
    result = cli(tmp_path, "headless", "--set", "LLM_MODEL=local", "--set", "LLM_AUTH=none")
    assert result.returncode == 2
    assert "Headless startup requires --game and --template." in result.stderr
    assert not (tmp_path / "runs").exists()


def test_reasoning_effort_inherits_per_purpose(tmp_path: Path) -> None:
    from sts2_harness.model_purpose import ModelPurpose

    config = load_configuration(
        tmp_path / "absent",
        {},
        {
            "LLM_MODEL": "model",
            "LLM_AUTH": "none",
            "LLM_REASONING_EFFORT": "low",
            "LLM_REASONING_EFFORT_DECK": "medium",
            "LLM_REASONING_EFFORT_COMBAT": "high",
        },
    ).model
    assert config is not None
    assert config.for_purpose(ModelPurpose.RUN).reasoning_effort == "low"
    assert config.for_purpose(ModelPurpose.DECK).reasoning_effort == "medium"
    assert config.for_purpose(ModelPurpose.COMBAT).reasoning_effort == "high"
    assert config.for_purpose(ModelPurpose.COMBAT_ELITE_BOSS).reasoning_effort == "high"
    fallback = load_configuration(
        tmp_path / "absent",
        {},
        {"LLM_MODEL": "model", "LLM_AUTH": "none", "LLM_REASONING_EFFORT": "low"},
    ).model
    assert fallback is not None
    assert all(fallback.for_purpose(purpose).reasoning_effort == "low" for purpose in ModelPurpose)
