"""Rebuild software state by consuming one immutable dependency prefix."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from pathlib import Path
from typing import Never

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.game import (
    Admit,
    Admitted,
    Catalog,
    Cataloged,
    GameReply,
    GameRequest,
    GameSuccess,
    Inspect,
    Inspected,
    Observe,
    Observed,
    OperationKind,
    Pause,
    Resumed,
    SessionStopped,
    Settle,
    Settlement,
)
from sts2_harness.journal import ArchiveError, DependencyKind, Journal, Record
from sts2_harness.journal_models import (
    BlobCollector,
    BlobEncoding,
    DocumentLookup,
    DocumentLookupResult,
    DocumentRead,
    DocumentReadResult,
    DocumentSnapshot,
    MaterialFailed,
    MaterialFailure,
    MaterialRecord,
    MaterialReply,
    MaterialRequest,
    MaterialSnapshot,
    ModelRecord,
    PromptRead,
    PromptReadResult,
    SkillListing,
    SkillListingResult,
    ToolSnapshot,
    archive_input,
)
from sts2_harness.knowledge import (
    Document,
    DocumentView,
    Resource,
    SkillRead,
    SkillSummary,
    SubjectId,
    UnknownDocument,
)
from sts2_harness.llm.messages import (
    AssistantMessage,
    ModelClient,
    ModelInput,
    ModelMetadata,
    ModelReply,
    ModelUnavailable,
    ToolDefinition,
    Usage,
)
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.results import Reason
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval.results import (
    ArchiveUnreadable,
    InputComponent,
    InputDifference,
    MissingTarget,
    ModelAnchor,
    OrderDifference,
    Reached,
    Rejected,
    RejectReason,
    ReplayResult,
    Unsupported,
    UnsupportedCause,
)

type Dependency = Record | ModelRecord | MaterialRecord


class _ReplayRejected(Exception):
    def __init__(self, result: Rejected) -> None:
        self.result = result
        super().__init__(result.reason.value)


def _unsupported(sequence: int | None, cause: UnsupportedCause) -> Never:
    raise _ReplayRejected(Rejected(RejectReason.UNSUPPORTED_HISTORY, sequence, Unsupported(cause)))


def _different(sequence: int, component: InputComponent) -> Never:
    raise _ReplayRejected(
        Rejected(RejectReason.INPUT_MISMATCH, sequence, InputDifference(component))
    )


def _kind(record: Dependency) -> DependencyKind | OperationKind:
    if isinstance(record, ModelRecord):
        return DependencyKind.MODEL
    if isinstance(record, MaterialRecord):
        return DependencyKind.MATERIAL
    return record.request.kind


class _Timeline:
    def __init__(self, records: tuple[Dependency, ...]) -> None:
        self.records = records
        self.index = 0
        self.cursor = 0

    def take(self, kind: DependencyKind | OperationKind) -> Dependency:
        if self.index == len(self.records):
            _unsupported(None, UnsupportedCause.UNEXPECTED_END)
        record = self.records[self.index]
        if _kind(record) is not kind:
            raise _ReplayRejected(
                Rejected(
                    RejectReason.ORDER_MISMATCH,
                    record.sequence,
                    OrderDifference(_kind(record), kind),
                )
            )
        self.index += 1
        return record

    @property
    def sequence(self) -> int:
        return self.records[self.index - 1].sequence

    def material(self, request: MaterialRequest) -> MaterialReply:
        record = self.take(DependencyKind.MATERIAL)
        assert isinstance(record, MaterialRecord)
        self.check_cursor(record)
        if record.request != request:
            _different(record.sequence, InputComponent.REQUEST)
        if record.reply is None:
            _unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        return record.reply

    def check_cursor(self, record: Dependency) -> None:
        if record.confirmed_operation_cursor != self.cursor:
            _different(record.sequence, InputComponent.CURSOR)


class _Game:
    def __init__(self, timeline: _Timeline, namespace: str) -> None:
        self._timeline = timeline
        self._namespace = namespace
        self._settles = 0

    def _reply[T: GameSuccess](self, request: GameRequest, success: type[T]) -> GameReply[T]:
        record = self._timeline.take(request.kind)
        assert isinstance(record, Record)
        if record.request != request:
            _different(record.sequence, InputComponent.REQUEST)
        reply = record.reply
        if reply is None:
            _unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        outcome = reply.outcome
        if isinstance(request, Settle):
            self._settles += 1
            if isinstance(outcome, Settlement):
                expected = native.StepRequest(
                    operation_id=f"{self._namespace}:{self._settles}",
                    expected_route=request.route,
                    step=request.step,
                )
                if outcome.request != expected or record.operation_id != expected.operation_id:
                    _different(record.sequence, InputComponent.OPERATION_ID)
                if outcome.receipt is None or isinstance(outcome.receipt, native.ReceiptUnknown):
                    _unsupported(record.sequence, UnsupportedCause.UNKNOWN_WRITE)
                if outcome.receipt.operation_id != expected.operation_id:
                    _different(record.sequence, InputComponent.OPERATION_ID)
                if isinstance(outcome.receipt, native.ReceiptCompleted):
                    self._timeline.cursor += 1
        self._timeline.check_cursor(record)
        if isinstance(outcome, (Admitted, Observed, Inspected, Cataloged, Settlement, Resumed)):
            if not isinstance(outcome, success):
                _different(record.sequence, InputComponent.REQUEST)
            return GameReply[T](outcome=outcome, observations=reply.observations)
        return GameReply[T](outcome=outcome, observations=reply.observations)

    async def admit(self) -> GameReply[Admitted]:
        return self._reply(Admit(), Admitted)

    async def observe(self) -> GameReply[Observed]:
        return self._reply(Observe(), Observed)

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        return self._reply(Inspect(query=query), Inspected)

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return self._reply(Catalog(request=request), Cataloged)

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        return self._reply(Settle(route=route, step=step), Settlement)

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        return self._reply(Pause(reason=reason), Resumed)


def _text(snapshot: MaterialSnapshot) -> MaterialText:
    return MaterialText(id=snapshot.id, version=snapshot.version, body=snapshot.body)


def _document(snapshot: DocumentSnapshot) -> DocumentView:
    return DocumentView(
        id=snapshot.id,
        name=_text(snapshot.name),
        text=_text(snapshot.text),
        resources=tuple(
            Resource(key=item.key, description=_text(item.description))
            for item in snapshot.resources
        ),
        resource=snapshot.resource,
    )


class _Prompts:
    def __init__(self, timeline: _Timeline) -> None:
        self._timeline = timeline

    def read(self, name: str) -> MaterialText:
        reply = self._timeline.material(PromptRead(name=name))
        if isinstance(reply, MaterialFailed):
            raise MaterialUnavailable("The required gameplay instructions could not be read.")
        if not isinstance(reply, PromptReadResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        return _text(reply.prompt)


class _Documents:
    def __init__(self, timeline: _Timeline) -> None:
        self._timeline = timeline

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        reply = self._timeline.material(SkillListing(role=role))
        if isinstance(reply, MaterialFailed):
            raise MaterialUnavailable("The enabled document source could not be read.")
        if not isinstance(reply, SkillListingResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        return tuple(
            SkillSummary(id=skill.id, name=_text(skill.name), description=_text(skill.description))
            for skill in reply.skills
        )

    async def read(self, request: SkillRead) -> DocumentView:
        reply = self._timeline.material(DocumentRead(id=request.id, resource=request.resource))
        if isinstance(reply, MaterialFailed):
            if reply.failure is MaterialFailure.UNKNOWN:
                raise UnknownDocument(f"Document {request.id!r} is not configured.")
            label = f"Document {request.id!r}"
            if request.resource is not None:
                label += f" resource {request.resource!r}"
            raise MaterialUnavailable(f"{label} could not be read.")
        if not isinstance(reply, DocumentReadResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        return _document(reply.document)

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        reply = self._timeline.material(DocumentLookup(subjects=subjects))
        if isinstance(reply, MaterialFailed):
            raise MaterialUnavailable("The enabled supplementary source could not be read.")
        if not isinstance(reply, DocumentLookupResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        documents: list[Document] = []
        for snapshot in reply.documents:
            if snapshot.subjects is MISSING:
                _unsupported(self._timeline.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
            view = _document(snapshot)
            documents.append(
                Document(
                    id=view.id,
                    name=view.name,
                    text=view.text,
                    resources=view.resources,
                    resource=view.resource,
                    subjects=snapshot.subjects,
                )
            )
        return tuple(documents)


class _Model:
    def __init__(
        self,
        timeline: _Timeline,
        role: native.DecisionOwner,
        stream: str,
        target: ModelAnchor,
        reached: asyncio.Future[Reached],
    ) -> None:
        self._timeline = timeline
        self._role = role
        self._stream = stream
        self._position = 0
        self._target = target
        self._reached = reached

    async def complete(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> ModelReply:
        record = self._timeline.take(DependencyKind.MODEL)
        assert isinstance(record, ModelRecord)
        self._timeline.check_cursor(record)
        if (self._role, self._stream) != (record.role, record.stream):
            _different(record.sequence, InputComponent.ROLE_STREAM)
        self._position += 1
        if record.position != self._position:
            _different(record.sequence, InputComponent.POSITION)
        messages = archive_input(input)
        if messages != record.input:
            _different(record.sequence, InputComponent.MODEL_INPUT)
        if tuple(ToolSnapshot.of(tool) for tool in tools) != record.tools:
            _different(record.sequence, InputComponent.TOOLS)
        encoding = BlobEncoding(BlobCollector().collect)
        encoded = tuple(message.model_dump_json(context=encoding).encode() for message in messages)
        digest = sha256(b"[" + b",".join(encoded) + b"]").hexdigest()
        if digest != record.semantic_digest:
            _different(record.sequence, InputComponent.SEMANTIC_DIGEST)
        if record.sequence == self._target.model_sequence:
            self._reached.set_result(Reached(self._target, self._timeline.cursor, digest))
            await asyncio.Future[None]()
        if record.failure is not None:
            raise ModelUnavailable(record.failure)
        if not record.delivered or not record.attempts:
            _unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        attempt = record.attempts[-1]
        if attempt.reply is None or attempt.failure is not None:
            _unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        reply = attempt.reply
        return ModelReply(
            AssistantMessage(reply.native_reply),
            Usage(
                reply.input_tokens,
                reply.output_tokens,
                reply.cache_read_tokens,
                reply.cache_write_tokens,
            ),
            ModelMetadata(
                reply.requested_model,
                reply.requested_effort,
                reply.response_model,
                reply.response_service_tier,
            ),
        )


async def _run(
    journal: Journal, timeline: _Timeline, target: ModelAnchor, reached: asyncio.Future[Reached]
) -> None:
    def model(role: native.DecisionOwner, stream: str) -> ModelClient:
        return _Model(timeline, role, stream, target, reached)

    dependencies = SessionDependencies(
        _Game(timeline, journal.manifest.recording_id),
        _Prompts(timeline),
        _Documents(timeline),
        model,
    )
    try:
        async with RunSession(journal.manifest, dependencies) as session:
            first = timeline.records[0]
            if isinstance(first, Record) and first.request == Pause():
                await session.start()
            elif not isinstance(first, Record) or first.request != Admit():
                _unsupported(first.sequence, UnsupportedCause.START_MODE)
            await session.run()
    except SessionStopped:
        _unsupported(None, UnsupportedCause.UNEXPECTED_END)
    _unsupported(None, UnsupportedCause.UNEXPECTED_END)


@asynccontextmanager
async def reach(archive: Path, target: ModelAnchor) -> AsyncGenerator[ReplayResult]:
    """Validate the prefix and hold its original RunSession at the target until context exit."""
    journal: Journal | None = None
    task: asyncio.Task[None] | None = None
    result: ReplayResult
    try:
        try:
            journal = Journal.open(archive)
            if journal.manifest.recording_id != target.recording_id or not any(
                summary.sequence == target.model_sequence
                for summary in journal.summaries(
                    kind=DependencyKind.MODEL, stop=target.model_sequence
                )
            ):
                raise _ReplayRejected(
                    Rejected(
                        RejectReason.TARGET_NOT_FOUND, target.model_sequence, MissingTarget(target)
                    )
                )
            if journal.manifest.initial_notes is None:
                _unsupported(None, UnsupportedCause.MISSING_INITIAL_NOTES)
            records = journal.timeline(stop=target.model_sequence)
            if any(gap.first_sequence <= target.model_sequence for gap in journal.gaps):
                _unsupported(None, UnsupportedCause.PREFIX_GAP)
            for expected, record in enumerate(records, 1):
                if record.sequence != expected:
                    _unsupported(expected, UnsupportedCause.PREFIX_GAP)
            reached = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(_run(journal, _Timeline(records), target, reached))
            await asyncio.wait((task, reached), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
            result = reached.result()
        except _ReplayRejected as error:
            result = error.result
        except ArchiveError as error:
            result = Rejected(RejectReason.UNSUPPORTED_HISTORY, None, ArchiveUnreadable(str(error)))
        yield result
    finally:
        try:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, _ReplayRejected):
                    await task
        finally:
            if journal is not None:
                journal.close()
