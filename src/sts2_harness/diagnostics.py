"""Bounded structured diagnostics, independent of durable game evidence."""

import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal, TextIO

from sts2_harness.client import models as native
from sts2_harness.config import AuthKind, Configuration, Driver, Protocol
from sts2_harness.game import Endpoint, OperationKind


class EventKind(StrEnum):
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    RECORDING_GAP = "recording_gap"
    RECORDING_RECOVERED = "recording_recovered"
    NATIVE_RETRY = "native_retry"
    NATIVE_RECOVERED = "native_recovered"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_COMPLETED = "model_request_completed"
    MODEL_RETRY = "model_retry"
    MODEL_RECOVERED = "model_recovered"
    MODEL_EXHAUSTED = "model_exhausted"
    LIFECYCLE_CHANGED = "lifecycle_changed"


class Event(native.WireModel):
    event: EventKind
    sequence: int | None = None
    operation_id: str | None = None
    result: str | None = None
    error: str | None = None
    endpoint: Endpoint | None = None
    failures: int | None = None
    recovered_from: str | None = None
    role: native.DecisionOwner | None = None
    attempt: int | None = None
    elapsed_seconds: float | None = None


class Level(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class DiagnosticLine(Event):
    time: str
    level: Level
    component: Literal["game_control", "model", "session"] = "game_control"
    recording_id: str | None
    suppressed: int


class Stage(StrEnum):
    IDLE = "idle"
    INFLIGHT = "inflight"


class Lifecycle(StrEnum):
    WAITING_GAME = "waiting_game"
    WAITING_RUN = "waiting_run"
    PAUSED = "paused"
    RUNNING = "running"
    TERMINAL = "terminal"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class Status(native.WireModel):
    recording_id: str | None
    stage: Stage
    lifecycle: Lifecycle | None = None
    control_intent: native.ControlIntent | None = None
    game_instance_id: str | None = None
    run_id: str | None = None
    archive_path: Path | None = None
    control_endpoint: str | None = None
    status_path: Path | None = None
    process_id: int | None = None
    mod_port: int | None = None
    driver: Driver | None = None
    model_protocol: Protocol | None = None
    model_authentication: AuthKind | None = None
    model_timeout_seconds: float | None = None
    model_extra_retries: int | None = None
    reasoning_effort_configured: bool | None = None
    service_tier_configured: bool | None = None
    operation: OperationKind | None = None
    operation_id: str | None = None
    role: native.DecisionOwner | None = None
    model_request: int | None = None
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
        self,
        recording_id: str | None,
        *,
        directory: Path | None = None,
        console: TextIO | None = None,
        control_endpoint: str | None = None,
    ) -> None:
        self._recording_id = _redact(recording_id)[:2048] if recording_id is not None else None
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
        self._retry_reports: dict[Endpoint | int, tuple[str, float, int]] = {}
        self._storage_error: str | None = None
        self._storage_report = 0.0
        self._storage_suppressed = 0
        self._status = Status(
            recording_id=self._recording_id,
            stage=Stage.IDLE,
            control_endpoint=control_endpoint,
            status_path=directory / "status.json" if directory is not None else None,
            process_id=os.getpid(),
        )

    @classmethod
    def console(cls, recording_id: str) -> "Diagnostics":
        return cls(recording_id)

    def emit(self, event: Event) -> None:
        if (
            event.event is EventKind.NATIVE_RETRY
            and event.error in ("connection", "timeout")
            and self._status.operation is OperationKind.PAUSE
        ):
            self.lifecycle(Lifecycle.WAITING_GAME)
        now = time.monotonic()
        native_event = event.event in (EventKind.NATIVE_RETRY, EventKind.NATIVE_RECOVERED)
        model_event = event.event in (
            EventKind.MODEL_RETRY,
            EventKind.MODEL_RECOVERED,
            EventKind.MODEL_EXHAUSTED,
        )
        report_key = event.endpoint if native_event else event.sequence if model_event else None
        retry_suppressed = 0
        if report_key is not None:
            previous = self._retry_reports.get(report_key)
            if event.error is not None:
                if (
                    previous is not None
                    and previous[0] == event.error
                    and now - previous[1] < 60
                    and event.event is not EventKind.MODEL_EXHAUSTED
                ):
                    self._retry_reports[report_key] = (
                        previous[0],
                        previous[1],
                        previous[2] + 1,
                    )
                    return
                retry_suppressed = previous[2] if previous is not None else 0
                self._retry_reports[report_key] = (event.error, now, 0)
            elif previous is not None:
                retry_suppressed = previous[2]
                del self._retry_reports[report_key]
        storage_failure = event.event is EventKind.RECORDING_GAP
        repeated = (
            storage_failure and event.error is not None and event.error == self._storage_error
        )
        if repeated and now - self._storage_report < 60:
            self._storage_suppressed += 1
            return
        recovering = event.event is EventKind.RECORDING_RECOVERED
        suppressed = (
            retry_suppressed
            if native_event or model_event
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
        severe = event.event is EventKind.MODEL_EXHAUSTED or event.error in (
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
            recording_id=self._recording_id,
            event=event.event,
            component="session"
            if event.event is EventKind.LIFECYCLE_CHANGED
            else "model"
            if event.event
            in (
                EventKind.MODEL_REQUEST_STARTED,
                EventKind.MODEL_REQUEST_COMPLETED,
                EventKind.MODEL_RETRY,
                EventKind.MODEL_RECOVERED,
                EventKind.MODEL_EXHAUSTED,
            )
            else "game_control",
            role=event.role,
            attempt=event.attempt,
            elapsed_seconds=event.elapsed_seconds,
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

    def configured(self, configuration: Configuration, port: int) -> None:
        model = configuration.model
        self._write_status(
            self._status.model_copy(
                update={
                    "mod_port": port,
                    "driver": configuration.driver,
                    "model_protocol": model.protocol if model is not None else None,
                    "model_authentication": model.authentication.kind
                    if model is not None
                    else None,
                    "model_timeout_seconds": model.timeout_seconds if model is not None else None,
                    "model_extra_retries": model.extra_retries if model is not None else None,
                    "reasoning_effort_configured": bool(model.reasoning_effort)
                    if model is not None
                    else None,
                    "service_tier_configured": bool(model.service_tier)
                    if model is not None
                    else None,
                }
            )
        )

    def bind_recording(
        self, recording_id: str | None, location: native.Location | None, archive_path: Path | None
    ) -> None:
        self._recording_id = _redact(recording_id)[:2048] if recording_id is not None else None
        self._write_status(
            self._status.model_copy(
                update={
                    "recording_id": self._recording_id,
                    "game_instance_id": location.game_instance_id if location is not None else None,
                    "run_id": location.run_id if location is not None else None,
                    "archive_path": archive_path,
                }
            )
        )

    def lifecycle(self, stage: Lifecycle) -> None:
        if self._status.lifecycle is stage:
            return
        self._write_status(self._status.model_copy(update={"lifecycle": stage}))
        self.emit(Event(event=EventKind.LIFECYCLE_CHANGED, result=stage.value))

    def native_status(self, status: native.Status, waiting: bool) -> None:
        self._write_status(
            self._status.model_copy(
                update={
                    "control_intent": status.control_intent,
                }
            )
        )
        if waiting:
            self.lifecycle(
                Lifecycle.PAUSED
                if status.control_intent is native.ControlIntent.PAUSED
                else Lifecycle.RUNNING
            )

    def started_model(self, role: native.DecisionOwner, sequence: int) -> None:
        self._write_status(
            self._status.model_copy(
                update={
                    "stage": Stage.INFLIGHT,
                    "role": role,
                    "model_request": sequence,
                    "started_at": datetime.now(UTC).isoformat(),
                    "sequence": sequence,
                }
            )
        )
        self.emit(Event(event=EventKind.MODEL_REQUEST_STARTED, role=role, sequence=sequence))

    def started(self, operation: OperationKind, *, sequence: int | None = None) -> None:
        self._retry_reports.clear()
        self._write_status(
            self._status.model_copy(
                update={
                    "stage": Stage.INFLIGHT,
                    "operation": operation,
                    "started_at": datetime.now(UTC).isoformat(),
                    "sequence": sequence,
                }
            )
        )
        self.emit(
            Event(event=EventKind.OPERATION_STARTED, result=operation.value, sequence=sequence)
        )

    def submitting(self, operation_id: str) -> None:
        self._write_status(
            self._status.model_copy(update={"operation_id": _redact(operation_id)[:2048]})
        )

    def finished(self) -> None:
        self._retry_reports.clear()
        self._write_status(
            self._status.model_copy(
                update={
                    "stage": Stage.IDLE,
                    "operation": None,
                    "operation_id": None,
                    "role": None,
                    "model_request": None,
                    "started_at": None,
                    "sequence": None,
                }
            )
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
