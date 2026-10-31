"""Record one constructed scenario through the ordinary gameplay session."""

import asyncio
from collections.abc import Callable
from hashlib import file_digest
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import uuid4

from pydantic import Field

from sts2_harness.capacity import CapacityPolicy
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import ControlResult
from sts2_harness.game import GameReadError, LiveGame, SessionStopped
from sts2_harness.journal import ArchiveError, Journal, Manifest, RecordedGame, SessionMaterials
from sts2_harness.knowledge import Documents
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    ModelClient,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    ToolDefinition,
)
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PromptReader
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval.cases import AfterDecisionsSpec, TargetBattle
from sts2_harness_eval.laboratory import (
    LaboratoryClient,
    LaboratoryOutcome,
    LaboratoryRecipe,
    LaboratoryReply,
)
from sts2_harness_eval.sample import (
    SETTLE_CONFIRMATION_SECONDS,
    AfterDecisions,
    BattleEnd,
    Completed,
    Incomplete,
    OperatorStopped,
    SampleBudget,
    SampleHost,
    StopCause,
    finish_after_stop,
)


class LaboratorySource(native.WireModel):
    recipe: LaboratoryRecipe
    construction: LaboratoryReply
    start: ControlResult | None
    opening: native.Observation | None
    initial_notes: str
    materials: SessionMaterials
    budget: SampleBudget
    endpoint: AfterDecisionsSpec | TargetBattle


class ArchiveRef(native.WireModel):
    path: Path
    recording_id: native.Nonempty
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class LaboratoryResult(native.WireModel):
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    archive: ArchiveRef | None
    sample: Completed | Incomplete | OperatorStopped | None
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def _result(
    directory: Path,
    archive: ArchiveRef | None,
    sample: Completed | Incomplete | OperatorStopped | None,
    started: float,
) -> LaboratoryResult:
    result = LaboratoryResult(
        source_sha256=_digest(directory / "source.json"),
        archive=archive,
        sample=sample,
        elapsed_seconds=monotonic() - started,
    )
    (directory / "result.json").write_text(result.model_dump_json(), encoding="utf-8")
    return result


def _combat(observation: native.Observation | None) -> native.Observation:
    if (
        observation is None
        or not isinstance(observation.state, native.CombatState)
        or observation.location.owner is not native.DecisionOwner.COMBAT
        or observation.input_readiness
        not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
    ):
        raise GameReadError("The constructed scenario is not ready for Combat input.")
    return observation


class _Sample:
    def __init__(self, host: SampleHost, stop_requested: Callable[[], bool]) -> None:
        self.host = host
        self._stop_requested = stop_requested
        self.operator_stopped = False
        self._first_request = True

    def stopped(self) -> bool:
        self.operator_stopped = self.operator_stopped or self._stop_requested()
        return self.operator_stopped or self.host.stop_requested()

    def models(
        self, model: Llm, journal: Journal
    ) -> Callable[[native.DecisionOwner, str], ModelClient]:
        sample = self

        def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
            recorded = RecordedLlm(model, journal, role=role, stream=stream)

            class Model:
                @property
                def capacity_policy(self) -> CapacityPolicy:
                    return recorded.capacity_policy

                def prepare(
                    self, input: ModelInput, tools: tuple[ToolDefinition, ...]
                ) -> PreparedRequest:
                    return recorded.prepare(input, tools)

                def _check_first(self) -> None:
                    if sample._first_request:
                        if role is not native.DecisionOwner.COMBAT:
                            raise ArchiveError("A laboratory sample must begin with Combat.")
                        sample._first_request = False

                async def complete_prepared(
                    self,
                    prepared: PreparedRequest,
                    *,
                    preparation: RequestPreparation | None = None,
                ) -> ModelReply:
                    self._check_first()
                    return await recorded.complete_prepared(prepared, preparation=preparation)

                async def interrupt_preparation(
                    self,
                    preparation: RequestPreparation,
                    tools: tuple[ToolDefinition, ...],
                    failure: ModelUnavailable,
                ) -> None:
                    self._check_first()
                    await recorded.interrupt_preparation(preparation, tools, failure)

                async def complete(
                    self, input: ModelInput, tools: tuple[ToolDefinition, ...]
                ) -> ModelReply:
                    self._check_first()
                    return await recorded.complete(input, tools)

            return Model()

        return self.host.models(factory)

    async def run(
        self, manifest: Manifest, dependencies: SessionDependencies
    ) -> Completed | Incomplete | OperatorStopped:
        try:
            async with RunSession(manifest, dependencies, diagnostics=None) as session:
                result = await self.host.run(session)
                assert isinstance(result, (Completed, Incomplete))
                return result
        except SessionStopped:
            if not self.operator_stopped:
                raise
            return OperatorStopped()

    def stop(self) -> None:
        self.operator_stopped = True


async def run_laboratory_sample(
    client: ModClient,
    laboratory: LaboratoryClient,
    recipe: LaboratoryRecipe,
    directory: Path,
    model: Llm,
    prompts: PromptReader,
    documents: Documents,
    materials: SessionMaterials,
    initial_notes: str,
    budget: SampleBudget,
    endpoint: AfterDecisionsSpec | TargetBattle,
    *,
    stop_requested: Callable[[], bool],
) -> LaboratoryResult:
    if budget.requests <= 0 or budget.seconds <= 0 or not isfinite(budget.seconds):
        raise ValueError("A laboratory sample requires a positive finite budget.")
    started = monotonic()
    directory.mkdir(parents=True)
    construction = await laboratory.construct(recipe)
    source = LaboratorySource(
        recipe=recipe,
        construction=construction,
        start=None,
        opening=None,
        initial_notes=initial_notes,
        materials=materials,
        budget=budget,
        endpoint=endpoint,
    )
    source_path = directory / "source.json"
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    archive: ArchiveRef | None = None
    outcome: Completed | Incomplete | OperatorStopped | None = None
    if construction.status is LaboratoryOutcome.COMPLETED:
        prepared = construction.prepared_observation
        current = _combat(construction.observation)
        location = current.location
        assert location.run_id is not None and location.node_id is not None
        if (
            prepared is None
            or prepared.location.game_instance_id != location.game_instance_id
            or prepared.location.run_id != location.run_id
        ):
            raise GameReadError("The scenario observations belong to different runs.")
        start = await client.control(native.ControlIntent.RUNNING)
        source = source.model_copy(update={"start": start})
        source_path.write_text(source.model_dump_json(), encoding="utf-8")
        if (
            not isinstance(start, native.ControlReply)
            or start.mode is not native.ControlIntent.RUNNING
        ):
            raise GameReadError("The laboratory Start intent was not confirmed.")
        refreshed = await client.state()
        if not isinstance(refreshed, native.StateCompleted):
            raise GameReadError("The laboratory opening could not be read.")
        source = source.model_copy(update={"opening": refreshed.observation})
        source_path.write_text(source.model_dump_json(), encoding="utf-8")
        opening = _combat(refreshed.observation)
        if opening.location != location:
            raise GameReadError("The laboratory opening moved to a different Combat.")
        manifest = Manifest(
            capacity_policy=CapacityPolicy(context_window=model.configuration.context_window),
            recording_id=uuid4().hex,
            core_version="0.1.0",
            game_instance_id=location.game_instance_id,
            run_id=location.run_id,
            initial_notes=initial_notes,
            materials=materials,
        )
        path = (directory / "journal.sqlite").resolve()
        journal = Journal.create(path, manifest)
        caller = asyncio.current_task()
        assert caller is not None
        initial_cancellations = caller.cancelling()
        sealed = False
        try:
            try:
                target = (
                    AfterDecisions(endpoint.count)
                    if isinstance(endpoint, AfterDecisionsSpec)
                    else BattleEnd(location.node_id)
                )
                host = SampleHost(budget, target, journal)
                sample = _Sample(host, stop_requested)

                def status(value: native.Status, waiting: bool) -> None:
                    journal.diagnostics.native_status(value, waiting)
                    host.native_status(value, waiting)

                game = LiveGame(
                    client,
                    namespace=manifest.recording_id,
                    audit=journal,
                    game_instance_id=location.game_instance_id,
                    run_id=location.run_id,
                    settle_confirmation_seconds=SETTLE_CONFIRMATION_SECONDS,
                    on_status=status,
                    stop_requested=sample.stopped,
                )
                dependencies = SessionDependencies(
                    host.game(RecordedGame(game, journal)),
                    RecordedPrompts(prompts, journal),
                    RecordedDocuments(documents, journal),
                    sample.models(model, journal),
                )
                host.arm(opening)
                task = asyncio.create_task(sample.run(manifest, dependencies))
                outcome = await finish_after_stop(task, sample.stop)
            finally:
                journal.close()
                sealed = True
        except ArchiveError:
            if isinstance(outcome, (Completed, Incomplete)):
                outcome = Incomplete(
                    StopCause.RECORDING_UNAVAILABLE, outcome.decisions, outcome.requests
                )
            elif (
                isinstance(outcome, OperatorStopped) or caller.cancelling() > initial_cancellations
            ):
                outcome = OperatorStopped()
            else:
                raise
        except asyncio.CancelledError:
            outcome = OperatorStopped()
            raise
        finally:
            try:
                if caller.cancelling() > initial_cancellations:
                    outcome = OperatorStopped()
                if sealed:
                    archive = ArchiveRef(
                        path=path,
                        recording_id=manifest.recording_id,
                        sha256=Journal.archive_sha256(path),
                    )
                result = _result(directory, archive, outcome, started)
            finally:
                if caller.cancelling() > initial_cancellations:
                    raise asyncio.CancelledError
        return result
    return _result(directory, archive, outcome, started)
