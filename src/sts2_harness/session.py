"""Own one run's software state using injected logical dependencies."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from sts2_harness.agent import MaterialReader
from sts2_harness.client import DisplayResult
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import DecisionSource
from sts2_harness.deck import CardChoiceCompletion
from sts2_harness.deck_role import DeckAgent, DeckRole
from sts2_harness.deck_stats import DeckStatistics
from sts2_harness.diagnostics import Diagnostics, MechanismTracker, RecoveryExhausted
from sts2_harness.entry_materials import entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import GameIO, GameLifetimeEnded, RunEnded
from sts2_harness.journal import Manifest
from sts2_harness.knowledge import Documents, DocumentView, MaterialRole
from sts2_harness.llm.messages import ModelClient
from sts2_harness.loop import CombatFactory, DefaultFlow, RunFlow
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.note_values import NotesView
from sts2_harness.notes import Notes
from sts2_harness.prompts import PromptReader
from sts2_harness.results import ActionToolResult, QueryToolResult
from sts2_harness.route_plan import RoutePlan, reconcile_plan
from sts2_harness.run import RunAgent, RunRole
from sts2_harness.tools import ToolCatalog
from sts2_harness.user_input_values import UserInputSource

type RunFactory = Callable[[Execution, Notes, ToolRecoveryBudget], RunRole]
type RoleFactory = Callable[
    [Execution, ToolRecoveryBudget, native.Observation, NotesView], DecisionSource
]
type DeckFactory = Callable[
    [Execution, ToolRecoveryBudget, native.Observation, native.Offer | None], DeckRole
]
type FlowFactory = Callable[[Execution, ToolRecoveryBudget, Notes, RunRole, CombatFactory], RunFlow]


type ModelFactory = Callable[[ModelPurpose, str], ModelClient]


@dataclass(frozen=True, slots=True)
class SessionDependencies:
    game: GameIO
    prompts: PromptReader
    documents: Documents
    model: ModelFactory
    card_completion: Callable[[CardChoiceCompletion], None] | None = None
    display: Callable[[str], Awaitable[DisplayResult]] | None = None


class RunSession:
    def __init__(
        self,
        manifest: Manifest,
        dependencies: SessionDependencies,
        *,
        run_factory: RunFactory | None = None,
        combat_factory: RoleFactory | None = None,
        deck_factory: DeckFactory | None = None,
        flow_factory: FlowFactory | None = None,
        diagnostics: Diagnostics | None = None,
        user_input: UserInputSource | None = None,
    ) -> None:
        if manifest.initial_notes is None:
            raise ValueError("A run session requires explicit initial notes in its manifest.")
        if manifest.run_context_policy != "act_local_v1":
            raise ValueError(
                f"Unsupported run context policy for this core: {manifest.run_context_policy}."
            )
        self.notes = Notes(manifest.initial_notes)
        self.deck_stats = DeckStatistics()
        self.recovery = ToolRecoveryBudget(on_exhausted=self._recovery_exhausted)
        self._dependencies = dependencies
        self._manifest = manifest
        self._run_factory = run_factory
        self._combat_factory = combat_factory
        self._deck_factory = deck_factory
        self._flow_factory = flow_factory
        self._diagnostics = diagnostics
        self._user_input = user_input
        self._run_stream = 0
        self._combat_stream = 0
        self._deck_stream = 0
        self._document_order: dict[str, int] = {}
        self._card_reports: list[tuple[AutomaticProgress, ActionToolResult]] = []
        self._current_run: RunRole | None = None
        self._flow: RunFlow | None = None
        self._mechanism_tracker: MechanismTracker | None = (
            MechanismTracker() if diagnostics is not None else None
        )

    def _recovery_exhausted(self, failures: int) -> None:
        if self._diagnostics is not None:
            self._diagnostics.emit(RecoveryExhausted(failures=failures))

    async def __aenter__(self) -> Self:
        self.execution = Execution(
            self._dependencies.game,
            game_instance_id=self._manifest.game_instance_id,
            run_id=self._manifest.run_id,
            recovery=self.recovery,
            diagnostics=self._diagnostics,
            display=self._dependencies.display,
        )
        run = self._new_run()
        self._flow = (
            self._flow_factory(self.execution, self.recovery, self.notes, run, self._combat)
            if self._flow_factory is not None
            else DefaultFlow(
                self.execution,
                self.recovery,
                self.notes,
                run,
                self._combat,
                new_run=self._new_run,
                diagnostics=self._diagnostics,
                minimal=self._manifest.minimal,
                deck=self._deck,
            )
        )
        return self

    def _new_run(self) -> RunRole:
        run = (
            self._run_factory(self.execution, self.notes, self.recovery)
            if self._run_factory is not None
            else RunAgent(
                self._run_client,
                self._tools(native.DecisionOwner.RUN),
                self.recovery,
                prompts=self._dependencies.prompts,
                materials=self._materials(MaterialRole.RUN),
                diagnostics=self._diagnostics,
                user_input=self._user_input,
                card_reports=self._card_reports,
                mechanism_tracker=self._mechanism_tracker,
            )
        )
        self._current_run = run
        return run

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def start(self) -> None:
        if self._flow is None:
            raise RuntimeError("Enter the run session before starting it.")
        await self.execution.pause_until_resumed(None)

    async def run(self) -> RunEnded:
        if self._flow is None:
            raise RuntimeError("Enter the run session before running its flow.")
        try:
            return await self._flow.run()
        finally:
            if self._diagnostics is not None and self._mechanism_tracker is not None:
                summary = self._mechanism_tracker.summary()
                if summary is not None:
                    self._diagnostics.emit(summary)

    def _tools(self, role: native.DecisionOwner, *, focused_deck: bool = False) -> ToolCatalog:
        return ToolCatalog(
            role,
            observe=self.execution.observe,
            inspect=self.execution.inspect,
            catalog=self.execution.catalog,
            present=self.execution.present if role is native.DecisionOwner.RUN else None,
            display=self.execution.display_reason if role is native.DecisionOwner.RUN else None,
            notes=self.notes if role is native.DecisionOwner.RUN else None,
            notes_write_guard=self._notes_write_guard,
            skills=self._dependencies.documents,
            supplements=self._dependencies.documents,
            minimal=self._manifest.minimal,
            deck_stats=self.deck_stats,
            focused_deck=focused_deck,
            fights=self.execution.fights if role is native.DecisionOwner.RUN else None,
            diagnostics=self._diagnostics,
        )

    async def _notes_write_guard(
        self, observation: native.Observation | None
    ) -> QueryToolResult | None:
        if observation is not None and self.execution.run_ended(observation) is not None:
            raise GameLifetimeEnded(observation.location)
        return None

    def _run_client(self) -> ModelClient:
        self._run_stream += 1
        return self._dependencies.model(ModelPurpose.RUN, f"run:{self._run_stream}")

    def _deck(self, opening: native.Observation, offer: native.Offer | None) -> DeckRole:
        if self._deck_factory is not None:
            return self._deck_factory(self.execution, self.recovery, opening, offer)
        self._deck_stream += 1
        stream = f"deck:{self._deck_stream}"
        return DeckAgent(
            lambda: self._dependencies.model(ModelPurpose.DECK, stream),
            self._tools(native.DecisionOwner.RUN, focused_deck=True),
            self.recovery,
            stream=stream,
            prompts=self._dependencies.prompts,
            materials=self._materials(MaterialRole.DECK),
            completion=self._dependencies.card_completion,
            reward_preview=self.execution,
            diagnostics=self._diagnostics,
            user_input=self._user_input,
            mechanism_tracker=self._mechanism_tracker,
        )

    def _materials(
        self, role: MaterialRole, opening: native.Observation | None = None
    ) -> MaterialReader | None:
        if not self._manifest.materials.entry_documents_enabled:
            return None

        async def read(observation: native.Observation) -> tuple[DocumentView, ...]:
            if (
                role is MaterialRole.COMBAT
                and opening is not None
                and (
                    not isinstance(observation.state, native.CombatState)
                    or observation.location.node_id != opening.location.node_id
                )
            ):
                observation = opening
            configured = self._manifest.materials.battle_guidance
            if role in (MaterialRole.RUN, MaterialRole.DECK):
                configured = (
                    self._manifest.materials.act_guidance.get(observation.state.progress.act, ())
                    if observation.state.progress.act is not None
                    else ()
                )
            active_plan = (
                reconcile_plan(
                    self._current_run.route_plan, observation.state.progress.position
                ).plan
                if role in (MaterialRole.RUN, MaterialRole.DECK) and self._current_run is not None
                else None
            )
            return await entry_materials(
                self.execution,
                self._dependencies.documents,
                role,
                observation,
                configured,
                document_order=self._document_order,
                route_plan=active_plan,
            )

        return read

    def _combat(
        self, opening: native.Observation, notes: NotesView, route_plan: RoutePlan | None
    ) -> DecisionSource:
        if self._combat_factory is not None:
            return self._combat_factory(self.execution, self.recovery, opening, notes)
        self._combat_stream += 1
        return CombatAgent(
            self._dependencies.model(ModelPurpose.COMBAT, f"combat:{self._combat_stream}"),
            self._tools(native.DecisionOwner.COMBAT),
            self.recovery,
            opening_notes=notes,
            route_plan=route_plan,
            prompts=self._dependencies.prompts,
            materials=self._materials(MaterialRole.COMBAT, opening),
            diagnostics=self._diagnostics,
            user_input=self._user_input,
            mechanism_tracker=self._mechanism_tracker,
        )
