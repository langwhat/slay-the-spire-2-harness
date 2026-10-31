"""One act's model role uses notes retained by the run session."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import MISSING

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.auto import choose_auto, shop_controls
from sts2_harness.capacity import CompactionDecision, CompactionValidation
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import AutomaticProgress, RunActInput, RunHandoffInput
from sts2_harness.decisions import (
    ActionDecision,
    ActTask,
    Decision,
    DecisionSource,
    DecisionTask,
    HandoffTask,
    TaskComplete,
)
from sts2_harness.diagnostics import AutoFinished, Diagnostics
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import (
    FailureKind,
    ModelClient,
    ModelReply,
    ModelUnavailable,
    RequestPreparation,
    TextMessage,
    TextRole,
)
from sts2_harness.material_text import MaterialParts, MaterialUnavailable
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import ActionCompleted, ActionToolResult, Reason, ReasonCode
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
        diagnostics: Diagnostics | None = None,
    ) -> None:
        if tools.notes is None:
            raise ValueError("Run requires the run's notes owner.")
        super().__init__(None, tools, recovery, materials=materials, diagnostics=diagnostics)
        self._client = client
        self._notes = tools.notes
        self._prompts = prompts if prompts is not None else PackagedPrompts()
        self._notes_delivered = False
        self._pending_auto: tuple[AutomaticProgress, native.Observation] | None = None
        self._force_strategy = False
        self._shop_strategy_node: native.Location | None = None

    async def _request(self, context: Context) -> ModelReply:
        assert self._llm is not None
        client = self._llm
        tools = self._tools.definitions
        before = context.model_input()
        prepared = client.prepare(before, tools)
        estimate = prepared.measurement.estimated_input_tokens
        complete_id, active_id = context.group_ids
        group_id = active_id or complete_id
        assert group_id is not None
        candidate = context.compaction_candidate()
        baseline = next(
            message
            for message in candidate
            if isinstance(message, TextMessage) and message.text.startswith('{"context_baseline":')
        )
        policy = client.capacity_policy
        below_trigger = (
            estimate * policy.trigger_denominator < policy.context_window * policy.trigger_numerator
        )
        already_minimal = candidate == before and estimate < policy.context_window
        if below_trigger or already_minimal:
            evidence = RequestPreparation(
                group_id,
                baseline,
                before,
                estimate,
                CompactionDecision.UNCHANGED,
                before,
                estimate,
                complete_id,
                active_id,
                CompactionValidation.NOT_NEEDED,
            )
            return await client.complete_prepared(prepared, preparation=evidence)
        try:
            context.validate_compaction(candidate)
        except ValueError:
            evidence = RequestPreparation(
                group_id,
                baseline,
                before,
                estimate,
                CompactionDecision.INVALID_CANDIDATE,
                None,
                None,
                complete_id,
                active_id,
                CompactionValidation.FAILED,
            )
            failure = ModelUnavailable(FailureKind.CAPACITY)
            await client.interrupt_preparation(evidence, tools, failure)
            raise failure from None
        compacted = client.prepare(candidate, tools)
        after = compacted.measurement.estimated_input_tokens
        if after >= policy.context_window:
            evidence = RequestPreparation(
                group_id,
                baseline,
                before,
                estimate,
                CompactionDecision.MINIMUM_EXCEEDS_WINDOW,
                candidate,
                after,
                complete_id,
                active_id,
                CompactionValidation.PASSED,
            )
            failure = ModelUnavailable(FailureKind.CAPACITY)
            await client.interrupt_preparation(evidence, tools, failure)
            raise failure
        context.install_compaction(candidate)
        evidence = RequestPreparation(
            group_id,
            baseline,
            before,
            estimate,
            CompactionDecision.COMPACTED,
            candidate,
            after,
            complete_id,
            active_id,
            CompactionValidation.PASSED,
        )
        return await client.complete_prepared(compacted, preparation=evidence)

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if self._pending_auto is not None or self._pending_call is not None:
            raise RuntimeError("Accept the previous action result before another decision.")
        if self._context is None:
            await self.enter_act(ActStart(observation))
        assert self._context is not None
        if (
            isinstance(task, ActTask)
            and not self._force_strategy
            and not self._context.has_active_group
        ):
            automatic = choose_auto(
                observation, shop_strategy=self._shop_strategy_node == observation.location
            )
            if automatic is not None:
                self._context.begin_group(observation, self._notes.read())
                proposal, progress = automatic
                self._pending_auto = progress, observation
                return ActionDecision(proposal)
        if isinstance(observation.state, native.RunState) and shop_controls(observation.state):
            self._shop_strategy_node = observation.location
        self._context.begin_group(observation, self._notes.read())
        decision = await super().decide(task, observation)
        if isinstance(decision, TaskComplete):
            self._context.finish_group()
            self._force_strategy = False
        return decision

    async def accept(self, outcome: ActionToolResult) -> None:
        if self._pending_auto is None:
            await super().accept(outcome)
            assert self._context is not None
            self._context.finish_group()
            self._force_strategy = False
            return
        progress, before = self._pending_auto
        if self._diagnostics is not None:
            self._diagnostics.emit(
                AutoFinished(
                    label=progress.label,
                    outcome=outcome.outcome.status,
                    reason=None
                    if isinstance(outcome.outcome, ActionCompleted)
                    else outcome.outcome.reason.code,
                    role=native.DecisionOwner.RUN,
                )
            )
        assert self._context is not None
        self._context.append_automatic(progress, outcome)
        self._context.finish_group()
        after = outcome.observation
        # Localized text changes alone cannot establish gameplay progress.
        self._force_strategy = (
            not isinstance(outcome.outcome, ActionCompleted)
            or after is None
            or after.game_language != before.game_language
            or after.model_copy(update={"stamp": before.stamp}) == before
        )
        self._pending_auto = None

    async def enter_act(self, start: ActStart) -> None:
        if self._pending_call is not None or self._pending_auto is not None:
            raise RuntimeError("Accept the previous action result before entering an act.")
        if self._context is not None:
            raise RuntimeError("A Run Agent cannot enter more than one act.")
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
        blocks = (
            self._prompts.read("run-tasks"),
            self._prompts.read("run-report-reader"),
            self._prompts.read("run-task-rules"),
            "\n",
            self._prompts.read("run-projected-reader"),
            "\n",
            self._prompts.read("run-observations"),
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
