"""Assemble a fresh cognition session around one verified native target."""

from collections.abc import Callable

from sts2_harness.capacity import CapacityPolicy, ModelAttemptReference
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame
from sts2_harness.journal import ArchiveError, Journal, RecordedGame, SceneVariant
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
from sts2_harness.prompts import PromptReader
from sts2_harness.results import HandoffCompletion
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval._material_boundary import RejectingDocuments, RejectingPrompts
from sts2_harness_eval._replay import prefix
from sts2_harness_eval.native import LoadedTarget
from sts2_harness_eval.results import ModelAnchor, Reached
from sts2_harness_eval.sample import (
    SETTLE_CONFIRMATION_SECONDS,
    Endpoint,
    SampleBudget,
    SampleHost,
    SampleResult,
)
from sts2_harness_eval.variants import Materials, VariantDocuments, VariantPrompts


class SceneSession:
    def __init__(
        self,
        loaded: LoadedTarget,
        journal: Journal,
        model: Llm,
        prompts: PromptReader,
        documents: Documents,
        budget: SampleBudget,
        endpoint: Endpoint,
        *,
        stop_requested: Callable[[], bool],
    ) -> None:
        self._journal = journal
        self._host = SampleHost(budget, endpoint, journal)
        self._stop_requested = stop_requested
        declaration = journal.manifest.variant
        assert isinstance(declaration, SceneVariant)
        materials = Materials(declaration.replacements)
        location = loaded.read.observation.location
        assert location.run_id is not None
        game = LiveGame(
            loaded.client,
            namespace=journal.manifest.recording_id,
            audit=journal,
            game_instance_id=location.game_instance_id,
            run_id=location.run_id,
            settle_confirmation_seconds=SETTLE_CONFIRMATION_SECONDS,
            on_status=self._status,
            stop_requested=self._stopped,
        )

        def factory(role: ModelPurpose, stream: str) -> ModelClient:
            recorded = RecordedLlm(model.for_purpose(role), journal, role=role.owner, stream=stream)

            class Model:
                @property
                def capacity_policy(self) -> CapacityPolicy:
                    return recorded.capacity_policy

                def prepare(
                    self,
                    input: ModelInput,
                    tools: tuple[ToolDefinition, ...],
                    *,
                    usage_anchor: ModelUsageAnchor | None = None,
                ) -> PreparedRequest:
                    return recorded.prepare(
                        input, materials.tools(tools), usage_anchor=usage_anchor
                    )

                async def record_recovery(self, recovery: CapacityRecovery) -> None:
                    await recorded.record_recovery(recovery)

                async def record_handoff(
                    self, reference: ModelAttemptReference, completion: HandoffCompletion
                ) -> None:
                    await recorded.record_handoff(reference, completion)

                async def complete_prepared(
                    self,
                    prepared: PreparedRequest,
                    *,
                    preparation: RequestPreparation | None = None,
                ) -> ModelReply:
                    return await recorded.complete_prepared(prepared, preparation=preparation)

                async def interrupt_preparation(
                    self,
                    preparation: RequestPreparation,
                    tools: tuple[ToolDefinition, ...],
                    failure: ModelUnavailable,
                ) -> None:
                    await recorded.interrupt_preparation(
                        preparation, materials.tools(tools), failure
                    )

                async def complete(
                    self, input: ModelInput, tools: tuple[ToolDefinition, ...]
                ) -> ModelReply:
                    return await recorded.complete(input, materials.tools(tools))

            return Model()

        recorded_prompts = RecordedPrompts(VariantPrompts(prompts, materials), journal)
        recorded_documents = RecordedDocuments(VariantDocuments(documents, materials), journal)
        self._dependencies = SessionDependencies(
            self._host.game(RecordedGame(game, journal)),
            RejectingPrompts(lambda: recorded_prompts),
            RejectingDocuments(lambda: recorded_documents),
            self._host.models(factory),
        )
        self._host.arm(loaded.read.observation)

    def _status(self, value: native.Status, waiting: bool) -> None:
        self._journal.diagnostics.native_status(value, waiting)
        self._host.native_status(value, waiting)

    def _stopped(self) -> bool:
        return self._stop_requested() or self._host.stop_requested()

    async def run(self) -> SampleResult:
        async with RunSession(
            self._journal.manifest, self._dependencies, diagnostics=None
        ) as session:
            return await self._host.run(session)

    def anchor(self) -> Reached | None:
        requests = self._journal.requests()
        if not requests:
            return None
        first = requests[0]
        if (
            first.role is not native.DecisionOwner.COMBAT
            or first.stream != "combat:1"
            or first.position != 1
        ):
            raise ArchiveError("The scene did not begin with a fresh Combat request.")
        anchor = ModelAnchor(self._journal.manifest.recording_id, first.sequence)
        prefix(self._journal, anchor)
        return Reached(
            anchor,
            first.confirmed_operation_cursor,
            first.semantic_digest,
        )
