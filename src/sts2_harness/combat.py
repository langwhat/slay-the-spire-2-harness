"""One battle's model role, execution units, trustworthy facts, and optional review."""

import logging

from pydantic import MISSING

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import CombatActInput, CombatReviewInput
from sts2_harness.decisions import (
    ActTask,
    BattleLocation,
    BattleReport,
    BattleResult,
    BoundaryValues,
    CombatHandoff,
    Decision,
    DecisionSource,
    DecisionTask,
    ReviewTask,
    TaskComplete,
)
from sts2_harness.diagnostics import BattleEnd, BattleFinished, BattleStarted, Diagnostics
from sts2_harness.execution import Executed, Execution, Rejected, RouteChanged
from sts2_harness.execution.recovery import Interrupted, ModelFailure, ToolRecoveryBudget
from sts2_harness.game import GameLifetimeEnded, GameReadError, RunEnded, SessionStopped
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import ModelClient, TextMessage, TextRole
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.note_values import NotesView
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import Reason, ReasonCode
from sts2_harness.tools import ToolCatalog
from sts2_harness.user_input_values import UserInputSource

_LOG = logging.getLogger(__name__)


def _system(prompts: PromptReader, *, viewer_input: bool, minimal: bool = False) -> MaterialParts:
    blocks: list[MaterialText | str] = []
    names = (
        "combat-tasks",
        "report-reader",
        "combat-task-rules",
        "state-format",
        "combat-observations",
    )
    for name in (
        *names,
        *(("combat-review",) if not minimal else ()),
        *(("viewer-input",) if viewer_input else ()),
    ):
        if blocks:
            blocks.append("\n")
        blocks.append(prompts.read(name))
    return MaterialParts(tuple(blocks))


class CombatAgent(Agent):
    def __init__(
        self,
        llm: ModelClient,
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        opening_notes: NotesView,
        prompts: PromptReader | None = None,
        materials: MaterialReader | None = None,
        diagnostics: Diagnostics | None = None,
        user_input: UserInputSource | None = None,
    ) -> None:
        super().__init__(
            llm,
            tools,
            recovery,
            materials=materials,
            diagnostics=diagnostics,
            user_input=user_input,
        )
        self._prompts = prompts if prompts is not None else PackagedPrompts()
        self._opening_notes = opening_notes
        self._opening_delivered = False

    def _initial_context(self, documents: tuple[DocumentView, ...]) -> Context:
        system = (
            TextMessage(
                TextRole.SYSTEM,
                _system(
                    self._prompts,
                    viewer_input=self._user_input is not None,
                    minimal=self._tools.minimal,
                ),
            ),
        )
        if documents:
            system += (TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=documents)),)
        return Context(native.DecisionOwner.COMBAT, system=system, minimal=self._tools.minimal)

    async def _prepare(self, observation: native.Observation) -> Context:
        context = await super()._prepare(observation)
        if isinstance(observation.state, native.CombatState):
            try:
                await self._tools.capture_history(observation)
            except (ClientUnavailable, GameReadError) as error:
                _LOG.warning("Battle-history sample unavailable: %s", error)
        return context

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        decision = await super().decide(task, observation)
        if isinstance(decision, TaskComplete):
            assert self._context is not None
            self._context.finish_group()
        return decision

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        context.begin_group(
            observation, NotesView() if self._tools.minimal else self._opening_notes
        )
        if isinstance(task, ReviewTask):
            context.append_task(CombatReviewInput(task.report))
            return
        if not isinstance(task, ActTask):
            raise TypeError("Combat accepts action and review tasks only.")
        context.append_task(
            CombatActInput(
                self._opening_notes
                if not self._opening_delivered and not self._tools.minimal
                else MISSING,
            ),
            observation,
        )
        self._opening_delivered = True


class CombatTask:
    def __init__(
        self,
        execution: Execution,
        role: DecisionSource,
        opening: native.Observation,
        recovery: ToolRecoveryBudget,
        *,
        diagnostics: Diagnostics | None = None,
        minimal: bool = False,
    ) -> None:
        self._diagnostics = diagnostics
        if not isinstance(opening.state, native.CombatState):
            raise ValueError("A Combat task requires a trustworthy opening battle observation.")
        self._execution = execution
        self._role = role
        self._opening = opening
        self._last = opening
        self._last_turn = opening.state.turn
        self._recovery = recovery
        self._minimal = minimal

    def _same_run(self, location: native.Location) -> bool:
        return (
            location.game_instance_id == self._opening.location.game_instance_id
            and location.run_id == self._opening.location.run_id
        )

    def _remember(self, observation: native.Observation) -> None:
        self._last = observation
        if isinstance(observation.state, native.CombatState) and observation.state.turn is not None:
            self._last_turn = observation.state.turn

    def _observed(self, observation: native.Observation) -> None:
        if (
            self._same_run(observation.location)
            and observation.location.node_id == self._opening.location.node_id
            and isinstance(observation.state, native.CombatState)
        ):
            self._remember(observation)

    async def _pause(self, reason: Reason | ModelFailure) -> None:
        if isinstance(reason, ModelFailure):
            reason = Reason(
                code=ReasonCode.MODEL_UNAVAILABLE,
                message=f"Model request unavailable: {reason.reason.value}.",
            )
        await self._execution.pause_until_resumed(reason)

    def _report(self) -> BattleReport:
        before = self._opening.state.player
        after = self._last.state.player
        progress = self._opening.state.progress
        return BattleReport(
            location=BattleLocation(
                act=progress.act, floor=progress.floor, room_type=progress.room_type
            ),
            result=BattleResult.WON
            if isinstance(self._last.state, native.RunState)
            and self._last.state.screen is native.RunScreen.REWARDS
            else BattleResult.UNKNOWN,
            last_observed_turn=self._last_turn,
            before=BoundaryValues(hp=before.hp, max_hp=before.max_hp, gold=before.gold),
            after=BoundaryValues(hp=after.hp, max_hp=after.max_hp, gold=after.gold),
            encounter=self._opening.encounter,
        )

    async def run(self) -> CombatHandoff | RunEnded:
        node = self._opening.location.node_id
        if self._diagnostics is not None:
            self._diagnostics.emit(BattleStarted(node_id=node, role=native.DecisionOwner.COMBAT))
        result: CombatHandoff | RunEnded | None = None
        stopped = False
        try:
            result = await self._run()
            return result
        except SessionStopped:
            stopped = True
            raise
        finally:
            if self._diagnostics is not None:
                self._diagnostics.emit(
                    BattleFinished(
                        node_id=node,
                        role=native.DecisionOwner.COMBAT,
                        ending=BattleEnd.STOPPED
                        if stopped
                        else BattleEnd.RUN_ENDED
                        if isinstance(result, RunEnded)
                        else BattleEnd.HANDOFF
                        if result is not None
                        else BattleEnd.INTERRUPTED,
                        end_reason=result.reason if isinstance(result, RunEnded) else None,
                    )
                )

    async def _run(self) -> CombatHandoff | RunEnded:
        try:
            while True:
                route = native.RouteKey.model_validate(self._last.location.model_dump())
                result = await self._execution.run_unit(
                    self._role, ActTask(route), observed=self._observed
                )
                if isinstance(result, Executed) and result.ended is not None:
                    return result.ended
                observation = (
                    result.outcome.observation
                    if isinstance(result, Executed)
                    else result.observation
                    if isinstance(result, (RouteChanged, Rejected))
                    else None
                )
                battle_finished = False
                if observation is not None:
                    ended = self._execution.run_ended(observation)
                    if ended is not None:
                        return ended
                    new_battle = (
                        observation.location.node_id != self._opening.location.node_id
                        and observation.state.phase is native.Phase.COMBAT
                    )
                    if not new_battle:
                        self._remember(observation)
                    battle_finished = (
                        observation.location.node_id != self._opening.location.node_id
                        or observation.location.owner is not native.DecisionOwner.COMBAT
                    )
                if isinstance(result, (Executed, Rejected, Interrupted)) and result.pause_required:
                    await self._pause(
                        result.reason
                        if isinstance(result, (Rejected, Interrupted))
                        else Reason(
                            code=ReasonCode.CONTROL_UNAVAILABLE,
                            message="The tool recovery budget is exhausted.",
                        )
                    )
                if battle_finished:
                    break
            report = self._report()
            if self._minimal:
                return CombatHandoff(report)
            while True:
                reviewed = await self._execution.run_unit(self._role, ReviewTask(report))
                if isinstance(reviewed, TaskComplete):
                    self._recovery.reset()
                    return CombatHandoff(report, reviewed.text)
                if isinstance(reviewed, Interrupted) and not reviewed.pause_required:
                    continue
                if isinstance(reviewed, RouteChanged):
                    ended = self._execution.run_ended(reviewed.observation)
                    if ended is not None:
                        return ended
                reason = (
                    reviewed.reason.reason.value
                    if isinstance(reviewed, Interrupted)
                    and isinstance(reviewed.reason, ModelFailure)
                    else reviewed.reason.code.value
                    if isinstance(reviewed, (Interrupted, Rejected))
                    and isinstance(reviewed.reason, Reason)
                    else type(reviewed).__name__
                )
                _LOG.error("Combat review did not complete: %s", reason)
                self._recovery.reset()
                return CombatHandoff(report)
        except GameLifetimeEnded as error:
            return RunEnded(self._execution.end_reason(error.location), None)
