"""One dependency sequence with compressed content-addressed game and model evidence."""

import hashlib
import sqlite3
import zlib
from collections.abc import Awaitable, Callable, Generator
from contextlib import ExitStack, closing, contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError

from sts2_harness.client import models as native
from sts2_harness.diagnostics import Diagnostics, Event, EventKind
from sts2_harness.game import (
    Admit,
    Admitted,
    Catalog,
    Cataloged,
    ControlFailure,
    Endpoint,
    GameFailure,
    GameIO,
    GameReply,
    GameRequest,
    GameSuccess,
    Inspect,
    Inspected,
    IOFailure,
    LifetimeEnded,
    NativeAttempt,
    NativeReply,
    Observe,
    Observed,
    OperationKind,
    Pause,
    ReadFailure,
    Resumed,
    Settle,
    Settlement,
    Stopped,
)
from sts2_harness.journal_models import (
    MATERIAL_REPLY,
    MATERIAL_REQUEST,
    MESSAGE,
    BlobCollector,
    BlobDecoding,
    BlobEncoding,
    BlobKind,
    ConfigurationSnapshot,
    InputDelta,
    MaterialFailed,
    MaterialKind,
    MaterialRecord,
    MaterialReply,
    MaterialRequest,
    MessageSnapshot,
    ModelAttempt,
    ModelRecord,
    ToolSet,
)
from sts2_harness.llm.messages import FailureKind
from sts2_harness.results import Reason


class StandardCharacter(StrEnum):
    IRONCLAD = "IRONCLAD"
    SILENT = "SILENT"
    REGENT = "REGENT"
    NECROBINDER = "NECROBINDER"
    DEFECT = "DEFECT"


class NativeStartReceipt(native.WireModel):
    status: Literal["completed"]
    effect_verified: Literal[True]
    reason_code: str | None
    seed: native.Nonempty


class HeadlessInitialConditions(native.WireModel):
    requested_character: StandardCharacter
    requested_ascension: Annotated[int, Field(ge=0, le=10)]
    effective_settings_sha256: str
    game_assembly_sha256: str
    mod_version: str
    mod_sha256: str
    start_receipt: NativeStartReceipt
    initial_control: native.ControlReply
    initial_observation_sha256: str


class Manifest(native.WireModel):
    schema_version: Literal[6] = 6
    scope: Literal["game_model_material_control"] = "game_model_material_control"
    exact_replay_supported: Literal[False] = False
    model_requests_supported: Literal[True] = True
    recording_id: str
    core_version: str
    game_instance_id: str
    run_id: str
    seed: str | None = None
    template: str | None = None
    initial_notes: str | None = None
    game_version: str | None = None
    headless: HeadlessInitialConditions | None = None


class GapCause(StrEnum):
    STORAGE_UNAVAILABLE = "storage_unavailable"


class RecordingAvailability(native.WireModel):
    manifest_persisted: bool
    error: GapCause | None


class Gap(native.WireModel):
    first_sequence: int
    last_sequence: int
    cause: GapCause = GapCause.STORAGE_UNAVAILABLE


class RecordedResult(native.WireModel):
    outcome: GameSuccess | GameFailure
    observations: tuple[native.Observation, ...]


class AttemptEvidence(NativeAttempt):
    index: int
    encoding_digest: str


class Record(native.WireModel):
    sequence: int
    confirmed_operation_cursor: int
    operation_id: str | None
    request: GameRequest
    reply: RecordedResult | None
    attempts: tuple[AttemptEvidence, ...]


class StoredReply(native.WireModel):
    outcome: str
    observations: tuple[str, ...]


class ArchiveError(Exception):
    """An invalid configuration, unsupported archive, or corrupt recorded evidence."""


_REQUEST = TypeAdapter(GameRequest)
_OUTCOME = TypeAdapter(GameSuccess | GameFailure)
_NATIVE = TypeAdapter(NativeReply)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blobs (digest TEXT PRIMARY KEY,
    content BLOB NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (sequence INTEGER PRIMARY KEY, cursor INTEGER NOT NULL,
    kind TEXT NOT NULL, operation_id TEXT, request TEXT NOT NULL, reply TEXT);
CREATE TABLE IF NOT EXISTS attempts (sequence INTEGER NOT NULL, attempt INTEGER NOT NULL,
    endpoint TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(sequence,attempt));
CREATE TABLE IF NOT EXISTS model_requests (sequence INTEGER PRIMARY KEY, cursor INTEGER NOT NULL,
    role TEXT NOT NULL, stream TEXT NOT NULL, position INTEGER NOT NULL, input TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0, failure TEXT);
CREATE TABLE IF NOT EXISTS model_attempts (sequence INTEGER NOT NULL, attempt INTEGER NOT NULL,
    value TEXT NOT NULL, PRIMARY KEY(sequence,attempt));
CREATE TABLE IF NOT EXISTS materials (sequence INTEGER PRIMARY KEY, cursor INTEGER NOT NULL,
    request TEXT NOT NULL, reply TEXT);
CREATE TABLE IF NOT EXISTS gaps (first_sequence INTEGER PRIMARY KEY,
    last_sequence INTEGER NOT NULL, cause TEXT NOT NULL);
"""


class DependencyKind(StrEnum):
    MODEL = "model"
    MATERIAL = "material"


class Journal:
    def __init__(
        self, path: Path, manifest: Manifest, *, diagnostics: Diagnostics | None = None
    ) -> None:
        self.path = path
        self.manifest = manifest
        self._resources = ExitStack()
        self.diagnostics = diagnostics or Diagnostics.console(manifest.recording_id)
        if diagnostics is None:
            self._resources.callback(self.diagnostics.close)
        self._sequence = 0
        self._stream_bases: dict[str, tuple[int, tuple[str, ...]]] = {}
        self._stream_positions: dict[str, int] = {}
        self._cursor = 0
        self._pending_gaps: list[Gap] = []
        self._active_request: GameRequest | None = None
        self._active_sequence: int | None = None
        self._native_failures: dict[Endpoint, tuple[str, int]] = {}
        self._active_attempt = 0
        self._read_only = False
        self._initialized = False
        self._owns_path = False
        self._storage_error: GapCause | None = None

    @classmethod
    def create(
        cls, path: Path, manifest: Manifest, *, diagnostics: Diagnostics | None = None
    ) -> "Journal":
        if not manifest.recording_id or not manifest.core_version:
            raise ArchiveError("Recording identity and core version are required.")
        if path.exists() or path.is_symlink():
            raise ArchiveError("A recording archive already exists at that path.")
        if path.parent.exists() and not path.parent.is_dir():
            raise ArchiveError("The recording parent must be a directory.")
        supplied = diagnostics
        diagnostics = diagnostics or Diagnostics(manifest.recording_id, directory=path.parent)
        journal = cls(path, manifest, diagnostics=diagnostics)
        if supplied is None:
            journal._resources.callback(diagnostics.close)
        try:
            journal._initialize()
        except (OSError, sqlite3.OperationalError):
            journal._storage_error = GapCause.STORAGE_UNAVAILABLE
            journal.diagnostics.emit(
                Event(event=EventKind.RECORDING_GAP, error=GapCause.STORAGE_UNAVAILABLE.value)
            )
        return journal

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._owns_path:
            try:
                with self.path.open("xb"):
                    pass
            except FileExistsError:
                raise ArchiveError("The recording path is occupied by another archive.") from None
            self._owns_path = True
        with closing(sqlite3.connect(self.path, timeout=0)) as connection, connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO manifest VALUES (1,?)", (self.manifest.model_dump_json(),)
            )
        self._initialized = True

    @property
    def availability(self) -> RecordingAvailability:
        return RecordingAvailability(
            manifest_persisted=self._initialized, error=self._storage_error
        )

    @classmethod
    def open(cls, path: Path) -> "Journal":
        try:
            with closing(cls._read_connection(path)) as connection:
                row = connection.execute("SELECT value FROM manifest").fetchone()
                if row is None or not isinstance(row[0], str):
                    raise ArchiveError("The recording manifest is missing.")
                manifest = Manifest.model_validate_json(row[0])
                sequence, cursor = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0), COALESCE(MAX(cursor),0) FROM ("
                    "SELECT sequence,cursor FROM records UNION ALL "
                    "SELECT sequence,cursor FROM model_requests UNION ALL "
                    "SELECT sequence,cursor FROM materials)"
                ).fetchone()
                if not isinstance(sequence, int) or not isinstance(cursor, int):
                    raise ArchiveError("Invalid recording indexes.")
        except (OSError, sqlite3.Error, ValidationError):
            raise ArchiveError(
                "Cannot read the recording archive or its schema is unsupported."
            ) from None
        journal = cls(path, manifest)
        journal._sequence = sequence
        journal._cursor = cursor
        journal._read_only = True
        journal._initialized = True
        return journal

    @staticmethod
    def _read_connection(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)

    def _blob(self, connection: sqlite3.Connection, value: native.WireModel) -> str:
        collector = BlobCollector()
        content = value.model_dump_json(context=BlobEncoding(collector.collect)).encode()
        for kind, block in collector.blocks.values():
            self._content_blob(connection, block, kind)
        return self._content_blob(connection, content)

    @staticmethod
    def _content_blob(
        connection: sqlite3.Connection,
        content: bytes,
        kind: BlobKind = BlobKind.VALUE,
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO blobs VALUES (?,?,?)",
            (digest, zlib.compress(content), kind.value),
        )
        return digest

    @staticmethod
    def _read_blob(connection: sqlite3.Connection, digest: str) -> bytes:
        row = connection.execute("SELECT content FROM blobs WHERE digest=?", (digest,)).fetchone()
        if row is None or not isinstance(row[0], bytes):
            raise ArchiveError("A recording content block is missing.")
        try:
            content = zlib.decompress(row[0])
        except zlib.error:
            raise ArchiveError("A recording content block is corrupt.") from None
        if hashlib.sha256(content).hexdigest() != digest:
            raise ArchiveError("A recording content block failed its integrity check.")
        return content

    def _allocate(self) -> int:
        if self._read_only:
            raise ArchiveError("An evidence reader cannot resume a recording.")
        if self._active_sequence is not None:
            raise RuntimeError("A recording dependency is already active.")
        self._sequence += 1
        self._active_sequence = self._sequence
        return self._sequence

    def begin_model(
        self,
        role: native.DecisionOwner,
        stream: str,
        messages: tuple[MessageSnapshot, ...],
        tools: ToolSet,
        configuration: ConfigurationSnapshot,
    ) -> int:
        sequence = self._allocate()
        position = self._stream_positions.get(stream, 0) + 1
        self._stream_positions[stream] = position
        collector = BlobCollector()
        encoding = BlobEncoding(collector.collect)
        encoded = tuple(message.model_dump_json(context=encoding).encode() for message in messages)
        digests = tuple(hashlib.sha256(message).hexdigest() for message in encoded)
        previous = self._stream_bases.get(stream)
        prefix = 0
        if previous is not None:
            for left, right in zip(previous[1], digests, strict=False):
                if left != right:
                    break
                prefix += 1
        with self._storage() as connection:
            if connection is not None:
                for kind, block in collector.blocks.values():
                    self._content_blob(connection, block, kind)
                suffix = tuple(
                    self._content_blob(connection, content) for content in encoded[prefix:]
                )
                delta = InputDelta(
                    base_sequence=previous[0] if previous else None,
                    prefix_length=prefix,
                    suffix=suffix,
                    semantic_digest=hashlib.sha256(b"[" + b",".join(encoded) + b"]").hexdigest(),
                    tools=self._blob(connection, tools),
                    configuration=self._blob(connection, configuration),
                )
                connection.execute(
                    "INSERT INTO model_requests VALUES (?,?,?,?,?,?,0,NULL)",
                    (
                        sequence,
                        self._cursor,
                        role.value,
                        stream,
                        position,
                        self._blob(connection, delta),
                    ),
                )
        # A failed transaction must never become the base of a later suffix.
        if self._storage_error is None:
            self._stream_bases[stream] = (sequence, digests)
        return sequence

    def model_attempt(self, sequence: int, attempt: ModelAttempt) -> None:
        with self._storage() as connection:
            if connection is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO model_attempts VALUES (?,?,?)",
                    (sequence, attempt.index, self._blob(connection, attempt)),
                )

    def finish_model(self, sequence: int, *, failure: FailureKind | None = None) -> None:
        with self._storage() as connection:
            if connection is not None:
                connection.execute(
                    "UPDATE model_requests SET delivered=?,failure=? WHERE sequence=?",
                    (int(failure is None), failure.value if failure else None, sequence),
                )

    def end_model(self) -> None:
        self._active_sequence = None

    def begin_material(self, request: MaterialRequest) -> int:
        sequence = self._allocate()
        with self._storage() as connection:
            if connection is not None:
                connection.execute(
                    "INSERT INTO materials VALUES (?,?,?,NULL)",
                    (sequence, self._cursor, self._blob(connection, request)),
                )
        return sequence

    def finish_material(self, sequence: int, reply: MaterialReply) -> None:
        with self._storage() as connection:
            if connection is not None:
                connection.execute(
                    "UPDATE materials SET reply=? WHERE sequence=?",
                    (self._blob(connection, reply), sequence),
                )

        self._active_sequence = None

    def requests(self, *, start: int = 1, stop: int | None = None) -> tuple[ModelRecord, ...]:
        try:
            with closing(self._read_connection(self.path)) as connection:
                rows = connection.execute(
                    "SELECT sequence,cursor,role,stream,position,input,delivered,failure "
                    "FROM model_requests WHERE sequence>=? AND (? IS NULL OR sequence<=?) "
                    "ORDER BY sequence",
                    (start, stop, stop),
                ).fetchall()
                result: list[ModelRecord] = []
                for sequence, cursor, role, stream, position, digest, delivered, failure in rows:
                    delta = InputDelta.model_validate_json(self._read_blob(connection, digest))
                    chain = [delta]
                    seen = {sequence}
                    while chain[-1].base_sequence is not None:
                        base = chain[-1].base_sequence
                        if base in seen:
                            raise ArchiveError("A model input prefix has a cyclic base.")
                        seen.add(base)
                        row = connection.execute(
                            "SELECT input FROM model_requests WHERE sequence=?", (base,)
                        ).fetchone()
                        if row is None:
                            raise ArchiveError("A model input prefix base is missing.")
                        chain.append(
                            InputDelta.model_validate_json(self._read_blob(connection, row[0]))
                        )
                    messages: tuple[str, ...] = ()
                    for part in reversed(chain):
                        if part.prefix_length > len(messages):
                            raise ArchiveError("A model input prefix exceeds its durable base.")
                        messages = messages[: part.prefix_length] + part.suffix
                    content = tuple(self._read_blob(connection, item) for item in messages)
                    semantic_digest = hashlib.sha256(b"[" + b",".join(content) + b"]").hexdigest()
                    if semantic_digest != delta.semantic_digest:
                        raise ArchiveError("A model input failed its semantic integrity check.")
                    result.append(
                        ModelRecord(
                            sequence=sequence,
                            confirmed_operation_cursor=cursor,
                            role=native.DecisionOwner(role),
                            stream=stream,
                            position=position,
                            base_sequence=delta.base_sequence,
                            prefix_length=delta.prefix_length,
                            suffix_length=len(delta.suffix),
                            semantic_digest=delta.semantic_digest,
                            input=tuple(
                                MESSAGE.validate_json(
                                    item,
                                    context=BlobDecoding(
                                        lambda digest: self._read_blob(connection, digest)
                                    ),
                                )
                                for item in content
                            ),
                            tools=ToolSet.model_validate_json(
                                self._read_blob(connection, delta.tools),
                                context=BlobDecoding(
                                    lambda digest: self._read_blob(connection, digest)
                                ),
                            ).tools,
                            configuration=ConfigurationSnapshot.model_validate_json(
                                self._read_blob(connection, delta.configuration)
                            ),
                            attempts=tuple(
                                ModelAttempt.model_validate_json(
                                    self._read_blob(connection, row[0]),
                                    context=BlobDecoding(
                                        lambda digest: self._read_blob(connection, digest)
                                    ),
                                )
                                for row in connection.execute(
                                    "SELECT value FROM model_attempts "
                                    "WHERE sequence=? ORDER BY attempt",
                                    (sequence,),
                                ).fetchall()
                            ),
                            delivered=bool(delivered),
                            failure=FailureKind(failure) if failure else None,
                        )
                    )
                return tuple(result)
        except (OSError, sqlite3.Error, ValidationError, ValueError):
            raise ArchiveError("Cannot decode the requested model evidence.") from None

    def request(self, sequence: int) -> ModelRecord:
        records = self.requests(start=sequence, stop=sequence)
        if not records:
            raise ArchiveError("No recorded model request at that sequence; inspect the gaps.")
        return records[0]

    def materials(self, *, start: int = 1, stop: int | None = None) -> tuple[MaterialRecord, ...]:
        try:
            with closing(self._read_connection(self.path)) as connection:
                return tuple(
                    MaterialRecord(
                        sequence=sequence,
                        confirmed_operation_cursor=cursor,
                        request=MATERIAL_REQUEST.validate_json(
                            self._read_blob(connection, request)
                        ),
                        reply=MATERIAL_REPLY.validate_json(
                            self._read_blob(connection, reply),
                            context=BlobDecoding(
                                lambda digest: self._read_blob(connection, digest)
                            ),
                        )
                        if reply is not None
                        else None,
                    )
                    for sequence, cursor, request, reply in connection.execute(
                        "SELECT sequence,cursor,request,reply FROM materials WHERE sequence>=? "
                        "AND (? IS NULL OR sequence<=?) ORDER BY sequence",
                        (start, stop, stop),
                    ).fetchall()
                )
        except (OSError, sqlite3.Error, ValidationError, ValueError):
            raise ArchiveError("Cannot decode the requested material evidence.") from None

    def begin(self, request: GameRequest) -> None:
        if self._read_only:
            raise ArchiveError("An evidence reader cannot resume a recording.")
        if self._active_request is not None:
            raise RuntimeError("A recording operation is already active.")
        self._allocate()
        self._active_request = request
        self._active_attempt = 0
        opened = False
        try:
            self.diagnostics.started(request.kind, sequence=self._sequence)
            with self._storage() as connection:
                if connection is not None:
                    connection.execute(
                        "INSERT INTO records VALUES (?,?,?,?,?,NULL)",
                        (
                            self._sequence,
                            self._cursor,
                            request.kind.value,
                            None,
                            self._blob(connection, request),
                        ),
                    )
            opened = True
        finally:
            if not opened:
                self.end()

    def submitting(self, operation_id: str) -> None:
        if not isinstance(self._active_request, Settle):
            raise RuntimeError("A native submission requires an active settlement operation.")
        self.diagnostics.submitting(operation_id)
        with self._storage() as connection:
            if connection is not None:
                connection.execute(
                    "UPDATE records SET operation_id=? WHERE sequence=?",
                    (operation_id, self._sequence),
                )

    def received(self, attempt: NativeAttempt) -> None:
        if self._active_request is None:
            raise RuntimeError("A native attempt requires an active recording operation.")
        self._diagnose_attempt(attempt)
        self._active_attempt += 1
        with self._storage() as connection:
            if connection is not None:
                self._write_attempt(connection, self._active_attempt, attempt)

    def _diagnose_attempt(self, attempt: NativeAttempt) -> None:
        if not isinstance(self._active_request, Pause) or attempt.endpoint not in (
            Endpoint.STATUS,
            Endpoint.STATE,
            Endpoint.CONTROL,
        ):
            return
        reply = attempt.reply
        if isinstance(reply, native.ReadError) and reply.error.code is native.ErrorCode.NO_RUN:
            return
        cause = (
            reply.reason.value
            if isinstance(reply, IOFailure)
            else reply.error.code.value
            if isinstance(
                reply, (native.ReadError, native.TechnicalError, native.InvalidRequestReply)
            )
            else None
        )
        previous = self._native_failures.get(attempt.endpoint)
        if cause is not None:
            count = previous[1] + 1 if previous is not None else 1
            self._native_failures[attempt.endpoint] = (cause, count)
            self.diagnostics.emit(
                Event(
                    event=EventKind.NATIVE_RETRY,
                    sequence=self._sequence,
                    endpoint=attempt.endpoint,
                    error=cause,
                    failures=count,
                )
            )
        elif previous is not None:
            del self._native_failures[attempt.endpoint]
            self.diagnostics.emit(
                Event(
                    event=EventKind.NATIVE_RECOVERED,
                    sequence=self._sequence,
                    endpoint=attempt.endpoint,
                    recovered_from=previous[0],
                    failures=previous[1],
                )
            )

    def _write_attempt(
        self, connection: sqlite3.Connection, index: int, attempt: NativeAttempt
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO attempts VALUES (?,?,?,?)",
            (self._sequence, index, attempt.endpoint.value, self._blob(connection, attempt.reply)),
        )

    def finish[T: GameSuccess](self, reply: GameReply[T]) -> None:
        request = self._active_request
        if request is None:
            raise RuntimeError("There is no active recording operation to finish.")
        outcome = reply.outcome
        if isinstance(outcome, Settlement) and isinstance(outcome.receipt, native.ReceiptCompleted):
            self._cursor += 1
        operation_id = outcome.request.operation_id if isinstance(outcome, Settlement) else None
        with self._storage() as connection:
            if connection is not None:
                stored = StoredReply(
                    outcome=self._blob(connection, outcome),
                    observations=tuple(
                        self._blob(connection, observation) for observation in reply.observations
                    ),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)",
                    (
                        self._sequence,
                        self._cursor,
                        request.kind.value,
                        operation_id,
                        self._blob(connection, request),
                        stored.model_dump_json(),
                    ),
                )
        result, error = _result(outcome)
        self.diagnostics.emit(
            Event(
                event=EventKind.OPERATION_COMPLETED,
                sequence=self._sequence,
                operation_id=operation_id,
                result=result.value,
                error=error,
            )
        )

    def end(self) -> None:
        self._active_request = None
        self._active_sequence = None
        self._active_attempt = 0
        self._native_failures.clear()
        self.diagnostics.finished()

    def _mark_gap(self) -> None:
        self._storage_error = GapCause.STORAGE_UNAVAILABLE
        if self._pending_gaps and self._pending_gaps[-1].last_sequence >= self._sequence - 1:
            previous = self._pending_gaps.pop()
            self._pending_gaps.append(
                Gap(first_sequence=previous.first_sequence, last_sequence=self._sequence)
            )
        else:
            self._pending_gaps.append(
                Gap(first_sequence=self._sequence, last_sequence=self._sequence)
            )
        self.diagnostics.emit(
            Event(
                event=EventKind.RECORDING_GAP,
                sequence=self._sequence,
                error=GapCause.STORAGE_UNAVAILABLE.value,
            )
        )

    @contextmanager
    def _storage(self) -> Generator[sqlite3.Connection | None]:
        try:
            if not self._initialized:
                self._initialize()
            connection = sqlite3.connect(self.path, timeout=0)
        except (OSError, sqlite3.OperationalError):
            self._mark_gap()
            yield None
            return
        try:
            for gap in self._pending_gaps:
                connection.execute(
                    "INSERT OR REPLACE INTO gaps VALUES (?,?,?)",
                    (gap.first_sequence, gap.last_sequence, gap.cause.value),
                )
        except (sqlite3.ProgrammingError, sqlite3.IntegrityError):
            connection.close()
            raise
        except (OSError, sqlite3.DatabaseError):
            connection.close()
            self._mark_gap()
            yield None
            return
        try:
            with closing(connection), connection:
                yield connection
        except (sqlite3.ProgrammingError, sqlite3.IntegrityError):
            raise
        except (OSError, sqlite3.DatabaseError):
            self._mark_gap()
        else:
            recovered = self._storage_error is not None
            self._storage_error = None
            self._pending_gaps.clear()
            if recovered:
                self.diagnostics.emit(
                    Event(event=EventKind.RECORDING_RECOVERED, sequence=self._sequence)
                )

    @property
    def gaps(self) -> tuple[Gap, ...]:
        try:
            with closing(self._read_connection(self.path)) as connection:
                rows = connection.execute(
                    "SELECT first_sequence,last_sequence,cause FROM gaps ORDER BY first_sequence"
                ).fetchall()
                persisted = tuple(
                    Gap(first_sequence=first, last_sequence=last, cause=GapCause(cause))
                    for first, last, cause in rows
                )
        except (OSError, sqlite3.Error):
            if self._pending_gaps:
                return tuple(self._pending_gaps)
            raise ArchiveError("Cannot read recording gaps.") from None
        return persisted + tuple(self._pending_gaps)

    def operations(
        self,
        *,
        kind: OperationKind | None = None,
        start: int = 1,
        stop: int | None = None,
        limit: int | None = None,
    ) -> tuple[Record, ...]:
        try:
            with closing(self._read_connection(self.path)) as connection:
                rows = connection.execute(
                    "SELECT sequence,cursor,operation_id,request,reply FROM records "
                    "WHERE sequence>=? "
                    "AND (? IS NULL OR sequence<=?) AND (? IS NULL OR kind=?) "
                    "ORDER BY sequence DESC LIMIT ?",
                    (
                        start,
                        stop,
                        stop,
                        kind.value if kind else None,
                        kind.value if kind else None,
                        limit if limit is not None else -1,
                    ),
                ).fetchall()
                records: list[Record] = []
                for sequence, cursor, operation_id, request_digest, stored_json in reversed(rows):
                    attempts = tuple(
                        AttemptEvidence(
                            index=index,
                            encoding_digest=digest,
                            endpoint=Endpoint(endpoint),
                            reply=_NATIVE.validate_json(self._read_blob(connection, digest)),
                        )
                        for index, endpoint, digest in connection.execute(
                            "SELECT attempt,endpoint,digest FROM attempts "
                            "WHERE sequence=? ORDER BY attempt",
                            (sequence,),
                        ).fetchall()
                    )
                    reply = None
                    if stored_json is not None:
                        stored = StoredReply.model_validate_json(stored_json)
                        reply = RecordedResult(
                            outcome=_OUTCOME.validate_json(
                                self._read_blob(connection, stored.outcome)
                            ),
                            observations=tuple(
                                native.Observation.model_validate_json(
                                    self._read_blob(connection, digest)
                                )
                                for digest in stored.observations
                            ),
                        )
                    records.append(
                        Record(
                            sequence=sequence,
                            confirmed_operation_cursor=cursor,
                            operation_id=operation_id,
                            request=_REQUEST.validate_json(
                                self._read_blob(connection, request_digest)
                            ),
                            reply=reply,
                            attempts=attempts,
                        )
                    )
                return tuple(records)
        except (OSError, sqlite3.Error, ValidationError, ValueError):
            raise ArchiveError("Cannot decode the requested recording evidence.") from None

    def timeline(
        self,
        *,
        kind: OperationKind | DependencyKind | None = None,
        start: int = 1,
        stop: int | None = None,
        limit: int | None = None,
    ) -> tuple[Record | ModelRecord | MaterialRecord, ...]:
        records: list[Record | ModelRecord | MaterialRecord] = []
        if kind is None or isinstance(kind, OperationKind):
            records.extend(self.operations(kind=kind, start=start, stop=stop))
        if kind is None or kind is DependencyKind.MODEL:
            records.extend(self.requests(start=start, stop=stop))
        if kind is None or kind is DependencyKind.MATERIAL:
            records.extend(self.materials(start=start, stop=stop))
        records.sort(key=lambda record: record.sequence)
        return tuple(records[-limit:] if limit is not None else records)

    def operation(self, sequence: int) -> Record:
        records = self.operations(start=sequence, stop=sequence)
        if not records:
            raise ArchiveError("No recorded operation at that sequence; inspect the gaps.")
        return records[0]

    def close(self) -> None:
        if self._active_sequence is not None:
            raise RuntimeError("Cannot close an active recording operation.")
        self._read_only = True
        self._resources.close()

    @property
    def storage(self) -> "StorageSummary":
        try:
            with closing(self._read_connection(self.path)) as connection:
                row = connection.execute(
                    "SELECT COUNT(*),COALESCE(SUM(LENGTH(content)),0), "
                    "COALESCE(SUM(kind='material'),0), COALESCE(SUM(kind='document'),0), "
                    "COALESCE(SUM(kind='native'),0) FROM blobs"
                ).fetchone()
                return StorageSummary(
                    unique_blocks=row[0],
                    compressed_bytes=row[1],
                    material_blocks=row[2],
                    document_blocks=row[3],
                    native_blocks=row[4],
                )
        except (OSError, sqlite3.Error):
            raise ArchiveError("Cannot read recording storage statistics.") from None


class StorageSummary(native.WireModel):
    unique_blocks: int
    compressed_bytes: int
    material_blocks: int
    document_blocks: int
    native_blocks: int


class RecordedGame:
    def __init__(self, live: GameIO, journal: Journal) -> None:
        self._live = live
        self._journal = journal

    async def _record[T: GameSuccess](
        self, request: GameRequest, operation: Callable[[], Awaitable[GameReply[T]]]
    ) -> GameReply[T]:
        self._journal.begin(request)
        try:
            reply = await operation()
            self._journal.finish(reply)
            return reply
        finally:
            self._journal.end()

    async def admit(self) -> GameReply[Admitted]:
        return await self._record(Admit(), self._live.admit)

    async def observe(self) -> GameReply[Observed]:
        return await self._record(Observe(), self._live.observe)

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        return await self._record(Inspect(query=query), lambda: self._live.inspect(query))

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return await self._record(Catalog(request=request), lambda: self._live.catalog(request))

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        return await self._record(
            Settle(route=route, step=step), lambda: self._live.settle(route, step)
        )

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        return await self._record(
            Pause(reason=reason), lambda: self._live.pause_until_resumed(reason)
        )


class ResultKind(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"
    PAUSED = "paused"
    LIFETIME_ENDED = "lifetime_ended"
    INCOMPLETE = "incomplete"
    STOPPED = "stopped"


def _result(outcome: GameSuccess | GameFailure) -> tuple[ResultKind, str | None]:
    if isinstance(outcome, IOFailure):
        return ResultKind.FAILED, outcome.reason.value
    if isinstance(outcome, ReadFailure):
        return ResultKind.FAILED, "read_unavailable"
    if isinstance(outcome, ControlFailure):
        return ResultKind.FAILED, outcome.reason.code.value
    if isinstance(outcome, Stopped):
        return ResultKind.STOPPED, None
    if isinstance(outcome, LifetimeEnded):
        return ResultKind.LIFETIME_ENDED, None
    if isinstance(outcome, Admitted):
        return (ResultKind.COMPLETED if outcome.allowed else ResultKind.PAUSED), None
    if isinstance(outcome, Settlement):
        if outcome.receipt is None:
            return ResultKind.UNKNOWN, "execution_unconfirmed"
        receipt = outcome.receipt
        return ResultKind(receipt.status.value), receipt.error.code.value if isinstance(
            receipt, (native.ReceiptFailed, native.ReceiptUnknown)
        ) else None
    if isinstance(outcome, (Inspected, Cataloged)):
        result = outcome.result
        return ResultKind(result.status.value), result.error.code.value if isinstance(
            result, (native.InspectFailed, native.CatalogFailed, native.TechnicalError)
        ) else None
    return ResultKind.COMPLETED, None


class OperationSummary(native.WireModel):
    sequence: int
    kind: OperationKind
    confirmed_operation_cursor: int
    operation_id: str | None
    result: ResultKind
    physical_attempts: int
    observations: int


class ModelSummary(native.WireModel):
    kind: Literal[DependencyKind.MODEL] = DependencyKind.MODEL
    sequence: int
    confirmed_operation_cursor: int
    role: native.DecisionOwner
    stream: str
    position: int
    delivered: bool
    failure: FailureKind | None
    physical_attempts: int
    input_messages: int


class MaterialSummary(native.WireModel):
    kind: Literal[DependencyKind.MATERIAL] = DependencyKind.MATERIAL
    sequence: int
    confirmed_operation_cursor: int
    operation: MaterialKind
    complete: bool
    failure: str | None


type Summary = OperationSummary | ModelSummary | MaterialSummary


def summarize(record: Record | ModelRecord | MaterialRecord) -> Summary:
    if isinstance(record, ModelRecord):
        return ModelSummary(
            sequence=record.sequence,
            confirmed_operation_cursor=record.confirmed_operation_cursor,
            role=record.role,
            stream=record.stream,
            position=record.position,
            delivered=record.delivered,
            failure=record.failure,
            physical_attempts=len(record.attempts),
            input_messages=len(record.input),
        )
    if isinstance(record, MaterialRecord):
        return MaterialSummary(
            sequence=record.sequence,
            confirmed_operation_cursor=record.confirmed_operation_cursor,
            operation=record.request.kind,
            complete=record.reply is not None,
            failure=record.reply.failure.value
            if isinstance(record.reply, MaterialFailed)
            else None,
        )
    return OperationSummary(
        sequence=record.sequence,
        kind=record.request.kind,
        confirmed_operation_cursor=record.confirmed_operation_cursor,
        operation_id=record.operation_id,
        result=_result(record.reply.outcome)[0]
        if record.reply is not None
        else ResultKind.INCOMPLETE,
        physical_attempts=len(record.attempts),
        observations=len(record.reply.observations) if record.reply is not None else 0,
    )


class Evidence(native.WireModel):
    manifest: Manifest
    gaps: tuple[Gap, ...]
    records: tuple[Record | ModelRecord | MaterialRecord | Summary, ...]


def evidence(
    journal: Journal,
    *,
    expand: bool = False,
    kind: OperationKind | DependencyKind | None = None,
    around: int | None = None,
    window: int = 2,
    sequence: int | None = None,
) -> Evidence:
    if window < 0 or (around is not None and around < 1) or (sequence is not None and sequence < 1):
        raise ArchiveError("Evidence indexes must be positive and the window nonnegative.")
    records = journal.timeline(
        kind=kind,
        start=sequence
        if sequence is not None
        else max(1, around - window)
        if around is not None
        else 1,
        stop=sequence if sequence is not None else around + window if around is not None else None,
        limit=20 if sequence is None and around is None else None,
    )
    if sequence is not None and not records:
        raise ArchiveError("No selected evidence at that sequence; inspect the gaps.")
    return Evidence(
        manifest=journal.manifest,
        gaps=journal.gaps,
        records=records if expand else tuple(summarize(record) for record in records),
    )
