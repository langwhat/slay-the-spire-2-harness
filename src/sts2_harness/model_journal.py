"""Record model and material dependencies at their actual IO boundaries."""

import hashlib
import time

from sts2_harness.capacity import Measurement
from sts2_harness.client import models as native
from sts2_harness.diagnostics import EncodingMetadata, Event, EventKind
from sts2_harness.journal import Journal
from sts2_harness.journal_models import (
    AttemptCause,
    BlobCollector,
    BlobEncoding,
    ConfigurationSnapshot,
    DocumentLookup,
    DocumentLookupResult,
    DocumentRead,
    DocumentReadResult,
    DocumentSnapshot,
    MaterialFailed,
    MaterialFailure,
    MaterialSnapshot,
    ModelAttempt,
    PromptRead,
    PromptReadResult,
    ReplySnapshot,
    SkillListing,
    SkillListingResult,
    SkillSnapshot,
    ToolSet,
    ToolSnapshot,
    archive_input,
)
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    SkillRead,
    SkillSummary,
    SubjectId,
    UnknownDocument,
)
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    FailureKind,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    ToolDefinition,
)
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.prompts import PromptReader


class _Attempts:
    def __init__(
        self,
        journal: Journal,
        sequence: int,
        role: native.DecisionOwner,
        input_digest: str,
    ) -> None:
        self._journal = journal
        self._sequence = sequence
        self._attempt = ModelAttempt(index=1)
        self._role = role
        self._input_digest = input_digest
        self._failures = 0
        self._last_failure: FailureKind | None = None
        self._started = time.monotonic()

    def started(self, index: int) -> None:
        self._attempt = ModelAttempt(
            index=index,
            input_digest=self._input_digest,
            cause=AttemptCause.INITIAL if index == 1 else AttemptCause.TRANSPORT_RETRY,
        )
        self._journal.model_attempt(self._sequence, self._attempt)
        if self._last_failure is not None:
            self._journal.diagnostics.emit(
                Event(
                    event=EventKind.MODEL_RETRY,
                    sequence=self._sequence,
                    role=self._role,
                    attempt=self._attempt.index,
                    error=self._last_failure.value,
                    failures=self._failures,
                )
            )

    def encoded(self, measurement: Measurement) -> None:
        self._journal.diagnostics.emit(
            EncodingMetadata(sequence=self._sequence, digest=measurement.digest)
        )
        self._attempt = self._attempt.model_copy(
            update={
                "protocol_encoding_digest": measurement.digest,
                "estimated_input_tokens": measurement.estimated_input_tokens,
            }
        )
        self._journal.model_attempt(self._sequence, self._attempt)

    def received(self, reply: ModelReply) -> None:
        self._attempt = self._attempt.model_copy(update={"reply": ReplySnapshot.of(reply)})
        self._journal.model_attempt(self._sequence, self._attempt)

    def failed(self, reason: FailureKind) -> None:
        self._attempt = self._attempt.model_copy(update={"failure": reason})
        self._journal.model_attempt(self._sequence, self._attempt)
        self._failures += 1
        self._last_failure = reason

    def finished(self, failure: FailureKind | None = None) -> None:
        if self._failures or failure is not None:
            self._journal.diagnostics.emit(
                Event(
                    event=EventKind.MODEL_FAILED if failure else EventKind.MODEL_RECOVERED,
                    sequence=self._sequence,
                    role=self._role,
                    attempt=self._attempt.index,
                    error=failure.value if failure else None,
                    failures=self._failures,
                    recovered_from=self._last_failure.value if self._last_failure else None,
                )
            )
        self._journal.diagnostics.emit(
            Event(
                event=EventKind.MODEL_REQUEST_COMPLETED,
                sequence=self._sequence,
                role=self._role,
                result="failed" if failure else "completed",
                elapsed_seconds=time.monotonic() - self._started,
            )
        )


class RecordedLlm:
    def __init__(
        self, live: Llm, journal: Journal, *, role: native.DecisionOwner, stream: str
    ) -> None:
        self._live = live
        self._journal = journal
        self._role = role
        self._stream = stream

    async def complete(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> ModelReply:
        messages = archive_input(input)
        tool_set = ToolSet(tools=tuple(ToolSnapshot.of(tool) for tool in tools))
        collector = BlobCollector()
        encoding = BlobEncoding(collector.collect)
        serialized = tuple(
            message.model_dump_json(context=encoding).encode() for message in messages
        )
        input_digest = hashlib.sha256(
            b"["
            + b",".join(serialized)
            + b"]"
            + tool_set.model_dump_json(context=encoding).encode()
        ).hexdigest()
        sequence = self._journal.begin_model(
            self._role,
            self._stream,
            messages,
            tool_set,
            ConfigurationSnapshot.of(self._live.configuration),
        )
        attempts = _Attempts(self._journal, sequence, self._role, input_digest)
        self._journal.diagnostics.started_model(self._role, sequence)
        try:
            reply = await self._live.complete(input, tools, evidence=attempts)
        except ModelUnavailable as error:
            self._journal.finish_model(sequence, failure=error.reason)
            attempts.finished(error.reason)
            raise
        else:
            self._journal.finish_model(sequence)
            attempts.finished()
            return reply
        finally:
            self._journal.end_model()
            self._journal.diagnostics.finished()


class RecordedPrompts:
    def __init__(self, live: PromptReader, journal: Journal) -> None:
        self._live = live
        self._journal = journal

    def read(self, name: str) -> MaterialText:
        sequence = self._journal.begin_material(PromptRead(name=name))
        try:
            material = self._live.read(name)
        except MaterialUnavailable as error:
            self._journal.finish_material(
                sequence, MaterialFailed(failure=MaterialFailure.UNAVAILABLE, message=str(error))
            )
            raise
        self._journal.finish_material(
            sequence, PromptReadResult(prompt=MaterialSnapshot.of(material))
        )
        return material


class RecordedDocuments:
    def __init__(self, live: Documents, journal: Journal) -> None:
        self._live = live
        self._journal = journal

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        sequence = self._journal.begin_material(SkillListing(role=role))
        try:
            skills = await self._live.list(role)
        except MaterialUnavailable as error:
            self._journal.finish_material(
                sequence, MaterialFailed(failure=MaterialFailure.UNAVAILABLE, message=str(error))
            )
            raise
        self._journal.finish_material(
            sequence, SkillListingResult(skills=tuple(SkillSnapshot.of(skill) for skill in skills))
        )
        return skills

    async def read(self, request: SkillRead) -> DocumentView:
        sequence = self._journal.begin_material(
            DocumentRead(id=request.id, resource=request.resource)
        )
        try:
            document = await self._live.read(request)
        except UnknownDocument as error:
            self._journal.finish_material(
                sequence, MaterialFailed(failure=MaterialFailure.UNKNOWN, message=str(error))
            )
            raise
        except MaterialUnavailable as error:
            self._journal.finish_material(
                sequence, MaterialFailed(failure=MaterialFailure.UNAVAILABLE, message=str(error))
            )
            raise
        self._journal.finish_material(
            sequence, DocumentReadResult(document=DocumentSnapshot.of(document))
        )
        return document

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        sequence = self._journal.begin_material(DocumentLookup(subjects=subjects))
        try:
            documents = await self._live.lookup(subjects)
        except MaterialUnavailable as error:
            self._journal.finish_material(
                sequence, MaterialFailed(failure=MaterialFailure.UNAVAILABLE, message=str(error))
            )
            raise
        self._journal.finish_material(
            sequence,
            DocumentLookupResult(
                documents=tuple(DocumentSnapshot.of(document) for document in documents)
            ),
        )
        return documents
