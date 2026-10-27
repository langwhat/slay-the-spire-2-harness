"""One run's model role retains notes while its act contexts are replaceable."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import MISSING

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import RunActInput, RunHandoffInput
from sts2_harness.decisions import ActTask, DecisionSource, DecisionTask, HandoffTask
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import ModelClient, TextMessage, TextRole
from sts2_harness.material_text import MaterialParts, MaterialUnavailable
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import Reason, ReasonCode
from sts2_harness.tools import ToolCatalog


@dataclass(frozen=True, slots=True)
class ActStart:
    observation: native.Observation


class RunRole(DecisionSource, Protocol):
    async def enter_act(self, start: ActStart) -> None: ...


class RunAgent(Agent):
    def __init__(
        self,
        client: Callable[[], ModelClient],
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        prompts: PromptReader | None = None,
        materials: MaterialReader | None = None,
    ) -> None:
        if tools.notes is None:
            raise ValueError("Run requires the run's notes owner.")
        super().__init__(None, tools, recovery, materials=materials)
        self._client = client
        self._notes = tools.notes
        self._prompts = prompts if prompts is not None else PackagedPrompts()
        self._notes_delivered = False

    async def enter_act(self, start: ActStart) -> None:
        if self._pending_call is not None:
            raise RuntimeError("Accept the previous action result before entering an act.")
        try:
            documents = (
                await self._materials(start.observation) if self._materials is not None else ()
            )
            context = self._initial_context(documents)
        except MaterialUnavailable as error:
            raise DecisionInterrupted(
                Reason(code=ReasonCode.CONTENT_UNAVAILABLE, message=str(error)), True
            ) from error
        client = self._client()
        self._llm = client
        self._context = context
        self._notes_delivered = False

    async def _prepare(self, observation: native.Observation) -> Context:
        if self._context is None:
            await self.enter_act(ActStart(observation))
            assert self._context is not None
            return self._context
        return await super()._prepare(observation)

    def _initial_context(self, documents: tuple[DocumentView, ...]) -> Context:
        blocks = tuple(
            self._prompts.read(name)
            for name in (
                "run-tasks",
                "run-report-reader",
                "run-task-rules",
                "projected-reader",
                "run-observations",
            )
        )
        system = (TextMessage(TextRole.SYSTEM, MaterialParts(blocks)),)
        if documents:
            system += (TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=documents)),)
        return Context(native.DecisionOwner.RUN, system=system)

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        notes = self._notes.read() if not self._notes_delivered else MISSING
        if isinstance(task, HandoffTask):
            context.append_task(RunHandoffInput(task.handoff, notes))
        elif isinstance(task, ActTask):
            context.append_task(RunActInput(notes, task.handoff), observation)
        else:
            raise TypeError("Run accepts action and handoff tasks only.")
        self._notes_delivered = True
