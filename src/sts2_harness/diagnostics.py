"""Bounded structured diagnostics, independent of durable game evidence."""

import logging
import re
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal, TextIO

from sts2_harness.client import models as native
from sts2_harness.game import Endpoint, OperationKind


class EventKind(StrEnum):
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    RECORDING_GAP = "recording_gap"
    RECORDING_RECOVERED = "recording_recovered"
    NATIVE_RETRY = "native_retry"
    NATIVE_RECOVERED = "native_recovered"


class Event(native.WireModel):
    event: EventKind
    sequence: int | None = None
    operation_id: str | None = None
    result: str | None = None
    error: str | None = None
    endpoint: Endpoint | None = None
    failures: int | None = None
    recovered_from: str | None = None


class Level(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class DiagnosticLine(Event):
    time: str
    level: Level
    component: Literal["game_control"] = "game_control"
    recording_id: str
    suppressed: int


class Stage(StrEnum):
    IDLE = "idle"
    INFLIGHT = "inflight"


class Status(native.WireModel):
    recording_id: str
    stage: Stage
    operation: OperationKind | None = None
    started_at: str | None = None
    sequence: int | None = None


class _QuietFile(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # Logging cannot recursively report a failed diagnostic destination.
        return


class _QuietConsole(logging.StreamHandler[TextIO]):
    def handleError(self, record: logging.LogRecord) -> None:
        return


def _redact(value: str) -> str:
    value = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\r\n]+", r"\1[redacted]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;&]+", r"\1[redacted]", value
    )
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    return re.sub(r"([?&][^\s=&#]+)=([^\s&#]*)", r"\1=[redacted]", value)


class Diagnostics:
    def __init__(
        self, recording_id: str, *, directory: Path | None = None, console: TextIO | None = None
    ) -> None:
        self._recording_id = recording_id
        self._directory = directory
        self._logger = logging.Logger("sts2_harness.game_control", logging.INFO)
        self._logger.propagate = False
        stream = _QuietConsole(console if console is not None else sys.stderr)
        stream.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(stream)
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                handler = _QuietFile(
                    directory / "diagnostics.jsonl",
                    maxBytes=10 * 1024 * 1024,
                    backupCount=2,
                    encoding="utf-8",
                )
            except OSError:
                handler = None
            if handler is not None:
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._logger.addHandler(handler)
        self._native_reports: dict[Endpoint, tuple[str, float, int]] = {}
        self._storage_error: str | None = None
        self._storage_report = 0.0
        self._storage_suppressed = 0
        self._status = Status(recording_id=_redact(self._recording_id)[:2048], stage=Stage.IDLE)

    @classmethod
    def console(cls, recording_id: str) -> "Diagnostics":
        return cls(recording_id)

    def emit(self, event: Event) -> None:
        now = time.monotonic()
        native_event = event.event in (EventKind.NATIVE_RETRY, EventKind.NATIVE_RECOVERED)
        native_suppressed = 0
        if native_event and event.endpoint is not None:
            previous = self._native_reports.get(event.endpoint)
            if event.error is not None:
                if previous is not None and previous[0] == event.error and now - previous[1] < 60:
                    self._native_reports[event.endpoint] = (
                        previous[0],
                        previous[1],
                        previous[2] + 1,
                    )
                    return
                native_suppressed = previous[2] if previous is not None else 0
                self._native_reports[event.endpoint] = (event.error, now, 0)
            elif previous is not None:
                native_suppressed = previous[2]
                del self._native_reports[event.endpoint]
        storage_failure = event.event is EventKind.RECORDING_GAP
        repeated = (
            storage_failure and event.error is not None and event.error == self._storage_error
        )
        if repeated and now - self._storage_report < 60:
            self._storage_suppressed += 1
            return
        recovering = event.event is EventKind.RECORDING_RECOVERED
        suppressed = (
            native_suppressed
            if native_event
            else self._storage_suppressed
            if repeated or recovering
            else 0
        )
        recovered_from = event.recovered_from
        if storage_failure and event.error is not None:
            self._storage_error = event.error
            self._storage_report = now
            self._storage_suppressed = 0
        elif recovering:
            recovered_from = self._storage_error
            self._storage_error = None
            self._storage_suppressed = 0
        # Only selected typed fields enter diagnostics, never native bodies or headers.
        severe = event.error in (
            "execution_unconfirmed",
            "execution_unknown",
            "control_unavailable",
            "dispatch_failed",
            "identity_unregistered",
            "catalog_identity_conflict",
            "native_preview_failed",
            "internal_error",
        )
        level = Level.ERROR if severe else Level.WARNING if event.error else Level.INFO
        payload = DiagnosticLine(
            time=datetime.now(UTC).isoformat(),
            level=level,
            recording_id=_redact(self._recording_id)[:2048],
            event=event.event,
            sequence=event.sequence,
            operation_id=_redact(event.operation_id)[:2048] if event.operation_id else None,
            result=_redact(event.result)[:2048] if event.result else None,
            error=_redact(event.error)[:2048] if event.error else None,
            suppressed=suppressed,
            endpoint=event.endpoint,
            failures=event.failures,
            recovered_from=_redact(recovered_from)[:2048] if recovered_from else None,
        )
        text = payload.model_dump_json(exclude_none=True)
        self._logger.log(
            logging.ERROR if severe else logging.WARNING if event.error else logging.INFO, text
        )

    def started(self, operation: OperationKind, *, sequence: int | None = None) -> None:
        self._native_reports.clear()
        self._write_status(
            Status(
                recording_id=_redact(self._recording_id)[:2048],
                stage=Stage.INFLIGHT,
                operation=operation,
                started_at=datetime.now(UTC).isoformat(),
                sequence=sequence,
            )
        )
        self.emit(
            Event(event=EventKind.OPERATION_STARTED, result=operation.value, sequence=sequence)
        )

    def finished(self) -> None:
        self._native_reports.clear()
        self._write_status(
            Status(recording_id=_redact(self._recording_id)[:2048], stage=Stage.IDLE)
        )

    def _write_status(self, status: Status) -> None:
        if self._status == status:
            return
        self._status = status
        if self._directory is None:
            return
        try:
            temporary = self._directory / "status.json.tmp"
            temporary.write_text(status.model_dump_json(), encoding="utf-8")
            temporary.replace(self._directory / "status.json")
        except OSError:
            return

    def close(self) -> None:
        for handler in self._logger.handlers:
            handler.close()
