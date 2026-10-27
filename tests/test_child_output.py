import asyncio
import sys
from pathlib import Path

import pytest

from sts2_harness.child_output import BoundedChildOutput, ChildStream


def test_both_streams_keep_only_three_bounded_raw_files(tmp_path: Path) -> None:
    async def exercise() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; data=bytes(range(256))*4096; "
            "[(os.write(1,data),os.write(2,data[::-1])) for _ in range(31)]",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout = BoundedChildOutput(tmp_path, ChildStream.STDOUT)
        stderr = BoundedChildOutput(tmp_path, ChildStream.STDERR)
        await asyncio.gather(stdout.drain(process.stdout), stderr.drain(process.stderr))
        assert await process.wait() == 0
        stdout.close()
        stderr.close()

    asyncio.run(exercise())
    for stream, block in (("stdout", bytes(range(256))), ("stderr", bytes(range(255, -1, -1)))):
        files = [tmp_path / f"game.{stream}.log{suffix}" for suffix in (".2", ".1", "")]
        assert [path.stat().st_size for path in files] == [10 * 1024**2, 10 * 1024**2, 1024**2]
        assert b"".join(path.read_bytes() for path in files) == block * (21 * 4096)


@pytest.mark.parametrize("failure", ["initialization", "rotation"])
def test_failed_sink_keeps_consuming_both_pipes(tmp_path: Path, failure: str) -> None:
    suffix = "" if failure == "initialization" else ".2"
    (tmp_path / f"game.stdout.log{suffix}").mkdir()

    async def exercise() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; data=b'x'*65536; [(os.write(1,data),os.write(2,data)) for _ in range(176)]",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        outputs = [BoundedChildOutput(tmp_path, stream) for stream in ChildStream]
        async with asyncio.timeout(5):
            await asyncio.gather(outputs[0].drain(process.stdout), outputs[1].drain(process.stderr))
            assert await process.wait() == 0
        for output in outputs:
            output.close()

    asyncio.run(exercise())
    assert (tmp_path / "game.stderr.log").stat().st_size == 1024**2


@pytest.mark.skipif(sys.platform == "win32", reason="Uses POSIX file-size limits.")
def test_write_failure_discards_output_without_blocking_the_child(tmp_path: Path) -> None:
    import subprocess

    program = """
import asyncio, resource, sys
from pathlib import Path
from sts2_harness.child_output import BoundedChildOutput, ChildStream
async def exercise():
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import os; os.write(1,b'x'*1048576); os.write(2,b'survived')",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
    outputs = [BoundedChildOutput(Path(sys.argv[1]), stream) for stream in ChildStream]
    async with asyncio.timeout(5):
        await asyncio.gather(outputs[0].drain(child.stdout), outputs[1].drain(child.stderr))
        assert await child.wait() == 0
    for output in outputs: output.close()
asyncio.run(exercise())
"""
    subprocess.run([sys.executable, "-c", program, str(tmp_path)], check=True, timeout=10)
    assert (tmp_path / "game.stdout.log").stat().st_size <= 1024
    assert (tmp_path / "game.stderr.log").read_bytes() == b"survived"
