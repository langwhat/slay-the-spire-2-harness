import asyncio
import signal
from pathlib import Path

import pytest

from sts2_harness import cli
from sts2_harness.config import Configuration
from sts2_harness.interactive import InteractiveOptions


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
