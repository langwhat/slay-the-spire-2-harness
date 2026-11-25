"""Bounded structured diagnostics, independent of durable game evidence."""

import json
import logging
import os
import re
import sqlite3
import sys
import time
import traceback
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, TextIO

from pydantic import AfterValidator, Field, JsonValue

from sts2_harness.client import models as native
from sts2_harness.config import AuthKind, Configuration, Driver, Protocol
from sts2_harness.game import Endpoint, OperationKind, RunEndReason
from sts2_harness.results import (
    ActionCompleted,
    Outcome,
    OutcomeStatus,
    ReadCompleted,
    ReasonCode,
    RewardBatchCompleted,
)
from sts2_harness.tools import ToolName


def _redact(value: str) -> str:
    value = re.sub(r"(?i)(authorization\s*[:=]\s*)[^\r\n]+", r"\1[redacted]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;&]+", r"\1[redacted]", value
    )
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    return re.sub(r"([?&][^\s=&#]+)=([^\s&#]*)", r"\1=[redacted]", value)


def _bounded_text(value: str) -> str:
    return _redact(value[:2048])[:2048].encode("utf-8", errors="replace").decode("utf-8")


type DiagnosticText = Annotated[str, AfterValidator(_bounded_text)]


class EventKind(StrEnum):
    OPERATION_STARTED = "operation_started"
    OPERATION_COMPLETED = "operation_completed"
    RECORDING_GAP = "recording_gap"
    RECORDING_SEAL_FAILED = "recording_seal_failed"
    RECORDING_RECOVERED = "recording_recovered"
    NATIVE_RETRY = "native_retry"
    NATIVE_RECOVERED = "native_recovered"
    NATIVE_FAILED = "native_failed"
    DISCOVERY_RETRY = "discovery_retry"
    DISCOVERY_RECOVERED = "discovery_recovered"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_REQUEST_COMPLETED = "model_request_completed"
    MODEL_RETRY = "model_retry"
    MODEL_RECOVERED = "model_recovered"
    MODEL_FAILED = "model_failed"
    LIFECYCLE_CHANGED = "lifecycle_changed"
    CONTROL_CHANGED = "control_changed"
    TOOL_RECOVERY_EXHAUSTED = "tool_recovery_exhausted"
    MODEL_ENCODING = "model_encoding"
    TOOL_FINISHED = "tool_finished"
    BATCH_SUBMITTED = "batch_submitted"
    BATCH_SETTLED = "batch_settled"
    AUTO_FINISHED = "auto_finished"
    ACT_ENTERED = "act_entered"
    ROLE_CHANGED = "role_changed"
    BATTLE_STARTED = "battle_started"
    BATTLE_FINISHED = "battle_finished"
    NOTE_BUDGET_EXCEEDED = "note_budget_exceeded"
    MECHANISM_METRICS = "mechanism_metrics"
    MECHANISM_METRICS_SUMMARY = "mechanism_metrics_summary"


class Event(native.WireModel):
    event: EventKind
    sequence: int | None = None
    operation_id: DiagnosticText | None = None
    result: DiagnosticText | None = None
    error: DiagnosticText | None = None
    endpoint: Endpoint | None = None
    failures: int | None = None
    recovered_from: DiagnosticText | None = None
    role: native.DecisionOwner | None = None
    attempt: int | None = None
    elapsed_seconds: float | None = None


class RecoveryExhausted(Event):
    event: EventKind = EventKind.TOOL_RECOVERY_EXHAUSTED


class EncodingMetadata(Event):
    event: EventKind = EventKind.MODEL_ENCODING
    digest: DiagnosticText


class NoteBudgetField(StrEnum):
    OTHER = "other"
    SUMMARY = "summary"


class NoteBudgetExceeded(Event):
    event: EventKind = EventKind.NOTE_BUDGET_EXCEEDED
    field: NoteBudgetField
    length: int
    soft_limit: int


class MechanismMetrics(Event):
    event: EventKind = EventKind.MECHANISM_METRICS
    mechanism_count: int
    mechanism_characters: int
    estimated_mechanism_tokens: int
    repeated_mechanism_count: int
    estimated_repeated_mechanism_tokens: int


class MechanismMetricsSummary(Event):
    event: EventKind = EventKind.MECHANISM_METRICS_SUMMARY
    requests_with_mechanisms: int
    unique_mechanism_count: int
    mechanism_count: int
    mechanism_characters: int
    estimated_mechanism_tokens: int
    repeated_mechanism_count: int
    estimated_repeated_mechanism_tokens: int


def _measure_mechanisms_with_bytes(
    mechanisms: Sequence[Mapping[str, JsonValue]],
    *,
    seen_keys: set[tuple[str, str, str | None, str | None]] | None = None,
    role: native.DecisionOwner | None = None,
) -> tuple[MechanismMetrics, int, int]:
    total_chars = 0
    total_bytes = 0
    repeated_count = 0
    repeated_bytes = 0
    for item in mechanisms:
        encoded_text = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded_bytes = len(encoded_text.encode("utf-8"))
        total_chars += len(encoded_text)
        total_bytes += encoded_bytes
        if seen_keys is not None:
            kind = item.get("kind")
            name = item.get("name")
            text = item.get("text")
            definition_id = item.get("definition_id")
            completeness = item.get("completeness")
            identity = definition_id if isinstance(definition_id, str) else str(name)
            key = (
                str(kind),
                identity,
                text if isinstance(text, str) else None,
                completeness if isinstance(completeness, str) else None,
            )
            if key in seen_keys:
                repeated_count += 1
                repeated_bytes += encoded_bytes
            else:
                seen_keys.add(key)
    metrics = MechanismMetrics(
        role=role,
        mechanism_count=len(mechanisms),
        mechanism_characters=total_chars,
        estimated_mechanism_tokens=(total_bytes + 3) // 4 if total_bytes else 0,
        repeated_mechanism_count=repeated_count,
        estimated_repeated_mechanism_tokens=(repeated_bytes + 3) // 4 if repeated_bytes else 0,
    )
    return metrics, total_bytes, repeated_bytes


def measure_mechanisms(
    mechanisms: Sequence[Mapping[str, JsonValue]],
    *,
    seen_keys: set[tuple[str, str, str | None, str | None]] | None = None,
    role: native.DecisionOwner | None = None,
) -> MechanismMetrics:
    return _measure_mechanisms_with_bytes(mechanisms, seen_keys=seen_keys, role=role)[0]


class MechanismTracker:
    """Run-scoped accumulator for delivered mechanism size and cross-node repeats."""

    def __init__(self) -> None:
        self._seen_keys: set[tuple[str, str, str | None, str | None]] = set()
        self._requests = 0
        self._count = 0
        self._characters = 0
        self._total_bytes = 0
        self._repeated_count = 0
        self._repeated_bytes = 0

    def record(
        self,
        mechanisms: Sequence[Mapping[str, JsonValue]],
        *,
        role: native.DecisionOwner | None = None,
        diagnostics: "Diagnostics | None" = None,
    ) -> MechanismMetrics | None:
        if not mechanisms:
            return None
        metrics, total_bytes, repeated_bytes = _measure_mechanisms_with_bytes(
            mechanisms, seen_keys=self._seen_keys, role=role
        )
        self._requests += 1
        self._count += metrics.mechanism_count
        self._characters += metrics.mechanism_characters
        self._total_bytes += total_bytes
        self._repeated_count += metrics.repeated_mechanism_count
        self._repeated_bytes += repeated_bytes
        if diagnostics is not None:
            diagnostics.emit(metrics)
        return metrics

    def summary(self) -> MechanismMetricsSummary | None:
        if self._requests == 0:
            return None
        return MechanismMetricsSummary(
            requests_with_mechanisms=self._requests,
            unique_mechanism_count=len(self._seen_keys),
            mechanism_count=self._count,
            mechanism_characters=self._characters,
            estimated_mechanism_tokens=(self._total_bytes + 3) // 4 if self._total_bytes else 0,
            repeated_mechanism_count=self._repeated_count,
            estimated_repeated_mechanism_tokens=(
                (self._repeated_bytes + 3) // 4 if self._repeated_bytes else 0
            ),
        )


class ResultEvent(Event):
    outcome: OutcomeStatus
    reason: ReasonCode | None


class ExceptionFrame(native.WireModel):
    file: DiagnosticText
    function: DiagnosticText
    line: int


class ExceptionDetails(native.WireModel):
    type: DiagnosticText
    frames: Annotated[tuple[ExceptionFrame, ...], Field(max_length=8)]

    @classmethod
    def capture(cls, error: Exception) -> "ExceptionDetails":
        frames: deque[ExceptionFrame] = deque(maxlen=8)
        for frame, line in traceback.walk_tb(error.__traceback__):
            frames.append(
                ExceptionFrame(
                    file=Path(frame.f_code.co_filename).name,
                    function=frame.f_code.co_name,
                    line=line,
                )
            )
        return cls(type=type(error).__name__, frames=tuple(frames))


class RecordingGap(Event):
    event: EventKind = EventKind.RECORDING_GAP
    exception: ExceptionDetails
    os_error_code: int | None
    sqlite_error_code: int | None

    @classmethod
    def capture(cls, error: Exception, sequence: int | None = None) -> "RecordingGap":
        return cls(
            error="storage_unavailable",
            sequence=sequence,
            exception=ExceptionDetails.capture(error),
            os_error_code=error.errno if isinstance(error, OSError) else None,
            sqlite_error_code=error.sqlite_errorcode
            if isinstance(error, sqlite3.DatabaseError) and hasattr(error, "sqlite_errorcode")
            else None,
        )


class RecordingSealFailed(RecordingGap):
    event: EventKind = EventKind.RECORDING_SEAL_FAILED


class ToolFinished(ResultEvent):
    event: EventKind = EventKind.TOOL_FINISHED
    tool: ToolName | None
    exception: ExceptionDetails | None = None


class AutoFinished(ResultEvent):
    event: EventKind = EventKind.AUTO_FINISHED
    label: DiagnosticText


class ActEntered(Event):
    event: EventKind = EventKind.ACT_ENTERED
    act: int | None


class ControlChanged(Event):
    event: EventKind = EventKind.CONTROL_CHANGED
    intent: native.ControlIntent


class RoleChanged(Event):
    event: EventKind = EventKind.ROLE_CHANGED


class BattleStarted(Event):
    event: EventKind = EventKind.BATTLE_STARTED
    node_id: DiagnosticText | None


class BattleEnd(StrEnum):
    HANDOFF = "handoff"
    RUN_ENDED = "run_ended"
    INTERRUPTED = "interrupted"
    STOPPED = "stopped"


class BattleFinished(BattleStarted):
    event: EventKind = EventKind.BATTLE_FINISHED
    ending: BattleEnd
    end_reason: RunEndReason | None


class BatchStatus(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    UNKNOWN = "unknown"
    INTERRUPTED = "interrupted"


class BatchSubmitted(Event):
    event: EventKind = EventKind.BATCH_SUBMITTED
    planned: int


class BatchSettled(Event):
    event: EventKind = EventKind.BATCH_SETTLED
    planned: int
    confirmed: int
    outcome: BatchStatus
    reason: ReasonCode | None
    uncertain_step: int | None = None


class Level(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class DiagnosticLine(native.WireModel):
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
    instance_directory: Path | None = None
    user_data_directory: Path | None = None
    user_data_alias: Path | None = None
    game_process_id: int | None = None
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
    native_card_preview: bool | None = None


_ORDINARY_RESULTS = ("rejected", "paused", "stopped")
_SEVERE_ERRORS = (
    "execution_unconfirmed",
    native.ErrorCode.EXECUTION_UNCERTAIN.value,
    "control_unavailable",
    "dispatch_failed",
    "identity_unregistered",
    "catalog_identity_conflict",
    "native_preview_failed",
    "internal_error",
)


def _event_level(event: Event) -> Level:
    if isinstance(event, (EncodingMetadata, NoteBudgetExceeded, MechanismMetrics)):
        return Level.DEBUG
    ordinary = (
        isinstance(event, ResultEvent) and event.outcome is OutcomeStatus.REJECTED
    ) or event.result in _ORDINARY_RESULTS
    if not ordinary and (
        event.event in (EventKind.MODEL_FAILED, EventKind.TOOL_RECOVERY_EXHAUSTED)
        or (
            isinstance(event, (ResultEvent, BatchSettled))
            and event.reason is ReasonCode.CONTROL_UNAVAILABLE
        )
        or (isinstance(event, BattleFinished) and event.ending is BattleEnd.INTERRUPTED)
        or (event.event is EventKind.LIFECYCLE_CHANGED and event.result == Lifecycle.FAILED.value)
        or (isinstance(event, ResultEvent) and event.outcome is OutcomeStatus.UNKNOWN)
        or (
            isinstance(event, BatchSettled)
            and event.outcome in (BatchStatus.UNKNOWN, BatchStatus.INTERRUPTED)
        )
        or event.result == "unknown"
        or event.error in _SEVERE_ERRORS
    ):
        return Level.ERROR
    if isinstance(event, ResultEvent):
        return Level.WARNING if event.outcome is OutcomeStatus.FAILED else Level.INFO
    if event.error is not None and event.result not in _ORDINARY_RESULTS:
        return Level.WARNING
    return Level.INFO


def _event_component(event: Event) -> Literal["game_control", "model", "session"]:
    if isinstance(event, (EncodingMetadata, MechanismMetrics, MechanismMetricsSummary)):
        return "model"
    if isinstance(event, NoteBudgetExceeded):
        return "session"
    if event.event in (
        EventKind.LIFECYCLE_CHANGED,
        EventKind.CONTROL_CHANGED,
        EventKind.ROLE_CHANGED,
        EventKind.ACT_ENTERED,
        EventKind.BATTLE_STARTED,
        EventKind.BATTLE_FINISHED,
    ):
        return "session"
    if event.event in (
        EventKind.MODEL_REQUEST_STARTED,
        EventKind.MODEL_REQUEST_COMPLETED,
        EventKind.MODEL_RETRY,
        EventKind.MODEL_RECOVERED,
        EventKind.MODEL_FAILED,
    ):
        return "model"
    return "game_control"


_SEGMENT_BYTES = 10 * 1024 * 1024
_SINK_RETRY_SECONDS = 1.0


class _BoundedFile(logging.Handler):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._retry_at = 0.0
        self._size = path.stat().st_size if path.exists() else 0

    def emit(self, record: logging.LogRecord) -> None:
        if time.monotonic() < self._retry_at:
            return
        encoded = (self.format(record) + "\n").encode("utf-8")
        # Fixed, bounded event fields keep a structured line below one segment.
        if len(encoded) > _SEGMENT_BYTES:
            return
        try:
            if self._retry_at:
                self._size = self._path.stat().st_size if self._path.exists() else 0
            if self._size + len(encoded) > _SEGMENT_BYTES:
                oldest = self._path.with_name(self._path.name + ".2")
                oldest.unlink(missing_ok=True)
                previous = self._path.with_name(self._path.name + ".1")
                if previous.exists():
                    previous.replace(oldest)
                if self._path.exists():
                    self._path.replace(previous)
                self._size = 0
            with self._path.open("ab") as stream:
                stream.write(encoded)
            self._size += len(encoded)
            self._retry_at = 0.0
        except OSError:
            self._retry_at = time.monotonic() + _SINK_RETRY_SECONDS


class _QuietConsole(logging.StreamHandler[TextIO]):
    def __init__(self, stream: TextIO) -> None:
        super().__init__(stream)
        self._retry_at = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        if time.monotonic() < self._retry_at:
            return
        text = self.format(record) + self.terminator
        try:
            self.stream.write(text)
            self.flush()
            self._retry_at = 0.0
        except (OSError, UnicodeError, ValueError):
            self._retry_at = time.monotonic() + _SINK_RETRY_SECONDS


class RetryChannel(StrEnum):
    NATIVE = "native"
    MODEL = "model"
    DISCOVERY = "discovery"


@dataclass(frozen=True)
class _RetryKey:
    channel: RetryChannel
    sequence: int | None
    endpoint: Endpoint | None


@dataclass
class _RetryReport:
    cause: str
    reported_at: float
    suppressed: int = 0


class Diagnostics:
    def __init__(
        self,
        recording_id: str | None,
        *,
        directory: Path | None = None,
        console: TextIO | None = None,
        control_endpoint: str | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._recording_id = _bounded_text(recording_id) if recording_id is not None else None
        self._directory = directory
        self._logger = logging.Logger("sts2_harness.game_control", level)
        self._logger.propagate = False
        stream = _QuietConsole(console if console is not None else sys.stderr)
        stream.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(stream)
        if directory is not None:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                handler = _BoundedFile(directory / "diagnostics.jsonl")
            except OSError:
                handler = None
            if handler is not None:
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._logger.addHandler(handler)
        self._retry_reports: dict[_RetryKey, _RetryReport] = {}
        self._status_retry_at = 0.0
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
        level = _event_level(event)
        if level is Level.DEBUG and not self._logger.isEnabledFor(logging.DEBUG):
            return
        now = time.monotonic()
        native_event = event.event in (
            EventKind.NATIVE_RETRY,
            EventKind.NATIVE_RECOVERED,
            EventKind.NATIVE_FAILED,
        )
        model_event = event.event in (
            EventKind.MODEL_RETRY,
            EventKind.MODEL_RECOVERED,
            EventKind.MODEL_FAILED,
        )
        discovery_event = event.event in (EventKind.DISCOVERY_RETRY, EventKind.DISCOVERY_RECOVERED)
        channel = (
            RetryChannel.NATIVE
            if native_event
            else RetryChannel.MODEL
            if model_event
            else RetryChannel.DISCOVERY
            if discovery_event
            else None
        )
        retry_suppressed = 0
        if channel is not None:
            key = _RetryKey(channel, event.sequence, event.endpoint)
            previous = self._retry_reports.get(key)
            ending = event.event in (
                EventKind.NATIVE_RECOVERED,
                EventKind.NATIVE_FAILED,
                EventKind.MODEL_RECOVERED,
                EventKind.MODEL_FAILED,
                EventKind.DISCOVERY_RECOVERED,
            )
            if previous is not None:
                if not ending and previous.cause == event.error and now - previous.reported_at < 60:
                    previous.suppressed += 1
                    return
                retry_suppressed = previous.suppressed
            if ending:
                self._retry_reports.pop(key, None)
            elif event.error is not None:
                self._retry_reports[key] = _RetryReport(event.error, now)
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
            if channel is not None
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
        payload = DiagnosticLine(
            time=datetime.now(UTC).isoformat(),
            level=level,
            recording_id=self._recording_id,
            component=_event_component(event),
            suppressed=suppressed,
        )
        if recovered_from is not None:
            event = event.model_copy(update={"recovered_from": recovered_from})
        text = json.dumps(
            payload.model_dump(mode="json", exclude_none=True)
            | event.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._logger.log(logging.getLevelNamesMapping()[level], text)

    def tool_finished(
        self, name: str, outcome: Outcome, *, exception: ExceptionDetails | None = None
    ) -> None:
        try:
            tool = ToolName(name)
        except ValueError:
            tool = None
        reason = (
            None
            if isinstance(outcome, (ReadCompleted, ActionCompleted, RewardBatchCompleted))
            else outcome.reason.code
        )
        self.emit(
            ToolFinished(
                tool=tool,
                error="internal_error" if exception is not None else None,
                exception=exception,
                outcome=outcome.status,
                reason=reason,
                role=None
                if tool is None
                else native.DecisionOwner.RUN
                if tool.value.startswith("run_")
                else native.DecisionOwner.COMBAT,
            )
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

    def headless_instance(
        self, directory: Path, user_directory: Path, alias: Path, process_id: int, port: int
    ) -> None:
        self._write_status(
            self._status.model_copy(
                update={
                    "instance_directory": directory,
                    "user_data_directory": user_directory,
                    "user_data_alias": alias,
                    "game_process_id": process_id,
                    "mod_port": port,
                    "control_endpoint": f"http://127.0.0.1:{port}/control",
                }
            )
        )

    def bind_recording(
        self, recording_id: str | None, location: native.Location | None, archive_path: Path | None
    ) -> None:
        self._recording_id = _bounded_text(recording_id) if recording_id is not None else None
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

    def set_native_card_preview(self, enabled: bool) -> None:
        self._write_status(self._status.model_copy(update={"native_card_preview": enabled}))

    def lifecycle(self, stage: Lifecycle) -> None:
        if self._status.lifecycle is stage:
            return
        self._write_status(self._status.model_copy(update={"lifecycle": stage}))
        self.emit(Event(event=EventKind.LIFECYCLE_CHANGED, result=stage.value))

    def native_status(self, status: native.Status, waiting: bool) -> None:
        changed = self._status.control_intent is not status.control_intent
        self._write_status(
            self._status.model_copy(
                update={
                    "control_intent": status.control_intent,
                }
            )
        )
        if changed:
            self.emit(ControlChanged(intent=status.control_intent))
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
        self._retry_reports = {
            key: report
            for key, report in self._retry_reports.items()
            if key.channel is RetryChannel.DISCOVERY or key.sequence != self._status.sequence
        }
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
        if self._status == status and not self._status_retry_at:
            return
        self._status = status
        if self._directory is None or time.monotonic() < self._status_retry_at:
            return
        try:
            temporary = self._directory / "status.json.tmp"
            temporary.write_text(status.model_dump_json(), encoding="utf-8")
            temporary.replace(self._directory / "status.json")
            self._status_retry_at = 0.0
        except (OSError, UnicodeError):
            self._status_retry_at = time.monotonic() + _SINK_RETRY_SECONDS

    def close(self) -> None:
        for handler in self._logger.handlers:
            handler.close()
