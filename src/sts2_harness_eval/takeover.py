"""Resume one verified historical request through its original run coroutine."""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack, closing, suppress
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx
from pydantic import JsonValue, ValidationError

from sts2_harness.capacity import CapacityPolicy, ModelAttemptReference
from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.game import (
    Admit,
    Admitted,
    CardRewardClosed,
    CardRewardOpened,
    Cataloged,
    ControlWriteError,
    GameLifetimeEnded,
    GameReadError,
    GameReply,
    Inspected,
    LiveGame,
    Observed,
    Pause,
    Presented,
    Resumed,
    SessionStopped,
    Settlement,
)
from sts2_harness.headless import HeadlessError
from sts2_harness.journal import (
    CURRENT_ARCHIVE_SCHEMA,
    ArchiveError,
    HistoricalVariant,
    Journal,
    Manifest,
    ParentPrefix,
    Record,
    RecordedGame,
    SceneSource,
    SceneVariant,
    current_core_identity,
)
from sts2_harness.journal_models import AssistantSnapshot, MaterialRecord, ModelRecord
from sts2_harness.knowledge import Documents
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    CapacityRecovery,
    ModelClient,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    ModelUsageAnchor,
    PreparedRequest,
    RequestPreparation,
    ToolDefinition,
)
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.native_template import TemplateError
from sts2_harness.prompts import PromptReader
from sts2_harness.results import HandoffCompletion, Reason
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval._material_boundary import RejectingDocuments, RejectingPrompts
from sts2_harness_eval._replay import (
    Dependency,
    ReplayDocuments,
    ReplayGame,
    ReplayModel,
    ReplayPrompts,
    ReplayRejected,
    Timeline,
    material_boundary,
    prefix,
    unsupported,
    verify_source,
)
from sts2_harness_eval._scene import SceneSession
from sts2_harness_eval.cases import (
    AdmissionFailure,
    AdmissionPlan,
    AdmissionRequest,
    AfterDecisionsSpec,
    Case,
    CaseEndpoint,
    IncompleteCause,
    Mismatch,
    RunEndSpec,
    Stage,
    plan_admission,
)
from sts2_harness_eval.cases import (
    Unsupported as AdmissionUnsupported,
)
from sts2_harness_eval.cases import (
    UnsupportedCause as AdmissionUnsupportedCause,
)
from sts2_harness_eval.checkpoints import (
    CheckpointCause,
    CheckpointRejected,
    CheckpointUncertain,
)
from sts2_harness_eval.identity import IdentityGame, TargetIdentity
from sts2_harness_eval.native import NativeRestorer
from sts2_harness_eval.results import (
    InputComponent,
    InputDifference,
    Reached,
    Rejected,
    RejectReason,
    UnsupportedCause,
)
from sts2_harness_eval.sample import (
    SETTLE_CONFIRMATION_SECONDS,
    AfterDecisions,
    BattleEnd,
    Completed,
    Endpoint,
    Incomplete,
    OperatorStopped,
    RunEnd,
    SampleBudget,
    SampleHost,
    SampleResult,
    finish_after_stop,
)
from sts2_harness_eval.variants import (
    Materials,
    MaterialVariant,
    SceneStart,
    VariantDocuments,
    VariantError,
    VariantPrompts,
    replacements,
)
from sts2_harness_eval.verification import VerificationFailure


@dataclass(frozen=True, slots=True)
class ProtocolMismatch:
    recorded: Protocol
    configured: Protocol


@dataclass(frozen=True, slots=True)
class NativeUncertain:
    stage: Stage
    cause: IncompleteCause
    message: str


type Refusal = Rejected | AdmissionUnsupported | Mismatch | CheckpointCause | ProtocolMismatch


@dataclass(frozen=True, slots=True)
class TakeoverCompleted:
    branch: Path
    anchor: Reached
    sample: Completed
    recording_id: str


@dataclass(frozen=True, slots=True)
class TakeoverIncomplete:
    branch: Path
    anchor: Reached | None
    evidence: Incomplete | NativeUncertain | OperatorStopped
    recording_id: str | None


@dataclass(frozen=True, slots=True)
class TakeoverUnsupported:
    branch: Path
    evidence: Refusal
    recording_id: str | None


type TakeoverResult = TakeoverCompleted | TakeoverIncomplete | TakeoverUnsupported


class _Switch:
    def __init__(self, replay: SessionDependencies) -> None:
        self.current = replay
        self.live = False


class _GameProxy:
    def __init__(self, switch: _Switch) -> None:
        self._switch = switch

    @property
    def card_reward_preview_enabled(self) -> bool:
        return self._switch.current.game.card_reward_preview_enabled

    def pending_card_reward_open(self) -> tuple[native.RouteKey, str] | None:
        return self._switch.current.game.pending_card_reward_open()

    def start_open_card_reward(self, route: native.RouteKey, offer: str) -> None:
        self._switch.current.game.start_open_card_reward(route, offer)

    async def cancel_open_card_reward(self) -> None:
        await self._switch.current.game.cancel_open_card_reward()

    async def admit(self) -> GameReply[Admitted]:
        return await self._switch.current.game.admit()

    async def observe(self) -> GameReply[Observed]:
        return await self._switch.current.game.observe()

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        return await self._switch.current.game.inspect(query)

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return await self._switch.current.game.catalog(request)

    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> GameReply[Presented]:
        return await self._switch.current.game.present(route, edges)

    async def open_card_reward(
        self, route: native.RouteKey, offer: str
    ) -> GameReply[CardRewardOpened]:
        return await self._switch.current.game.open_card_reward(route, offer)

    async def close_card_reward(
        self, route: native.RouteKey, offer: str
    ) -> GameReply[CardRewardClosed]:
        return await self._switch.current.game.close_card_reward(route, offer)

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        return await self._switch.current.game.settle(route, step)

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        return await self._switch.current.game.pause_until_resumed(reason)


async def take_over(
    case: Case,
    restorer: NativeRestorer,
    branch: Path,
    model: Llm,
    prompts: PromptReader,
    documents: Documents,
    budget: SampleBudget,
    *,
    stop_requested: Callable[[], bool],
    variant: MaterialVariant | None = None,
) -> TakeoverResult:
    """Verify both prefixes, then release the held request into recorded live dependencies."""
    return await _Takeover(
        case, restorer, branch, model, prompts, documents, budget, stop_requested, variant
    ).run()


def _model_body(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: value for key, value in body.items() if key != "prompt_cache_key"}


def _sample_endpoint(spec: CaseEndpoint, location: native.Location) -> Endpoint:
    if isinstance(spec, AfterDecisionsSpec):
        return AfterDecisions(spec.count)
    if isinstance(spec, RunEndSpec):
        return RunEnd()
    assert location.node_id is not None
    return BattleEnd(location.node_id)


class _Takeover:
    def __init__(
        self,
        case: Case,
        restorer: NativeRestorer,
        branch: Path,
        model: Llm,
        prompts: PromptReader,
        documents: Documents,
        budget: SampleBudget,
        stop_requested: Callable[[], bool],
        variant: MaterialVariant | None,
    ) -> None:
        self.case = case
        self.restorer = restorer
        self.branch = branch
        self.model = model
        self.prompts = prompts
        self.documents = documents
        self.budget = budget
        self.stop_requested = stop_requested
        self.variant = variant
        self.anchor: Reached | None = None
        self.recording_id: str | None = None
        self.stage = Stage.PREPARATION
        self.operator_stopped = False
        self.caller_cancellation: asyncio.CancelledError | None = None

    async def run(self) -> TakeoverResult:
        caller = asyncio.current_task()
        assert caller is not None
        initial_cancellations = caller.cancelling()
        try:
            with ExitStack() as resources:
                plan = plan_admission(
                    AdmissionRequest(
                        id=self.case.id,
                        archive=self.case.source.archive,
                        anchor=self.case.source.anchor,
                        endpoint=self.case.endpoint,
                    ),
                    self.restorer.store.directory,
                )
                if plan.source != self.case.source:
                    raise ReplayRejected(
                        Rejected(
                            RejectReason.INPUT_MISMATCH,
                            self.case.source.anchor.model_sequence,
                            InputDifference(InputComponent.REQUEST),
                        )
                    )
                source = resources.enter_context(closing(Journal.open(self.case.source.archive)))
                records = prefix(source, self.case.source.anchor)
                target = source.request(self.case.source.anchor.model_sequence)
                if self.variant is not None and isinstance(self.variant.start, SceneStart):
                    return await self._scene(
                        resources, plan, source, records, self.variant, self.variant.start
                    )
                return await self._historical(resources, plan, source, records, target)

        except SessionStopped:
            if not self.operator_stopped:
                raise
            return TakeoverIncomplete(
                self.branch, self.anchor, OperatorStopped(), recording_id=self.recording_id
            )
        except ReplayRejected as error:
            return TakeoverUnsupported(self.branch, error.result, recording_id=self.recording_id)
        except VariantError as error:
            return TakeoverUnsupported(self.branch, error.rejection, recording_id=self.recording_id)
        except AdmissionFailure as error:
            return TakeoverUnsupported(self.branch, error.result, recording_id=self.recording_id)
        except VerificationFailure as error:
            return TakeoverUnsupported(
                self.branch,
                Mismatch(
                    relation=error.relation,
                    sequence=error.sequence,
                    field_path=error.field_path,
                ),
                recording_id=self.recording_id,
            )
        except CheckpointRejected as error:
            return TakeoverUnsupported(self.branch, error.cause, recording_id=self.recording_id)
        except (
            CheckpointUncertain,
            ClientUnavailable,
            GameReadError,
            ControlWriteError,
            GameLifetimeEnded,
        ) as error:
            return TakeoverIncomplete(
                self.branch,
                self.anchor,
                NativeUncertain(
                    self.stage,
                    IncompleteCause.NATIVE_UNCERTAIN,
                    str(error),
                ),
                recording_id=self.recording_id,
            )
        except (OSError, ArchiveError, httpx.HTTPError, TimeoutError) as error:
            return TakeoverIncomplete(
                self.branch,
                self.anchor,
                NativeUncertain(self.stage, IncompleteCause.IO, str(error)),
                recording_id=self.recording_id,
            )
        except ValidationError as error:
            return TakeoverIncomplete(
                self.branch,
                self.anchor,
                NativeUncertain(self.stage, IncompleteCause.PROTOCOL, str(error)),
                recording_id=self.recording_id,
            )
        except (HeadlessError, TemplateError) as error:
            return TakeoverIncomplete(
                self.branch,
                self.anchor,
                NativeUncertain(self.stage, IncompleteCause.STARTUP, str(error)),
                recording_id=self.recording_id,
            )
        finally:
            # Cleanup failures must not turn a caller cancellation into a normal result.
            if self.caller_cancellation is not None:
                raise self.caller_cancellation
            if caller.cancelling() > initial_cancellations:
                raise asyncio.CancelledError

    async def _finish(self, task: asyncio.Task[SampleResult]) -> SampleResult:
        def stop() -> None:
            self.operator_stopped = True

        try:
            return await finish_after_stop(task, stop)
        except asyncio.CancelledError as error:
            self.caller_cancellation = error
            raise

    async def _scene(
        self,
        resources: ExitStack,
        plan: AdmissionPlan,
        source: Journal,
        records: tuple[Dependency, ...],
        variant: MaterialVariant,
        start: SceneStart,
    ) -> TakeoverResult:
        if source.manifest.variant is not None:
            raise VariantError("New scenes require an original verified case.")
        declared = replacements(variant)
        materials = Materials(declared)
        materials.preflight(
            tuple(record for record in records if isinstance(record, (ModelRecord, MaterialRecord)))
        )
        if not isinstance(plan.target.state, native.CombatState):
            return TakeoverUnsupported(
                self.branch,
                AdmissionUnsupported(
                    cause=AdmissionUnsupportedCause.TARGET,
                    sequence=self.case.source.anchor.model_sequence,
                ),
                recording_id=None,
            )
        digest = Journal.archive_sha256(self.case.source.archive)
        self.stage = Stage.LOAD
        async with self.restorer.loaded(self.case) as loaded:
            location = loaded.read.observation.location
            assert location.run_id is not None
            if loaded.initial.observation != loaded.read.observation:
                raise CheckpointUncertain("The scene target differs from its loaded evidence.")
            control = await loaded.client.control(native.ControlIntent.RUNNING)
            if (
                not isinstance(control, native.ControlReply)
                or control.mode is not native.ControlIntent.RUNNING
            ):
                raise CheckpointUncertain("The live Start intent was not confirmed.")
            initial = loaded.initial.model_copy(update={"control": control})
            declaration = SceneVariant(
                case_id=self.case.id,
                case_sha256=sha256(self.case.model_dump_json().encode()).hexdigest(),
                source=SceneSource(
                    archive=self.case.source.archive.resolve(),
                    recording_id=self.case.source.anchor.recording_id,
                    model_sequence=self.case.source.anchor.model_sequence,
                    archive_sha256=digest,
                ),
                replacements=declared,
                initial=initial,
            )
            manifest = Manifest(
                minimal=plan.manifest.minimal,
                capacity_policy=self.model.capacity_policy,
                recording_id=uuid4().hex,
                game_instance_id=location.game_instance_id,
                run_id=location.run_id,
                materials=plan.manifest.materials,
                initial_notes=start.initial_notes,
                seed=initial.restore.canonical_seed,
                template=initial.template,
                game_version=initial.game_version,
                variant=declaration,
                **current_core_identity(),
            )
            journal = resources.enter_context(closing(Journal.create(self.branch, manifest)))
            self.recording_id = journal.manifest.recording_id
            endpoint = _sample_endpoint(self.case.endpoint, location)

            def scene_stopped() -> bool:
                self.operator_stopped = self.operator_stopped or self.stop_requested()
                return self.operator_stopped

            scene = SceneSession(
                loaded,
                journal,
                self.model,
                self.prompts,
                self.documents,
                self.budget,
                endpoint,
                stop_requested=scene_stopped,
            )
            task = asyncio.create_task(scene.run())
            try:
                sample = await self._finish(task)
            finally:
                self.anchor = scene.anchor()
            if isinstance(sample, Completed):
                if self.anchor is None:
                    raise ArchiveError("A completed scene has no recorded model request.")
                return TakeoverCompleted(
                    self.branch, self.anchor, sample, recording_id=self.recording_id
                )
            assert isinstance(sample, Incomplete)
            return TakeoverIncomplete(
                self.branch, self.anchor, sample, recording_id=self.recording_id
            )

    async def _historical(
        self,
        resources: ExitStack,
        plan: AdmissionPlan,
        source: Journal,
        records: tuple[Dependency, ...],
        target: ModelRecord,
    ) -> TakeoverResult:
        released = False
        for message in target.input:
            if isinstance(message, AssistantSnapshot):
                protocol = message.native_reply.protocol
                if protocol is not self.model.configuration.protocol:
                    return TakeoverUnsupported(
                        self.branch,
                        ProtocolMismatch(
                            protocol,
                            self.model.configuration.protocol,
                        ),
                        recording_id=self.recording_id,
                    )
        digest = Journal.archive_sha256(self.case.source.archive)
        parent = ParentPrefix(
            archive=self.case.source.archive.resolve(),
            recording_id=self.case.source.anchor.recording_id,
            before_sequence=self.case.source.anchor.model_sequence,
            archive_sha256=digest,
        )
        declaration = source.manifest.variant
        if isinstance(declaration, SceneVariant):
            raise VariantError("Scene history requires its own verified native case.")
        if declaration is not None:
            if self.variant is not None and replacements(self.variant) != declaration.replacements:
                raise VariantError(
                    "Different variants require sibling branches from the original case."
                )
        elif self.variant is not None:
            declaration = HistoricalVariant(
                case_id=self.case.id,
                case_sha256=sha256(self.case.model_dump_json().encode()).hexdigest(),
                source=parent,
                replacements=replacements(self.variant),
            )
        materials = Materials(declaration.replacements) if declaration is not None else None
        original_before = declaration.source.before_sequence if declaration is not None else None
        if materials is not None:
            await verify_source(parent)
            original_records = records
            if source.manifest.variant is not None:
                assert declaration is not None
                original_source = resources.enter_context(
                    closing(Journal.open(declaration.source.archive))
                )
                original_records = original_source.timeline(stop=original_before)
            materials.preflight(
                tuple(
                    record
                    for record in original_records
                    if isinstance(record, (ModelRecord, MaterialRecord))
                )
            )
        manifest = plan.manifest.model_copy(
            update={
                "schema_version": CURRENT_ARCHIVE_SCHEMA,
                "recording_id": uuid4().hex,
                "parent": parent,
                "variant": declaration,
                **current_core_identity(),
            }
        )
        journal = resources.enter_context(closing(Journal.create(self.branch, manifest)))
        self.recording_id = journal.manifest.recording_id
        endpoint = _sample_endpoint(self.case.endpoint, plan.target.location)
        host = SampleHost(self.budget, endpoint, journal)
        reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
        release = asyncio.Event()
        # Only the original source target still needs material mapping.
        mapping_before = material_boundary(source)
        timeline = Timeline(
            records,
            materials,
            mapping_before,
            variant_journal=journal,
            record_variant=True,
        )
        historical_models: list[ReplayModel] = []

        def factory(role: ModelPurpose, stream: str) -> ModelClient:
            async def continue_live(
                input: ModelInput,
                tools: tuple[ToolDefinition, ...],
                preparation: RequestPreparation | None,
                prepared: PreparedRequest | None,
            ) -> ModelReply:
                await release.wait()
                client = switch.current.model(role, stream)
                if preparation is None:
                    return await client.complete(input, tools)
                live = client.prepare(input, tools)
                # The preparation was measured with the usage of the previous replayed reply,
                # which only the replayed request carries, so the live client must send the
                # same model input and keeps that measurement. The prompt cache key only routes
                # the request and follows the recording or session that sends it.
                if prepared is None or _model_body(live.body) != _model_body(prepared.body):
                    raise ReplayRejected(
                        Rejected(
                            RejectReason.INPUT_MISMATCH,
                            self.case.source.anchor.model_sequence,
                            InputDifference(InputComponent.MODEL_INPUT),
                        )
                    )
                measurement = replace(
                    live.measurement,
                    estimated_input_tokens=prepared.measurement.estimated_input_tokens,
                )
                return await client.complete_prepared(
                    replace(live, measurement=measurement), preparation=preparation
                )

            historical = ReplayModel(
                timeline, role.owner, stream, self.case.source.anchor, reached, continue_live
            )
            historical_models.append(historical)

            class Model:
                @property
                def capacity_policy(self) -> CapacityPolicy:
                    if switch.live:
                        return switch.current.model(role, stream).capacity_policy
                    return historical.capacity_policy

                def prepare(
                    self,
                    input: ModelInput,
                    tools: tuple[ToolDefinition, ...],
                    *,
                    usage_anchor: ModelUsageAnchor | None = None,
                ) -> PreparedRequest:
                    treated_tools = materials.tools(tools) if materials is not None else tools
                    if switch.live:
                        return switch.current.model(role, stream).prepare(
                            input, treated_tools, usage_anchor=usage_anchor
                        )
                    return historical.prepare(input, treated_tools, usage_anchor=usage_anchor)

                async def record_recovery(self, recovery: CapacityRecovery) -> None:
                    if switch.live:
                        await switch.current.model(role, stream).record_recovery(recovery)
                    else:
                        await historical.record_recovery(recovery)

                async def record_handoff(
                    self, reference: ModelAttemptReference, completion: HandoffCompletion
                ) -> None:
                    if switch.live:
                        await switch.current.model(role, stream).record_handoff(
                            reference, completion
                        )
                    else:
                        await historical.record_handoff(reference, completion)

                async def complete_prepared(
                    self,
                    prepared: PreparedRequest,
                    *,
                    preparation: RequestPreparation | None = None,
                ) -> ModelReply:
                    if switch.live:
                        return await switch.current.model(role, stream).complete_prepared(
                            prepared, preparation=preparation
                        )
                    return await historical.complete_prepared(prepared, preparation=preparation)

                async def interrupt_preparation(
                    self,
                    preparation: RequestPreparation,
                    tools: tuple[ToolDefinition, ...],
                    failure: ModelUnavailable,
                ) -> None:
                    treated_tools = materials.tools(tools) if materials is not None else tools
                    if switch.live:
                        await switch.current.model(role, stream).interrupt_preparation(
                            preparation, treated_tools, failure
                        )
                    else:
                        await historical.interrupt_preparation(preparation, treated_tools, failure)

                async def complete(
                    self,
                    input: ModelInput,
                    tools: tuple[ToolDefinition, ...],
                ) -> ModelReply:
                    treated_tools = materials.tools(tools) if materials is not None else tools
                    if switch.live:
                        return await switch.current.model(role, stream).complete(
                            input, treated_tools
                        )
                    return await historical.complete(input, treated_tools)

            return Model()

        switch = _Switch(
            SessionDependencies(
                ReplayGame(timeline),
                ReplayPrompts(timeline),
                ReplayDocuments(timeline),
                factory,
            )
        )

        dependencies = SessionDependencies(
            _GameProxy(switch),
            RejectingPrompts(lambda: switch.current.prompts),
            RejectingDocuments(lambda: switch.current.documents),
            factory,
        )

        def stopped() -> bool:
            self.operator_stopped = self.operator_stopped or self.stop_requested()
            return self.operator_stopped or host.stop_requested()

        async def run() -> SampleResult:
            async with RunSession(plan.manifest, dependencies, diagnostics=None) as session:
                first = records[0]
                if isinstance(first, Record) and first.request == Pause():
                    await session.start()
                elif not isinstance(first, Record) or first.request != Admit():
                    unsupported(first.sequence, UnsupportedCause.START_MODE)
                try:
                    return await host.run(session)
                except SessionStopped:
                    if not switch.live:
                        unsupported(None, UnsupportedCause.UNEXPECTED_END)
                    raise

        task = asyncio.create_task(run())
        try:
            await asyncio.wait((task, reached), return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                await task
                raise RuntimeError("The replay stopped without reaching its target.")
            self.anchor = reached.result()
            if (
                self.anchor.confirmed_operation_cursor != self.case.source.target_cursor
                or self.anchor.semantic_digest != self.case.source.semantic_digest
            ):
                raise ReplayRejected(
                    Rejected(
                        RejectReason.INPUT_MISMATCH,
                        self.case.source.anchor.model_sequence,
                        InputDifference(InputComponent.CURSOR),
                    )
                )
            self.stage = Stage.LOAD
            async with self.restorer.loaded(self.case) as loaded:
                location = loaded.read.observation.location
                assert location.run_id is not None

                def status(value: native.Status, waiting: bool) -> None:
                    journal.diagnostics.native_status(value, waiting)
                    host.native_status(value, waiting)

                game = IdentityGame(
                    LiveGame(
                        loaded.client,
                        namespace=manifest.recording_id,
                        audit=journal,
                        game_instance_id=location.game_instance_id,
                        run_id=location.run_id,
                        settle_confirmation_seconds=SETTLE_CONFIRMATION_SECONDS,
                        on_status=status,
                        stop_requested=stopped,
                    ),
                    TargetIdentity(plan.target.location, location, loaded.refs),
                )

                def live_model(role: ModelPurpose, stream: str) -> ModelClient:
                    return RecordedLlm(
                        self.model.for_purpose(role), journal, role=role.owner, stream=stream
                    )

                live = SessionDependencies(
                    host.game(RecordedGame(game, journal)),
                    RecordedPrompts(
                        VariantPrompts(self.prompts, materials)
                        if materials is not None
                        else self.prompts,
                        journal,
                    ),
                    RecordedDocuments(
                        VariantDocuments(self.documents, materials)
                        if materials is not None
                        else self.documents,
                        journal,
                    ),
                    host.models(live_model),
                )
                host.arm(game.observation(loaded.read.observation))
                control = await loaded.client.control(native.ControlIntent.RUNNING)
                if (
                    not isinstance(control, native.ControlReply)
                    or control.mode is not native.ControlIntent.RUNNING
                ):
                    raise CheckpointUncertain("The live Start intent was not confirmed.")
                switch.current = live
                switch.live = True
                released = True
                release.set()
                sample = await self._finish(task)
                if isinstance(sample, Completed):
                    return TakeoverCompleted(
                        self.branch, self.anchor, sample, recording_id=self.recording_id
                    )
                assert isinstance(sample, Incomplete)
                return TakeoverIncomplete(
                    self.branch, self.anchor, sample, recording_id=self.recording_id
                )
        finally:
            if not released:
                task.cancel()
                with suppress(asyncio.CancelledError, ReplayRejected):
                    await task
            for historical in historical_models:
                await historical.close()
