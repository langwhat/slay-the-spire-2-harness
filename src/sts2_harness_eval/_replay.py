"""Rebuild software state by consuming one immutable dependency prefix."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager, closing, suppress
from pathlib import Path
from typing import Never

from pydantic import MISSING

from sts2_harness.capacity import CapacityPolicy, ModelAttemptReference, RecoveryDecision
from sts2_harness.client import models as native
from sts2_harness.game import (
    Admit,
    Admitted,
    CardRewardClosed,
    CardRewardOpened,
    Catalog,
    Cataloged,
    CloseCardReward,
    GameReply,
    GameRequest,
    GameSuccess,
    Inspect,
    Inspected,
    Observe,
    Observed,
    OpenCardReward,
    OperationKind,
    Pause,
    Present,
    Presented,
    Resumed,
    SessionStopped,
    Settle,
    Settlement,
)
from sts2_harness.journal import (
    ArchiveError,
    DependencyKind,
    HistoricalVariant,
    Journal,
    ParentPrefix,
    Record,
    SceneVariant,
)
from sts2_harness.journal_models import (
    AttemptCause,
    ConfigurationSnapshot,
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
    PreparationBaseline,
    PreparationEvidence,
    PreparationInput,
    PromptRead,
    PromptReadResult,
    RecordedRecovery,
    SkillListing,
    SkillListingResult,
    ToolSet,
    ToolSnapshot,
    VariantPrefixOutcome,
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
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    AssistantMessage,
    CapacityRecovery,
    CapacityRejected,
    ModelClient,
    ModelInput,
    ModelMetadata,
    ModelReply,
    ModelUnavailable,
    ModelUsageAnchor,
    PreparedRequest,
    RequestPreparation,
    ToolDefinition,
    Usage,
)
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.results import HandoffCompletion, Reason
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
from sts2_harness_eval.variants import Materials, VariantError

type Dependency = Record | ModelRecord | MaterialRecord


class ReplayRejected(Exception):
    def __init__(self, result: Rejected) -> None:
        self.result = result
        super().__init__(result.reason.value)


def unsupported(sequence: int | None, cause: UnsupportedCause) -> Never:
    raise ReplayRejected(Rejected(RejectReason.UNSUPPORTED_HISTORY, sequence, Unsupported(cause)))


def _different(sequence: int, component: InputComponent) -> Never:
    raise ReplayRejected(
        Rejected(RejectReason.INPUT_MISMATCH, sequence, InputDifference(component))
    )


def _kind(record: Dependency) -> DependencyKind | OperationKind:
    if isinstance(record, ModelRecord):
        return DependencyKind.MODEL
    if isinstance(record, MaterialRecord):
        return DependencyKind.MATERIAL
    return record.request.kind


def _preparation_evidence(preparation: RequestPreparation) -> PreparationEvidence:
    return PreparationEvidence(
        group_id=preparation.group_id,
        baseline=archive_input((preparation.baseline,))[0] if preparation.baseline else None,
        input_before=archive_input(preparation.input_before),
        estimate_before=preparation.estimate_before,
        decision=preparation.decision,
        input_after=(
            archive_input(preparation.input_after) if preparation.input_after is not None else None
        ),
        estimate_after=preparation.estimate_after,
        retained_complete_group_id=preparation.retained_complete_group_id,
        active_group_id=preparation.active_group_id,
        validation=preparation.validation,
    )


class Timeline:
    def __init__(
        self,
        records: tuple[Dependency, ...],
        materials: Materials | None = None,
        original_before: int | None = None,
        variant_journal: Journal | None = None,
        record_variant: bool = False,
    ) -> None:
        self.records = records
        self.index = 0
        self.cursor = 0
        self.materials = materials
        self.original_before = original_before
        self.variant_journal = variant_journal
        self.record_variant = record_variant
        self.variant_preparations = (
            variant_journal.variant_preparations() if variant_journal is not None else ()
        )

    def needs_variant_preparation(self, sequence: int) -> bool:
        journal = self.variant_journal
        if self.materials is None or journal is None:
            return False
        parent = journal.manifest.parent
        return parent is not None and sequence <= parent.before_sequence

    def preparation(
        self,
        source: ModelRecord,
        preparation: RequestPreparation | None,
        prepared: PreparedRequest | None,
        tools: tuple[ToolDefinition, ...],
        outcome: VariantPrefixOutcome,
        reply_sequence: int | None,
    ) -> None:
        journal = self.variant_journal
        if journal is None or preparation is None:
            unsupported(source.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        evidence = _preparation_evidence(preparation)
        tool_set = ToolSet(tools=tuple(ToolSnapshot.of(tool) for tool in tools))
        digest = prepared.measurement.digest if prepared is not None else None
        if self.record_variant:
            journal.record_variant_preparation(
                source,
                evidence,
                tool_set,
                protocol_encoding_digest=digest,
                outcome=outcome,
                source_reply_sequence=reply_sequence,
            )
            return
        expected = next(
            (
                item
                for item in self.variant_preparations
                if item.source_model_sequence == source.sequence
            ),
            None,
        )
        if expected is None:
            unsupported(source.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        if (
            expected.configuration != source.configuration
            or (
                expected.role,
                expected.stream,
                expected.position,
                expected.confirmed_operation_cursor,
            )
            != (source.role, source.stream, source.position, source.confirmed_operation_cursor)
            or expected.before != PreparationInput(messages=evidence.input_before, tools=tool_set)
            or expected.after
            != (
                PreparationInput(messages=evidence.input_after, tools=tool_set)
                if evidence.input_after is not None
                else None
            )
            or expected.baseline
            != (PreparationBaseline(message=evidence.baseline) if evidence.baseline else None)
            or expected.protocol_encoding_digest != digest
            or expected.outcome is not outcome
            or expected.source_reply_sequence != reply_sequence
            or any(
                getattr(expected, key) != getattr(evidence, key)
                for key in (
                    "group_id",
                    "estimate_before",
                    "decision",
                    "estimate_after",
                    "retained_complete_group_id",
                    "active_group_id",
                    "validation",
                )
            )
        ):
            _different(source.sequence, InputComponent.MODEL_INPUT)

    def original(self, sequence: int) -> bool:
        return self.original_before is None or sequence < self.original_before

    def take(self, kind: DependencyKind | OperationKind) -> Dependency:
        if self.index == len(self.records):
            unsupported(None, UnsupportedCause.UNEXPECTED_END)
        record = self.records[self.index]
        if _kind(record) is not kind:
            raise ReplayRejected(
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
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        reply = record.reply
        if self.materials is None or not self.original(record.sequence):
            return reply
        try:
            return self.materials.reply(reply)
        except VariantError as error:
            raise ReplayRejected(error.rejection) from error

    def check_cursor(self, record: Dependency) -> None:
        if record.confirmed_operation_cursor != self.cursor:
            _different(record.sequence, InputComponent.CURSOR)


class ReplayGame:
    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline

    def _reply[T: GameSuccess](self, request: GameRequest, success: type[T]) -> GameReply[T]:
        record = self._timeline.take(request.kind)
        assert isinstance(record, Record)
        if record.request != request:
            _different(record.sequence, InputComponent.REQUEST)
        reply = record.reply
        if reply is None:
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        outcome = reply.outcome
        if isinstance(request, Present) and isinstance(outcome, Presented):
            if (
                not record.operation_id
                or outcome.request.operation_id != record.operation_id
                or outcome.request.expected_route != request.route
                or outcome.request.edges != request.edges
            ):
                _different(record.sequence, InputComponent.OPERATION_ID)
            if outcome.receipt is None:
                unsupported(record.sequence, UnsupportedCause.UNKNOWN_WRITE)
            if (
                isinstance(
                    outcome.receipt, (native.PresentationCompleted, native.PresentationRejected)
                )
                and outcome.receipt.operation_id != record.operation_id
            ):
                _different(record.sequence, InputComponent.OPERATION_ID)
        if isinstance(request, Settle):
            if isinstance(outcome, Settlement):
                if (
                    not record.operation_id
                    or outcome.request.operation_id != record.operation_id
                    or outcome.request.expected_route != request.route
                    or outcome.request.step != request.step
                ):
                    _different(record.sequence, InputComponent.OPERATION_ID)
                if outcome.receipt is None or isinstance(outcome.receipt, native.ReceiptUnknown):
                    unsupported(record.sequence, UnsupportedCause.UNKNOWN_WRITE)
                if outcome.receipt.operation_id != record.operation_id:
                    _different(record.sequence, InputComponent.OPERATION_ID)
                if isinstance(outcome.receipt, native.ReceiptCompleted):
                    self._timeline.cursor += 1
        self._timeline.check_cursor(record)
        if isinstance(
            outcome,
            (
                Admitted,
                Observed,
                Inspected,
                Cataloged,
                Settlement,
                Resumed,
                Presented,
                CardRewardOpened,
                CardRewardClosed,
            ),
        ):
            if not isinstance(outcome, success):
                _different(record.sequence, InputComponent.REQUEST)
            return GameReply[T](outcome=outcome, observations=reply.observations)
        return GameReply[T](outcome=outcome, observations=reply.observations)

    @property
    def card_reward_preview_enabled(self) -> bool:
        return any(
            isinstance(record, Record) and isinstance(record.request, OpenCardReward)
            for record in self._timeline.records
        )

    def start_open_card_reward(self, route: native.RouteKey, offer: str) -> None:
        return None

    async def cancel_open_card_reward(self) -> None:
        return None

    async def admit(self) -> GameReply[Admitted]:
        return self._reply(Admit(), Admitted)

    async def observe(self) -> GameReply[Observed]:
        return self._reply(Observe(), Observed)

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        return self._reply(Inspect(query=query), Inspected)

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return self._reply(Catalog(request=request), Cataloged)

    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> GameReply[Presented]:
        return self._reply(Present(route=route, edges=edges), Presented)

    async def open_card_reward(
        self, route: native.RouteKey, offer: str
    ) -> GameReply[CardRewardOpened]:
        return self._reply(OpenCardReward(route=route, offer=offer), CardRewardOpened)

    async def close_card_reward(
        self, route: native.RouteKey, offer: str
    ) -> GameReply[CardRewardClosed]:
        return self._reply(CloseCardReward(route=route, offer=offer), CardRewardClosed)

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        return self._reply(Settle(route=route, step=step), Settlement)

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        return self._reply(Pause(reason=reason), Resumed)


def _text(snapshot: MaterialSnapshot) -> MaterialText:
    return MaterialText(id=snapshot.id, version=snapshot.version, body=snapshot.body)


def document_view(snapshot: DocumentSnapshot) -> DocumentView:
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


class ReplayPrompts:
    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline

    def read(self, name: str) -> MaterialText:
        reply = self._timeline.material(PromptRead(name=name))
        if isinstance(reply, MaterialFailed):
            raise MaterialUnavailable(reply.message)
        if not isinstance(reply, PromptReadResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        return _text(reply.prompt)


class ReplayDocuments:
    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        reply = self._timeline.material(SkillListing(role=role))
        if isinstance(reply, MaterialFailed):
            raise MaterialUnavailable(reply.message)
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
                raise UnknownDocument(reply.message)
            raise MaterialUnavailable(reply.message)
        if not isinstance(reply, DocumentReadResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        return document_view(reply.document)

    async def _lookup_snapshots(
        self, subjects: tuple[SubjectId, ...]
    ) -> tuple[DocumentSnapshot, ...]:
        reply = self._timeline.material(DocumentLookup(subjects=subjects))
        if isinstance(reply, MaterialFailed):
            raise MaterialUnavailable(reply.message)
        if not isinstance(reply, DocumentLookupResult):
            _different(self._timeline.sequence, InputComponent.REQUEST)
        return reply.documents

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        documents: list[Document] = []
        for snapshot in await self._lookup_snapshots(subjects):
            if snapshot.subjects is MISSING or snapshot.auto_roles is MISSING:
                unsupported(self._timeline.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
            view = document_view(snapshot)
            documents.append(
                Document(
                    id=view.id,
                    name=view.name,
                    text=view.text,
                    resources=view.resources,
                    resource=view.resource,
                    subjects=snapshot.subjects,
                    auto_roles=snapshot.auto_roles,
                )
            )
        return tuple(documents)


class ReplayModel:
    def __init__(
        self,
        timeline: Timeline,
        role: native.DecisionOwner,
        stream: str,
        target: ModelAnchor,
        reached: asyncio.Future[Reached],
        continuation: Callable[
            [
                ModelInput,
                tuple[ToolDefinition, ...],
                RequestPreparation | None,
                PreparedRequest | None,
            ],
            Awaitable[ModelReply],
        ]
        | None = None,
    ) -> None:
        self._timeline = timeline
        self._role = role
        self._stream = stream
        self._position = 0
        self._target = target
        self._reached = reached
        self._continuation = continuation
        self._preparers: dict[str, Llm] = {}
        self._rejected_record: ModelRecord | None = None

    def _next_record(self) -> ModelRecord:
        for record in self._timeline.records[self._timeline.index :]:
            if isinstance(record, ModelRecord) and (record.role, record.stream) == (
                self._role,
                self._stream,
            ):
                return record
        unsupported(None, UnsupportedCause.UNEXPECTED_END)

    def _next_configuration(self) -> ConfigurationSnapshot:
        if self._rejected_record is not None:
            return self._rejected_record.configuration
        return self._next_record().configuration

    @property
    def capacity_policy(self) -> CapacityPolicy:
        return self._next_configuration().capacity_policy

    def prepare(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        usage_anchor: ModelUsageAnchor | None = None,
    ) -> PreparedRequest:
        return self._encode(input, tools, self._next_configuration(), usage_anchor)

    def _encode(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        configuration: ConfigurationSnapshot,
        usage_anchor: ModelUsageAnchor | None = None,
    ) -> PreparedRequest:
        # Snapshots are mutable Pydantic values, so identify preparers by their serialized value.
        key = configuration.model_dump_json()
        preparer = self._preparers.get(key)
        if preparer is None:
            preparer = Llm(configuration.for_offline_preparation())
            self._preparers[key] = preparer
        return preparer.prepare(input, tools, usage_anchor=usage_anchor)

    async def record_recovery(self, recovery: CapacityRecovery) -> None:
        record = self._rejected_record
        if record is None or record.capacity_recovery is None:
            unsupported(None, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        expected: RecordedRecovery = record.capacity_recovery
        candidate = recovery.candidate
        actual = (
            PreparationInput(
                messages=archive_input(candidate.input),
                tools=ToolSet(tools=tuple(ToolSnapshot.of(tool) for tool in candidate.tools)),
            )
            if candidate is not None
            else None
        )
        if (
            expected.rejected != recovery.rejected.reference
            or expected.signal != recovery.rejected.signal
            or expected.candidate_input != actual
            or expected.candidate_estimate
            != (candidate.measurement.estimated_input_tokens if candidate is not None else None)
            or expected.comparison is not recovery.comparison
            or expected.decision is not recovery.decision
            or (expected.decision is RecoveryDecision.RETRY)
            != (expected.retry_sequence is not None)
        ):
            _different(record.sequence, InputComponent.MODEL_INPUT)
        self._rejected_record = None
        if expected.retry_sequence is not None:
            next_record = self._next_record()
            if (
                next_record.sequence != expected.retry_sequence
                or next_record.confirmed_operation_cursor != record.confirmed_operation_cursor
                or not next_record.attempts
                or any(
                    attempt.index != index for index, attempt in enumerate(next_record.attempts, 1)
                )
                or next_record.attempts[0].cause is not AttemptCause.CAPACITY_RETRY
                or next_record.attempts[0].recovery_of != expected.rejected
            ):
                unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)

    async def record_handoff(
        self, reference: ModelAttemptReference, completion: HandoffCompletion
    ) -> None:
        return None

    def _check_preparation(
        self, record: ModelRecord, preparation: RequestPreparation | None
    ) -> None:
        if self._timeline.materials is not None and self._timeline.original(record.sequence):
            return
        expected = record.preparation
        if preparation is None:
            if expected is not None:
                _different(record.sequence, InputComponent.MODEL_INPUT)
            return
        if expected is None:
            _different(record.sequence, InputComponent.MODEL_INPUT)
        actual = _preparation_evidence(preparation)
        archived = PreparationEvidence.model_validate(
            expected.model_dump(include=set(PreparationEvidence.model_fields))
        )
        if record.attempts and all(
            attempt.estimator_id == "input_characters_v1" for attempt in record.attempts
        ):
            archived = archived.model_copy(
                update={
                    "estimate_before": actual.estimate_before,
                    "estimate_after": actual.estimate_after,
                }
            )
        if actual != archived:
            _different(record.sequence, InputComponent.MODEL_INPUT)

    async def complete_prepared(
        self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
    ) -> ModelReply:
        self._check_preparation(self._next_record(), preparation)
        return await self.complete(
            prepared.input, prepared.tools, preparation=preparation, prepared=prepared
        )

    async def interrupt_preparation(
        self,
        preparation: RequestPreparation,
        tools: tuple[ToolDefinition, ...],
        failure: ModelUnavailable,
    ) -> None:
        record = self._timeline.take(DependencyKind.MODEL)
        assert isinstance(record, ModelRecord)
        self._timeline.check_cursor(record)
        if (self._role, self._stream) != (record.role, record.stream):
            _different(record.sequence, InputComponent.ROLE_STREAM)
        self._position += 1
        if record.position != self._position:
            _different(record.sequence, InputComponent.POSITION)
        if self._timeline.materials is not None and self._timeline.original(record.sequence):
            candidate = (
                self._encode(preparation.input_after, tools, record.configuration)
                if preparation.input_after is not None
                else None
            )
            self._timeline.preparation(
                record, preparation, candidate, tools, VariantPrefixOutcome.INTERRUPTED, None
            )
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        self._check_preparation(record, preparation)
        if tuple(ToolSnapshot.of(tool) for tool in tools) != record.tools:
            _different(record.sequence, InputComponent.TOOLS)
        if record.attempts or record.delivered or record.failure is not failure.reason:
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)

    async def close(self) -> None:
        for preparer in self._preparers.values():
            await preparer.__aexit__(None, None, None)

    async def complete(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        preparation: RequestPreparation | None = None,
        prepared: PreparedRequest | None = None,
    ) -> ModelReply:
        record = self._timeline.take(DependencyKind.MODEL)
        assert isinstance(record, ModelRecord)
        if any(attempt.index != index for index, attempt in enumerate(record.attempts, 1)):
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        self._timeline.check_cursor(record)
        if (self._role, self._stream) != (record.role, record.stream):
            _different(record.sequence, InputComponent.ROLE_STREAM)
        self._position += 1
        if record.position != self._position:
            _different(record.sequence, InputComponent.POSITION)
        messages = archive_input(input)
        materials = self._timeline.materials
        treated = materials is not None and self._timeline.original(record.sequence)
        expected_input = (
            materials.messages(record.input) if treated and materials is not None else record.input
        )
        expected_tools = (
            materials.tool_snapshots(record.tools)
            if treated and materials is not None
            else record.tools
        )
        if not treated and messages != expected_input:
            _different(record.sequence, InputComponent.MODEL_INPUT)
        if tuple(ToolSnapshot.of(tool) for tool in tools) != expected_tools:
            _different(record.sequence, InputComponent.TOOLS)
        if not treated and record.attempts:
            measurement = (
                prepared.measurement
                if prepared is not None
                else self._encode(input, tools, record.configuration).measurement
            )
            if any(
                attempt.protocol_encoding_digest is not None
                and attempt.protocol_encoding_digest != measurement.digest
                or attempt.estimated_input_tokens is not None
                and attempt.estimator_id == "reported_usage_bytes_v1"
                and attempt.estimated_input_tokens != measurement.estimated_input_tokens
                for attempt in record.attempts
            ):
                _different(record.sequence, InputComponent.MODEL_INPUT)
        source_record = record
        if treated and record.capacity_recovery is not None:
            if record.sequence == self._target.model_sequence:
                unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
            recovery = record.capacity_recovery
            retry = self._timeline.take(DependencyKind.MODEL)
            if not isinstance(retry, ModelRecord) or (
                recovery.decision is not RecoveryDecision.RETRY
                or recovery.retry_sequence != retry.sequence
                or retry.sequence >= self._target.model_sequence
                or (retry.role, retry.stream, retry.position, retry.confirmed_operation_cursor)
                != (
                    record.role,
                    record.stream,
                    record.position + 1,
                    record.confirmed_operation_cursor,
                )
                or retry.configuration != record.configuration
                or not retry.attempts
                or retry.attempts[0].cause is not AttemptCause.CAPACITY_RETRY
                or retry.attempts[0].recovery_of != recovery.rejected
                or not retry.delivered
                or retry.failure is not None
            ):
                unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
            self._position += 1
            record = retry
        if treated and record.failure is not None:
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        if self._timeline.needs_variant_preparation(source_record.sequence):
            journal = self._timeline.variant_journal
            assert journal is not None and journal.manifest.parent is not None
            prefix_anchor = journal.manifest.parent.before_sequence
            self._timeline.preparation(
                source_record,
                preparation,
                self._encode(input, tools, source_record.configuration),
                tools,
                (
                    VariantPrefixOutcome.ANCHOR_READY
                    if source_record.sequence == prefix_anchor
                    else VariantPrefixOutcome.SOURCE_REPLY
                ),
                None if source_record.sequence == prefix_anchor else record.sequence,
            )
        if record.sequence == self._target.model_sequence:
            # Native admission stays bound to the source request digest.
            self._reached.set_result(
                Reached(self._target, self._timeline.cursor, record.semantic_digest)
            )
            if self._continuation is not None:
                return await self._continuation(input, tools, preparation, prepared)
            await asyncio.Future[None]()
        if record.failure is not None:
            if record.attempts and record.attempts[-1].capacity_signal is not None:
                attempt = record.attempts[-1]
                assert attempt.capacity_signal is not None
                if not record.configuration.capacity_policy.capacity_classifier_config.supports(
                    record.configuration.protocol, attempt.capacity_signal
                ):
                    unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
                rejection = CapacityRejected(attempt.capacity_signal, attempt=attempt.index)
                rejection.sequence = record.sequence
                self._rejected_record = record
                raise rejection
            raise ModelUnavailable(record.failure)
        if not record.delivered or not record.attempts:
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        attempt = record.attempts[-1]
        if attempt.reply is None or attempt.failure is not None:
            unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
        reply = attempt.reply
        return ModelReply(
            AssistantMessage(reply.native_reply),
            Usage()
            if treated
            else Usage(
                reply.input_tokens,
                reply.output_tokens,
                reply.cache_read_tokens,
                reply.cache_write_tokens,
                reply.reasoning_tokens,
            ),
            ModelMetadata(
                reply.requested_model,
                reply.requested_effort,
                reply.response_model,
                reply.response_service_tier,
            ),
        )


async def _run(
    journal: Journal, timeline: Timeline, target: ModelAnchor, reached: asyncio.Future[Reached]
) -> None:
    models: list[ReplayModel] = []

    def model(role: ModelPurpose, stream: str) -> ModelClient:
        historical = ReplayModel(timeline, role.owner, stream, target, reached)
        models.append(historical)

        class Model:
            @property
            def capacity_policy(self) -> CapacityPolicy:
                return historical.capacity_policy

            def prepare(
                self,
                input: ModelInput,
                tools: tuple[ToolDefinition, ...],
                *,
                usage_anchor: ModelUsageAnchor | None = None,
            ) -> PreparedRequest:
                materials = timeline.materials
                return historical.prepare(
                    input,
                    materials.tools(tools) if materials is not None else tools,
                    usage_anchor=usage_anchor,
                )

            async def record_recovery(self, recovery: CapacityRecovery) -> None:
                await historical.record_recovery(recovery)

            async def record_handoff(
                self, reference: ModelAttemptReference, completion: HandoffCompletion
            ) -> None:
                await historical.record_handoff(reference, completion)

            async def complete_prepared(
                self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
            ) -> ModelReply:
                return await historical.complete_prepared(prepared, preparation=preparation)

            async def interrupt_preparation(
                self,
                preparation: RequestPreparation,
                tools: tuple[ToolDefinition, ...],
                failure: ModelUnavailable,
            ) -> None:
                materials = timeline.materials
                await historical.interrupt_preparation(
                    preparation, materials.tools(tools) if materials is not None else tools, failure
                )

            async def complete(
                self, input: ModelInput, tools: tuple[ToolDefinition, ...]
            ) -> ModelReply:
                materials = timeline.materials
                return await historical.complete(
                    input, materials.tools(tools) if materials is not None else tools
                )

        return Model()

    dependencies = SessionDependencies(
        ReplayGame(timeline),
        ReplayPrompts(timeline),
        ReplayDocuments(timeline),
        model,
    )
    try:
        async with RunSession(journal.manifest, dependencies) as session:
            first = timeline.records[0]
            if isinstance(first, Record) and first.request == Pause():
                await session.start()
            elif not isinstance(first, Record) or first.request != Admit():
                unsupported(first.sequence, UnsupportedCause.START_MODE)
            await session.run()
    except SessionStopped:
        unsupported(None, UnsupportedCause.UNEXPECTED_END)
    finally:
        for historical in models:
            await historical.close()
    unsupported(None, UnsupportedCause.UNEXPECTED_END)


def prefix(journal: Journal, target: ModelAnchor) -> tuple[Dependency, ...]:
    if journal.manifest.recording_id != target.recording_id or not any(
        summary.sequence == target.model_sequence
        for summary in journal.summaries(kind=DependencyKind.MODEL, stop=target.model_sequence)
    ):
        raise ReplayRejected(
            Rejected(RejectReason.TARGET_NOT_FOUND, target.model_sequence, MissingTarget(target))
        )
    if journal.manifest.initial_notes is None:
        unsupported(None, UnsupportedCause.MISSING_INITIAL_NOTES)
    if journal.manifest.run_context_policy != "act_local_v1":
        unsupported(None, UnsupportedCause.RUN_CONTEXT_POLICY)
    # Replayed game reads answer from recorded replies; attempt evidence is not consulted.
    records = journal.timeline(stop=target.model_sequence, attempts=False)
    declaration = journal.manifest.variant
    if isinstance(declaration, SceneVariant):
        admitted = next((record for record in records if isinstance(record, Record)), None)
        if (
            admitted is None
            or admitted.request != Admit()
            or admitted.reply is None
            or not isinstance(admitted.reply.outcome, Admitted)
            or not admitted.reply.outcome.allowed
            or not admitted.reply.observations
        ):
            unsupported(None, UnsupportedCause.START_MODE)
        observation = admitted.reply.observations[-1]
        initial = declaration.initial.observation
        if observation.model_copy(update={"stamp": initial.stamp}) != initial:
            _different(admitted.sequence, InputComponent.REQUEST)
    if any(gap.first_sequence <= target.model_sequence for gap in journal.gaps):
        unsupported(None, UnsupportedCause.PREFIX_GAP)
    for expected, record in enumerate(records, 1):
        if record.sequence != expected:
            unsupported(expected, UnsupportedCause.PREFIX_GAP)
    return records


def material_boundary(journal: Journal) -> int | None:
    """Find the original segment through the existing immutable parent chain."""
    boundary: int | None = 0 if isinstance(journal.manifest.variant, SceneVariant) else None
    with ExitStack() as resources:
        current = journal
        while isinstance(current.manifest.variant, HistoricalVariant):
            parent = current.manifest.parent
            if parent is None:
                raise ArchiveError("A historical material variant requires its parent prefix.")
            boundary = (
                min(boundary, parent.before_sequence)
                if boundary is not None
                else parent.before_sequence
            )
            current = resources.enter_context(closing(Journal.open(parent.archive)))
    return boundary


@asynccontextmanager
async def reach(archive: Path, target: ModelAnchor) -> AsyncGenerator[ReplayResult]:
    """Validate the prefix and hold its original RunSession at the target until context exit."""
    journal: Journal | None = None
    task: asyncio.Task[None] | None = None
    result: ReplayResult
    try:
        try:
            journal = Journal.open(archive)
            records = prefix(journal, target)
            reached = asyncio.get_running_loop().create_future()
            declaration = journal.manifest.variant
            if isinstance(declaration, HistoricalVariant):
                await verify_source(declaration.source)
            materials = Materials(declaration.replacements) if declaration is not None else None
            original_before = material_boundary(journal)
            task = asyncio.create_task(
                _run(
                    journal,
                    Timeline(records, materials, original_before, variant_journal=journal),
                    target,
                    reached,
                )
            )
            await asyncio.wait((task, reached), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
            result = reached.result()
        except ReplayRejected as error:
            result = error.result
        except VariantError as error:
            result = error.rejection
        except ArchiveError as error:
            result = Rejected(RejectReason.UNSUPPORTED_HISTORY, None, ArchiveUnreadable(str(error)))
        yield result
    finally:
        try:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, ReplayRejected):
                    await task
        finally:
            if journal is not None:
                journal.close()


async def verify_source(source: ParentPrefix) -> None:
    with closing(Journal.open(source.archive)) as journal:
        for record in prefix(journal, ModelAnchor(source.recording_id, source.before_sequence)):
            if isinstance(record, ModelRecord) and any(
                attempt.protocol_encoding_digest is None or attempt.estimated_input_tokens is None
                for attempt in record.attempts
            ):
                unsupported(record.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
    async with reach(
        source.archive, ModelAnchor(source.recording_id, source.before_sequence)
    ) as result:
        if isinstance(result, Rejected):
            raise ReplayRejected(result)
