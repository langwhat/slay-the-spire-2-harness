"""Resume one verified historical request through its original run coroutine."""

import asyncio
from collections.abc import Callable
from contextlib import ExitStack, closing, suppress
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from uuid import uuid4

import httpx2 as httpx
from pydantic import ValidationError

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.game import (
    Admit,
    Admitted,
    Cataloged,
    ControlWriteError,
    GameLifetimeEnded,
    GameReadError,
    GameReply,
    Inspected,
    LiveGame,
    Observed,
    Pause,
    Resumed,
    SessionStopped,
    Settlement,
)
from sts2_harness.headless import HeadlessError
from sts2_harness.journal import ArchiveError, Journal, ParentPrefix, Record, RecordedGame
from sts2_harness.journal_models import AssistantSnapshot
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    SkillRead,
    SkillSummary,
    SubjectId,
)
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient, ModelInput, ModelReply, ToolDefinition
from sts2_harness.material_text import MaterialText
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.native_template import TemplateError
from sts2_harness.prompts import PromptReader
from sts2_harness.results import Reason
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval._replay import (
    ReplayDocuments,
    ReplayGame,
    ReplayModel,
    ReplayPrompts,
    ReplayRejected,
    Timeline,
    prefix,
    unsupported,
)
from sts2_harness_eval.cases import (
    AdmissionFailure,
    AdmissionRequest,
    AfterDecisionsSpec,
    Case,
    IncompleteCause,
    Mismatch,
    Stage,
    plan_admission,
)
from sts2_harness_eval.cases import (
    Unsupported as AdmissionUnsupported,
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
    SampleBudget,
    SampleHost,
    SampleResult,
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


@dataclass(frozen=True, slots=True)
class OperatorStopped:
    """The caller's explicit stop reached a complete execution boundary."""


type Refusal = Rejected | AdmissionUnsupported | Mismatch | CheckpointCause | ProtocolMismatch


@dataclass(frozen=True, slots=True)
class TakeoverCompleted:
    branch: Path
    anchor: Reached
    sample: Completed


@dataclass(frozen=True, slots=True)
class TakeoverIncomplete:
    branch: Path
    anchor: Reached | None
    evidence: Incomplete | NativeUncertain | OperatorStopped


@dataclass(frozen=True, slots=True)
class TakeoverUnsupported:
    branch: Path
    evidence: Refusal


type TakeoverResult = TakeoverCompleted | TakeoverIncomplete | TakeoverUnsupported


class _Switch:
    def __init__(self, replay: SessionDependencies) -> None:
        self.current = replay
        self.live = False


class _GameProxy:
    def __init__(self, switch: _Switch) -> None:
        self._switch = switch

    async def admit(self) -> GameReply[Admitted]:
        return await self._switch.current.game.admit()

    async def observe(self) -> GameReply[Observed]:
        return await self._switch.current.game.observe()

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        return await self._switch.current.game.inspect(query)

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return await self._switch.current.game.catalog(request)

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        return await self._switch.current.game.settle(route, step)

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        return await self._switch.current.game.pause_until_resumed(reason)


class _PromptProxy:
    def __init__(self, switch: _Switch) -> None:
        self._switch = switch

    def read(self, name: str) -> MaterialText:
        return self._switch.current.prompts.read(name)


class _DocumentProxy:
    def __init__(self, switch: _Switch) -> None:
        self._switch = switch

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        return await self._switch.current.documents.list(role)

    async def read(self, request: SkillRead) -> DocumentView:
        return await self._switch.current.documents.read(request)

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        return await self._switch.current.documents.lookup(subjects)


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
) -> TakeoverResult:
    """Verify both prefixes, then release the held request into recorded live dependencies."""
    anchor: Reached | None = None
    stage = Stage.PREPARATION
    task: asyncio.Task[SampleResult] | None = None
    released = False
    operator_stopped = False
    try:
        with ExitStack() as resources:
            plan = plan_admission(
                AdmissionRequest(
                    id=case.id,
                    archive=case.source.archive,
                    anchor=case.source.anchor,
                    endpoint=case.endpoint,
                ),
                restorer.store.directory,
            )
            if plan.source != case.source:
                raise ReplayRejected(
                    Rejected(
                        RejectReason.INPUT_MISMATCH,
                        case.source.anchor.model_sequence,
                        InputDifference(InputComponent.REQUEST),
                    )
                )
            source = resources.enter_context(closing(Journal.open(case.source.archive)))
            records = prefix(source, case.source.anchor)
            target = source.request(case.source.anchor.model_sequence)
            for message in target.input:
                if isinstance(message, AssistantSnapshot):
                    protocol = message.native_reply.protocol
                    if protocol is not model.configuration.protocol:
                        return TakeoverUnsupported(
                            branch,
                            ProtocolMismatch(
                                protocol,
                                model.configuration.protocol,
                            ),
                        )
            with case.source.archive.open("rb") as original:
                digest = file_digest(original, "sha256").hexdigest()
            manifest = plan.manifest.model_copy(
                update={
                    "recording_id": uuid4().hex,
                    "parent": ParentPrefix(
                        archive=case.source.archive.resolve(),
                        recording_id=case.source.anchor.recording_id,
                        before_sequence=case.source.anchor.model_sequence,
                        archive_sha256=digest,
                    ),
                }
            )
            journal = resources.enter_context(closing(Journal.create(branch, manifest)))
            endpoint: Endpoint
            if isinstance(case.endpoint, AfterDecisionsSpec):
                endpoint = AfterDecisions(case.endpoint.count)
            else:
                node = plan.target.location.node_id
                assert node is not None
                endpoint = BattleEnd(node)
            host = SampleHost(budget, endpoint, journal)
            reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
            release = asyncio.Event()
            timeline = Timeline(records)

            def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
                async def continue_live(
                    input: ModelInput,
                    tools: tuple[ToolDefinition, ...],
                ) -> ModelReply:
                    await release.wait()
                    return await switch.current.model(role, stream).complete(input, tools)

                historical = ReplayModel(
                    timeline, role, stream, case.source.anchor, reached, continue_live
                )

                class Model:
                    async def complete(
                        self,
                        input: ModelInput,
                        tools: tuple[ToolDefinition, ...],
                    ) -> ModelReply:
                        if switch.live:
                            return await switch.current.model(role, stream).complete(input, tools)
                        return await historical.complete(input, tools)

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
                _PromptProxy(switch),
                _DocumentProxy(switch),
                factory,
            )

            def stopped() -> bool:
                nonlocal operator_stopped
                operator_stopped = operator_stopped or stop_requested()
                return operator_stopped or host.stop_requested()

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
                anchor = reached.result()
                if (
                    anchor.confirmed_operation_cursor != case.source.target_cursor
                    or anchor.semantic_digest != case.source.semantic_digest
                ):
                    raise ReplayRejected(
                        Rejected(
                            RejectReason.INPUT_MISMATCH,
                            case.source.anchor.model_sequence,
                            InputDifference(InputComponent.CURSOR),
                        )
                    )
                stage = Stage.LOAD
                async with restorer.loaded(case) as loaded:
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

                    def live_model(role: native.DecisionOwner, stream: str) -> ModelClient:
                        return RecordedLlm(model, journal, role=role, stream=stream)

                    live = SessionDependencies(
                        host.game(RecordedGame(game, journal)),
                        RecordedPrompts(prompts, journal),
                        RecordedDocuments(documents, journal),
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
                    try:
                        sample = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        operator_stopped = True
                        with suppress(SessionStopped):
                            while not task.done():
                                try:
                                    await asyncio.shield(task)
                                except asyncio.CancelledError:
                                    continue
                            task.result()
                        raise
                    if isinstance(sample, Completed):
                        return TakeoverCompleted(branch, anchor, sample)
                    assert isinstance(sample, Incomplete)
                    return TakeoverIncomplete(branch, anchor, sample)
            finally:
                if not released:
                    task.cancel()
                    with suppress(asyncio.CancelledError, ReplayRejected):
                        await task
    except SessionStopped:
        if not operator_stopped:
            raise
        return TakeoverIncomplete(branch, anchor, OperatorStopped())
    except ReplayRejected as error:
        return TakeoverUnsupported(branch, error.result)
    except AdmissionFailure as error:
        return TakeoverUnsupported(branch, error.result)
    except VerificationFailure as error:
        return TakeoverUnsupported(
            branch,
            Mismatch(
                relation=error.relation,
                sequence=error.sequence,
                field_path=error.field_path,
            ),
        )
    except CheckpointRejected as error:
        return TakeoverUnsupported(branch, error.cause)
    except (
        CheckpointUncertain,
        ClientUnavailable,
        GameReadError,
        ControlWriteError,
        GameLifetimeEnded,
    ) as error:
        return TakeoverIncomplete(
            branch,
            anchor,
            NativeUncertain(
                stage,
                IncompleteCause.NATIVE_UNCERTAIN,
                str(error),
            ),
        )
    except (OSError, ArchiveError, httpx.HTTPError, TimeoutError) as error:
        return TakeoverIncomplete(
            branch, anchor, NativeUncertain(stage, IncompleteCause.IO, str(error))
        )
    except ValidationError as error:
        return TakeoverIncomplete(
            branch, anchor, NativeUncertain(stage, IncompleteCause.PROTOCOL, str(error))
        )
    except (HeadlessError, TemplateError) as error:
        return TakeoverIncomplete(
            branch, anchor, NativeUncertain(stage, IncompleteCause.STARTUP, str(error))
        )
