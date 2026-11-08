"""Schedule one run's roles, act entry, and pending battle handoff."""

from collections.abc import Callable
from typing import Protocol, assert_never

from sts2_harness.client import models as native
from sts2_harness.combat import CombatTask
from sts2_harness.decisions import ActTask, CombatHandoff, DecisionSource, HandoffTask, TaskComplete
from sts2_harness.diagnostics import ActEntered, Diagnostics, RoleChanged
from sts2_harness.execution import Executed, Execution, Rejected, RouteChanged
from sts2_harness.execution.recovery import (
    DecisionInterrupted,
    Interrupted,
    InterruptionReason,
    ModelFailure,
    ToolRecoveryBudget,
)
from sts2_harness.game import GameLifetimeEnded, RunEnded
from sts2_harness.notes import Notes
from sts2_harness.results import Reason, ReasonCode
from sts2_harness.run import ActStart, RunRole

type CombatFactory = Callable[[native.Observation, str], DecisionSource]


class RunFlow(Protocol):
    async def run(self) -> RunEnded: ...


class DefaultFlow:
    def __init__(
        self,
        execution: Execution,
        recovery: ToolRecoveryBudget,
        notes: Notes,
        run: RunRole,
        combat: CombatFactory,
        *,
        new_run: Callable[[], RunRole],
        diagnostics: Diagnostics | None = None,
        minimal: bool = False,
    ) -> None:
        self._diagnostics = diagnostics
        self._active_role: native.DecisionOwner | None = None
        self._execution = execution
        self._recovery = recovery
        self._notes = notes
        self._run = run
        self._new_run = new_run
        self._combat = combat
        self._initialized = False
        self._act: int | None = None
        self._pending: CombatHandoff | None = None
        self._latest_handoff: CombatHandoff | None = None
        self._minimal = minimal

    def _enter_role(self, role: native.DecisionOwner) -> None:
        if self._active_role is role:
            return
        self._active_role = role
        if self._diagnostics is not None:
            self._diagnostics.emit(RoleChanged(role=role))

    async def _pause(self, reason: InterruptionReason) -> None:
        if isinstance(reason, ModelFailure):
            reason = Reason(
                code=ReasonCode.MODEL_UNAVAILABLE,
                message=f"Model request unavailable: {reason.reason.value}.",
            )
        await self._execution.pause_until_resumed(reason)

    async def run(self) -> RunEnded:
        observation: native.Observation | None = None
        entry_admitted = False
        try:
            while True:
                if observation is None:
                    admitted = await self._execution.admit()
                    if isinstance(admitted, native.Observation):
                        observation = admitted
                        entry_admitted = True
                    elif isinstance(admitted, RouteChanged):
                        ended = self._execution.run_ended(admitted.observation)
                        assert ended is not None
                        return ended
                    else:
                        if admitted.pause_required:
                            await self._pause(admitted.reason)
                        continue
                ended = self._execution.run_ended(observation)
                if ended is not None:
                    return ended
                act = observation.state.progress.act
                if self._initialized and self._act is None and act is not None:
                    self._act = act
                act_changed = act is not None and self._act is not None and act != self._act
                if not self._initialized or act_changed:
                    if not entry_admitted:
                        observation = None
                        continue
                    if self._initialized:
                        self._run = self._new_run()
                    try:
                        await self._run.enter_act(ActStart(observation))
                    except DecisionInterrupted as error:
                        if error.pause_required:
                            await self._pause(error.reason)
                        observation = None
                        continue
                    self._enter_role(native.DecisionOwner.RUN)
                    if self._diagnostics is not None:
                        self._diagnostics.emit(ActEntered(act=act))
                    self._act = act
                    self._initialized = True
                if observation.location.owner is native.DecisionOwner.COMBAT:
                    if self._pending is None or self._minimal:
                        self._enter_role(native.DecisionOwner.COMBAT)
                        role = self._combat(
                            observation, "" if self._minimal else self._notes.read()
                        )
                        result = await CombatTask(
                            self._execution,
                            role,
                            observation,
                            self._recovery,
                            diagnostics=self._diagnostics,
                            minimal=self._minimal,
                        ).run()
                        if isinstance(result, RunEnded):
                            return result
                        self._pending = self._latest_handoff = result
                        observation = None
                        continue
                    task = HandoffTask(self._pending)
                else:
                    task = ActTask(
                        native.RouteKey.model_validate(observation.location.model_dump()),
                        self._pending,
                        self._latest_handoff,
                    )
                self._enter_role(native.DecisionOwner.RUN)
                outcome = await self._execution.run_unit(self._run, task)
                entry_admitted = False
                if isinstance(outcome, TaskComplete):
                    self._recovery.reset()
                    self._pending = None
                    observation = None
                    continue
                match outcome:
                    case Executed():
                        if outcome.ended is not None:
                            return outcome.ended
                        if outcome.handoff_processed:
                            self._pending = None
                        observation = outcome.outcome.observation
                    case Rejected() | RouteChanged():
                        observation = outcome.observation
                    case Interrupted():
                        observation = None
                    case _:
                        assert_never(outcome)
                if observation is not None:
                    ended = self._execution.run_ended(observation)
                    if ended is not None:
                        return ended
                if (
                    isinstance(outcome, (Executed, Rejected, Interrupted))
                    and outcome.pause_required
                ):
                    await self._pause(
                        outcome.reason
                        if isinstance(outcome, (Rejected, Interrupted))
                        else Reason(
                            code=ReasonCode.CONTROL_UNAVAILABLE,
                            message="The tool recovery budget is exhausted.",
                        )
                    )
                    observation = None
        except GameLifetimeEnded as error:
            return RunEnded(self._execution.end_reason(error.location), None)
