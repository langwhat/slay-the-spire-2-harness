"""One battle's model role, execution units, trustworthy facts, and optional review."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files

from pydantic import MISSING

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import CombatActInput, CombatReviewInput
from sts2_harness.decisions import (
    ActTask,
    BattleLocation,
    BattleReport,
    BoundaryValues,
    CombatHandoff,
    DecisionSource,
    DecisionTask,
    ReviewTask,
    TaskComplete,
)
from sts2_harness.execution import Executed, Execution, Rejected, RouteChanged
from sts2_harness.execution.recovery import Interrupted, ModelFailure, ToolRecoveryBudget
from sts2_harness.game import GameLifetimeEnded
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import TextMessage, TextRole
from sts2_harness.material_text import MaterialParts, MaterialText, MaterialUnavailable
from sts2_harness.results import Reason, ReasonCode
from sts2_harness.tools import ToolCatalog

_LOG = logging.getLogger(__name__)


def _system() -> MaterialParts:
    blocks: list[MaterialText | str] = []
    for name in (
        "combat-tasks",
        "report-reader",
        "combat-task-rules",
        "projected-reader",
        "combat-observations",
    ):
        try:
            body = files("sts2_harness.prompts").joinpath(name + ".md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise MaterialUnavailable(
                "The required Combat instructions could not be read."
            ) from None
        blocks.append(MaterialText.create("prompt:" + name, body))
    return MaterialParts(tuple(blocks))


class CombatAgent(Agent):
    def __init__(
        self,
        llm: Llm,
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        opening_notes: str,
        materials: MaterialReader | None = None,
    ) -> None:
        super().__init__(llm, tools, recovery, materials=materials)
        self._opening_notes = opening_notes
        self._opening_delivered = False

    def _initial_context(self, documents: tuple[DocumentView, ...]) -> Context:
        system = (TextMessage(TextRole.SYSTEM, _system()),)
        if documents:
            system += (TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=documents)),)
        return Context(native.DecisionOwner.COMBAT, system=system)

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        if isinstance(task, ReviewTask):
            context.append_task(CombatReviewInput(task.report))
            return
        if not isinstance(task, ActTask):
            raise TypeError("Combat accepts action and review tasks only.")
        context.append_task(
            CombatActInput(
                self._opening_notes if not self._opening_delivered else MISSING,
            ),
            observation,
        )
        self._opening_delivered = True


class CombatEndReason(StrEnum):
    TERMINAL = "terminal"
    RUN_CHANGED = "run_changed"
    NO_RUN = "no_run"


@dataclass(frozen=True, slots=True)
class CombatEnded:
    reason: CombatEndReason
    observation: native.Observation | None


class CombatTask:
    def __init__(
        self,
        execution: Execution,
        role: DecisionSource,
        opening: native.Observation,
        recovery: ToolRecoveryBudget,
    ) -> None:
        if not isinstance(opening.state, native.CombatState):
            raise ValueError("A Combat task requires a trustworthy opening battle observation.")
        self._execution = execution
        self._role = role
        self._opening = opening
        self._last = opening
        self._last_turn = opening.state.turn
        self._recovery = recovery

    def _same_run(self, location: native.Location) -> bool:
        return (
            location.game_instance_id == self._opening.location.game_instance_id
            and location.run_id == self._opening.location.run_id
        )

    def _ended(self, observation: native.Observation) -> CombatEnded | None:
        if observation.location.run_id is None:
            return CombatEnded(CombatEndReason.NO_RUN, observation)
        if not self._same_run(observation.location):
            return CombatEnded(CombatEndReason.RUN_CHANGED, observation)
        if observation.state.phase is native.Phase.TERMINAL:
            return CombatEnded(CombatEndReason.TERMINAL, observation)
        return None

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
            result=native.TerminalResult.UNKNOWN,
            last_observed_turn=self._last_turn,
            before=BoundaryValues(hp=before.hp, max_hp=before.max_hp, gold=before.gold),
            after=BoundaryValues(hp=after.hp, max_hp=after.max_hp, gold=after.gold),
        )

    async def run(self) -> CombatHandoff | CombatEnded:
        try:
            while True:
                route = native.RouteKey.model_validate(self._last.location.model_dump())
                result = await self._execution.run_unit(
                    self._role, ActTask(route), observed=self._observed
                )
                observation = (
                    result.outcome.observation
                    if isinstance(result, Executed)
                    else result.observation
                    if isinstance(result, (RouteChanged, Rejected))
                    else None
                )
                battle_finished = False
                if observation is not None:
                    ended = self._ended(observation)
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
            while True:
                reviewed = await self._execution.run_unit(self._role, ReviewTask(report))
                if isinstance(reviewed, TaskComplete):
                    self._recovery.reset()
                    return CombatHandoff(report, reviewed.text)
                if isinstance(reviewed, Interrupted) and not reviewed.pause_required:
                    continue
                if isinstance(reviewed, RouteChanged):
                    ended = self._ended(reviewed.observation)
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
            if error.location is None or error.location.run_id is None:
                kind = CombatEndReason.NO_RUN
            else:
                kind = (
                    CombatEndReason.TERMINAL
                    if self._same_run(error.location)
                    else CombatEndReason.RUN_CHANGED
                )
            return CombatEnded(kind, None)
