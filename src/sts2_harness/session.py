"""Compose and release the actual resources for one built-in run."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from types import TracebackType
from typing import Self

from sts2_harness.agent import MaterialReader
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent
from sts2_harness.decisions import DecisionSource
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.entry_materials import entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame, RunEnded
from sts2_harness.journal import Journal, Manifest, RecordedGame
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.loop import CombatFactory, DefaultFlow, RunFlow
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.notes import Notes
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.run import RunAgent, RunRole
from sts2_harness.tools import ToolCatalog

type RunFactory = Callable[[Execution, Notes, ToolRecoveryBudget], RunRole]
type RoleFactory = Callable[
    [Execution, ToolRecoveryBudget, native.Observation, str], DecisionSource
]
type FlowFactory = Callable[[Execution, ToolRecoveryBudget, Notes, RunRole, CombatFactory], RunFlow]


class RunSession:
    def __init__(
        self,
        client: ModClient,
        manifest: Manifest,
        archive_path: Path,
        *,
        model: Llm | None = None,
        documents: FileDocuments | None = None,
        act_guidance: Mapping[int, tuple[str, ...]] | None = None,
        battle_guidance: tuple[str, ...] = (),
        run_factory: RunFactory | None = None,
        combat_factory: RoleFactory | None = None,
        flow_factory: FlowFactory = DefaultFlow,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
        diagnostics: Diagnostics | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        if manifest.initial_notes is None:
            raise ValueError("A run session requires explicit initial notes in its manifest.")
        if model is None and (run_factory is None or combat_factory is None):
            raise ValueError("Default model roles require a model resource.")
        if documents is None and (act_guidance or battle_guidance):
            raise ValueError("Configured guidance requires a document provider.")
        self.notes = Notes(manifest.initial_notes)
        self.recovery = ToolRecoveryBudget()
        self._client = client
        self._model = model
        self._manifest = manifest
        self._archive_path = archive_path
        self._documents = documents
        self._act_guidance = act_guidance
        self._battle_guidance = battle_guidance
        self._run_factory = run_factory
        self._combat_factory = combat_factory
        self._flow_factory = flow_factory
        self._wait = wait
        self._diagnostics = diagnostics
        self._stop_requested = stop_requested
        self._resources = AsyncExitStack()
        self._run_stream = 0
        self._combat_stream = 0
        self._flow: RunFlow | None = None

    async def __aenter__(self) -> Self:
        try:
            await self._resources.enter_async_context(self._client)
            if self._model is not None:
                await self._resources.enter_async_context(self._model)
            self.journal = Journal.create(
                self._archive_path, self._manifest, diagnostics=self._diagnostics
            )
            self._resources.callback(self.journal.close)
            self.execution = Execution(
                RecordedGame(
                    LiveGame(
                        self._client,
                        namespace=self._manifest.recording_id,
                        game_instance_id=self._manifest.game_instance_id,
                        run_id=self._manifest.run_id,
                        wait_for_start=True,
                        audit=self.journal,
                        wait=self._wait,
                        stop_requested=self._stop_requested,
                        on_status=self._diagnostics.native_status
                        if self._diagnostics is not None
                        else None,
                    ),
                    self.journal,
                ),
                game_instance_id=self._manifest.game_instance_id,
                run_id=self._manifest.run_id,
                recovery=self.recovery,
            )
            self._recorded_documents = RecordedDocuments(
                self._documents if self._documents is not None else FileDocuments(), self.journal
            )
            self._prompts = RecordedPrompts(PackagedPrompts(), self.journal)
            run = (
                self._run_factory(self.execution, self.notes, self.recovery)
                if self._run_factory is not None
                else RunAgent(
                    self._run_client,
                    self._tools(native.DecisionOwner.RUN),
                    self.recovery,
                    prompts=self._prompts,
                    materials=self._materials(native.DecisionOwner.RUN),
                )
            )
            self._flow = self._flow_factory(
                self.execution, self.recovery, self.notes, run, self._combat
            )
            return self
        except BaseException:
            await self._resources.aclose()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._resources.aclose()

    async def start(self) -> None:
        if self._flow is None:
            raise RuntimeError("Enter the run session before starting it.")
        await self.execution.pause_until_resumed(None)

    async def run(self) -> RunEnded:
        if self._flow is None:
            raise RuntimeError("Enter the run session before running its flow.")
        return await self._flow.run()

    def _tools(self, role: native.DecisionOwner) -> ToolCatalog:
        return ToolCatalog(
            role,
            observe=self.execution.observe,
            inspect=self.execution.inspect,
            catalog=self.execution.catalog,
            notes=self.notes if role is native.DecisionOwner.RUN else None,
            skills=self._recorded_documents,
            supplements=self._recorded_documents,
        )

    def _run_client(self) -> ModelClient:
        assert self._model is not None
        self._run_stream += 1
        return RecordedLlm(
            self._model,
            self.journal,
            role=native.DecisionOwner.RUN,
            stream=f"run:{self._run_stream}",
        )

    def _materials(
        self, role: native.DecisionOwner, opening: native.Observation | None = None
    ) -> MaterialReader | None:
        if self._documents is None:
            return None

        async def read(observation: native.Observation) -> tuple[DocumentView, ...]:
            if opening is not None and (
                not isinstance(observation.state, native.CombatState)
                or observation.location.node_id != opening.location.node_id
            ):
                observation = opening
            configured = self._battle_guidance
            if role is native.DecisionOwner.RUN:
                configured = (
                    self._act_guidance.get(observation.state.progress.act, ())
                    if self._act_guidance is not None and observation.state.progress.act is not None
                    else ()
                )
            return await entry_materials(
                self.execution, self._recorded_documents, role, observation, configured
            )

        return read

    def _combat(self, opening: native.Observation, notes: str) -> DecisionSource:
        if self._combat_factory is not None:
            return self._combat_factory(self.execution, self.recovery, opening, notes)
        assert self._model is not None
        self._combat_stream += 1
        return CombatAgent(
            RecordedLlm(
                self._model,
                self.journal,
                role=native.DecisionOwner.COMBAT,
                stream=f"combat:{self._combat_stream}",
            ),
            self._tools(native.DecisionOwner.COMBAT),
            self.recovery,
            opening_notes=notes,
            prompts=self._prompts,
            materials=self._materials(native.DecisionOwner.COMBAT, opening),
        )
