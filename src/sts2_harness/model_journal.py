"""Record model and material dependencies at their actual IO boundaries."""

import hashlib
import time
from typing import Literal

from sts2_harness.capacity import CapacityPolicy, Measurement, ModelAttemptReference
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
    PreparationEvidence,
    PreparationInput,
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
    CapacityRecovery,
    CapacityRejected,
    FailureKind,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    ModelUsageAnchor,
    PreparedRequest,
    RequestPreparation,
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
        recovery_of: ModelAttemptReference | None,
        estimator_id: Literal["input_characters_v1", "reported_usage_bytes_v1"],
    ) -> None:
        self._journal = journal
        self._sequence = sequence
        self._attempt = ModelAttempt(index=1)
        self._role = role
        self._input_digest = input_digest
        self._recovery_of = recovery_of
        self._estimator_id: Literal["input_characters_v1", "reported_usage_bytes_v1"] = estimator_id
        self._failures = 0
        self._last_failure: FailureKind | None = None
        self._started = time.monotonic()

    def started(self, index: int) -> None:
        self._attempt = ModelAttempt(
            index=index,
            input_digest=self._input_digest,
            estimator_id=self._estimator_id,
            cause=(
                AttemptCause.TRANSPORT_RETRY
                if index != 1
                else AttemptCause.CAPACITY_RETRY
                if self._recovery_of is not None
                else AttemptCause.INITIAL
            ),
            recovery_of=self._recovery_of,
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

    def failed(self, reason: FailureKind, *, capacity_signal: str | None = None) -> None:
        self._attempt = self._attempt.model_copy(
            update={"failure": reason, "capacity_signal": capacity_signal}
        )
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

    @property
    def capacity_policy(self) -> CapacityPolicy:
        return self._journal.manifest.capacity_policy

    async def complete(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> ModelReply:
        return await self._complete(input, tools, None)

    def prepare(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        usage_anchor: ModelUsageAnchor | None = None,
    ) -> PreparedRequest:
        return self._live.prepare(input, tools, usage_anchor=usage_anchor)

    async def record_recovery(self, recovery: CapacityRecovery) -> None:
        reference = recovery.rejected.reference
        if reference is None:
            raise RuntimeError("Recorded recovery requires the rejected attempt identity.")
        candidate = recovery.candidate
        candidate_input = (
            PreparationInput(
                messages=archive_input(candidate.input),
                tools=ToolSet(tools=tuple(ToolSnapshot.of(tool) for tool in candidate.tools)),
            )
            if candidate is not None
            else None
        )
        self._journal.record_model_recovery(
            reference,
            recovery.rejected.signal,
            candidate_input,
            candidate.measurement.estimated_input_tokens if candidate is not None else None,
            recovery.comparison,
            recovery.decision,
        )

    async def complete_prepared(
        self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
    ) -> ModelReply:
        return await self._complete(prepared.input, prepared.tools, prepared, preparation)

    async def interrupt_preparation(
        self,
        preparation: RequestPreparation,
        tools: tuple[ToolDefinition, ...],
        failure: ModelUnavailable,
    ) -> None:
        sequence = self._journal.begin_model(
            self._role,
            self._stream,
            archive_input(preparation.input_before),
            ToolSet(tools=tuple(ToolSnapshot.of(tool) for tool in tools)),
            ConfigurationSnapshot.of(self._live.configuration, self._live.capacity_policy),
            preparation=self._preparation(preparation),
        )
        try:
            self._journal.finish_model(sequence, failure=failure.reason)
        finally:
            self._journal.end_model()

    @staticmethod
    def _preparation(preparation: RequestPreparation) -> PreparationEvidence:
        return PreparationEvidence(
            group_id=preparation.group_id,
            baseline=(
                archive_input((preparation.baseline,))[0]
                if preparation.baseline is not None
                else None
            ),
            input_before=archive_input(preparation.input_before),
            estimate_before=preparation.estimate_before,
            decision=preparation.decision,
            input_after=(
                archive_input(preparation.input_after)
                if preparation.input_after is not None
                else None
            ),
            estimate_after=preparation.estimate_after,
            retained_complete_group_id=preparation.retained_complete_group_id,
            active_group_id=preparation.active_group_id,
            validation=preparation.validation,
        )

    async def _complete(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        prepared: PreparedRequest | None,
        preparation: RequestPreparation | None = None,
    ) -> ModelReply:
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
            ConfigurationSnapshot.of(self._live.configuration, self._live.capacity_policy),
            preparation=self._preparation(preparation) if preparation is not None else None,
        )
        recovery_of = preparation.recovery_of if preparation is not None else None
        if recovery_of is not None:
            self._journal.link_model_recovery(recovery_of, sequence)
        attempts = _Attempts(
            self._journal,
            sequence,
            self._role,
            input_digest,
            recovery_of,
            self._live.capacity_policy.estimator_id,
        )
        self._journal.diagnostics.started_model(self._role, sequence)
        try:
            if prepared is None:
                reply = await self._live.complete(input, tools, evidence=attempts)
            else:
                reply = await self._live.complete_prepared(prepared, evidence=attempts)
        except ModelUnavailable as error:
            self._journal.finish_model(sequence, failure=error.reason)
            attempts.finished(error.reason)
            if isinstance(error, CapacityRejected):
                error.sequence = sequence
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
