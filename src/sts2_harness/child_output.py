"""Drain owned child pipes while keeping bounded raw startup evidence."""

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

_READ_BYTES = 16 * 1024
_FILE_BYTES = 10 * 1024 * 1024
_PROBE_BYTES = 64 * 1024


class ChildStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class BoundedChildOutput:
    def __init__(self, directory: Path, stream: ChildStream) -> None:
        self._path = directory / f"game.{stream.value}.log"
        self._file: BinaryIO | None = None
        self._size = 0
        try:
            self._file = self._path.open("wb")
        except OSError:
            self.close()

    async def drain(self, reader: asyncio.StreamReader) -> None:
        while data := await reader.read(_READ_BYTES):
            if self._file is None:
                continue
            try:
                self._write(data)
            except OSError:
                self.close()

    def _write(self, data: bytes) -> None:
        while data:
            if self._size == _FILE_BYTES:
                self._rotate()
            assert self._file is not None
            piece = data[: _FILE_BYTES - self._size]
            self._file.write(piece)
            self._size += len(piece)
            data = data[len(piece) :]

    def _rotate(self) -> None:
        assert self._file is not None
        self._file.close()
        self._file = None
        self._path.with_name(self._path.name + ".2").unlink(missing_ok=True)
        previous = self._path.with_name(self._path.name + ".1")
        if previous.exists():
            previous.replace(self._path.with_name(self._path.name + ".2"))
        self._path.replace(previous)
        self._file = self._path.open("wb")
        self._size = 0

    def close(self) -> None:
        destination, self._file = self._file, None
        if destination is not None:
            try:
                destination.close()
            except OSError:
                # A failed sink must never stop pipe consumption or process cleanup.
                return


async def capture_probe(reader: asyncio.StreamReader, overflow: asyncio.Event) -> bytes:
    captured = bytearray()
    while data := await reader.read(_READ_BYTES):
        remaining = _PROBE_BYTES - len(captured)
        captured.extend(data[:remaining])
        if len(data) > remaining:
            overflow.set()
    return bytes(captured)
