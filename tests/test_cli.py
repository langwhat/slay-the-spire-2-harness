import asyncio
import json
import signal
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sts2_harness import cli
from sts2_harness.config import Configuration
from sts2_harness.interactive import InteractiveOptions
from sts2_harness.journal import Journal, Manifest, SessionMaterials


def test_start_stops_on_ctrl_c_without_asyncio_signal_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(loop: asyncio.AbstractEventLoop, signum: int, callback: object) -> None:
        raise NotImplementedError

    started = False

    async def run_external(
        options: InteractiveOptions,
        configuration: Configuration,
        *,
        tool_port: int,
        stop_requested: asyncio.Event,
    ) -> None:
        nonlocal started
        started = True
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stop_requested.wait(), timeout=2)

    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", unsupported)
    monkeypatch.setattr(cli, "run_external", run_external)
    original = signal.getsignal(signal.SIGINT)
    assert (
        cli.main(["start", "--env-file", str(tmp_path / ".env"), "--set", "STS2_DRIVER=external"])
        == 0
    )
    assert started
    assert signal.getsignal(signal.SIGINT) == original


@pytest.mark.parametrize("historical", [False, True])
def test_journal_displays_recorded_core_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], historical: bool
) -> None:
    manifest = Manifest(
        schema_version=21 if historical else 22,
        recording_id="recording",
        core_version="1.2.3",
        core_revision=None if historical else "a" * 40 + "-dirty",
        game_instance_id="game",
        run_id="run",
        materials=SessionMaterials(
            entry_documents_enabled=False, act_guidance={}, battle_guidance=()
        ),
    )
    archive = tmp_path / "recording.sqlite"
    Journal.create(archive, manifest).close()
    if historical:
        with closing(sqlite3.connect(archive)) as connection:
            (value,) = connection.execute("SELECT value FROM manifest").fetchone()
        assert "core_revision" not in json.loads(value)

    assert cli.main(["journal", "--archive", str(archive)]) == 0
    shown = json.loads(capsys.readouterr().out)["manifest"]
    assert shown["schema_version"] == (21 if historical else 22)
    assert shown["core_version"] == manifest.core_version
    assert shown["core_revision"] == manifest.core_revision
