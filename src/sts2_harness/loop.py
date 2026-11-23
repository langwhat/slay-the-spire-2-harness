"""Schedule one run's roles, act entry, and pending battle handoff."""

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol, assert_never

from sts2_harness.auto import BattleRewardAuto, battle_reward
from sts2_harness.client import models as native
from sts2_harness.combat import CombatTask
from sts2_harness.decisions import (
    ActTask,
    CombatHandoff,
    DecisionSource,
    HandoffTask,
    TaskComplete,
)
from sts2_harness.deck import adding_selection, next_card_reward
from sts2_harness.deck_role import DeckRole
from sts2_harness.diagnostics import ActEntered, AutoFinished, Diagnostics, RoleChanged
from sts2_harness.execution import Executed, Execution, Rejected, RouteChanged, UnitResult
from sts2_harness.execution.recovery import (
    DecisionInterrupted,
    Interrupted,
    InterruptionReason,
    ModelFailure,
    ToolRecoveryBudget,
)
from sts2_harness.game import GameLifetimeEnded, RunEnded
from sts2_harness.note_values import NotesView
from sts2_harness.notes import Notes
from sts2_harness.results import ActionCompleted, Reason, ReasonCode
from sts2_harness.route_plan import RoutePlan, reconcile_plan
from sts2_harness.run import ActStart, RunRole

type CombatFactory = Callable[[native.Observation, NotesView, RoutePlan | None], DecisionSource]
type DeckFactory = Callable[[native.Observation, native.Offer | None, frozenset[str]], DeckRole]


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
        deck: DeckFactory,
        diagnostics: Diagnostics | None = None,
        minimal: bool = False,
    ) -> None:
        self._diagnostics = diagnostics
        self._active_role: native.DecisionOwner | None = None
        self._execution: Execution = execution
        self._recovery: ToolRecoveryBudget = recovery
        self._notes: Notes = notes
        self._run: RunRole = run
        self._new_run: Callable[[], RunRole] = new_run
        self._combat: CombatFactory = combat
        self._initialized = False
        self._act: int | None = None
        self._pending: CombatHandoff | None = None
        self._latest_handoff: CombatHandoff | None = None
        self._minimal = minimal
        self._deck_factory = deck
        self._deck: DeckRole | None = None
        self._deck_node: tuple[str, str | None, str | None] | None = None
        self._resolved_cards: set[str] = set()
        self._reward_auto_finished = False

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

    def _finish_card(self, observation: native.Observation) -> None:
        if self._deck is None:
            return
        for resolution in self._deck.take_resolutions():
            result = resolution.result
            if result.observation is None:
                result = replace(result, observation=observation)
            self._run.card_progress(resolution.progress, result)
            if resolution.resolved_offer is not None:
                self._resolved_cards.add(resolution.resolved_offer)

    def _card_source(self, observation: native.Observation) -> DecisionSource:
        state = observation.state
        if not isinstance(state, native.RunState):
            self._deck = None
            return self._run
        node = (
            observation.location.game_instance_id,
            observation.location.run_id,
            observation.location.node_id,
        )
        if node != self._deck_node:
            self._resolved_cards.clear()
            self._deck = None
            self._deck_node = node
            self._reward_auto_finished = False
        if self._deck is not None:
            if self._deck.offered(observation):
                return self._deck
            self._deck = None
        if not self._reward_auto_finished and battle_reward(observation) is not None:
            return BattleRewardAuto()
        if adding_selection(observation) is not None:
            self._reward_auto_finished = True
            self._deck = self._deck_factory(observation, None, frozenset(self._resolved_cards))
            return self._deck
        if state.screen is native.RunScreen.REWARDS:
            self._reward_auto_finished = True
        offer = next_card_reward(observation, self._resolved_cards)
        if offer is None:
            return self._run
        self._deck = self._deck_factory(observation, offer, frozenset(self._resolved_cards))
        return self._deck

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
                if observation.input_readiness not in (
                    native.InputReadiness.READY,
                    native.InputReadiness.SELECTION,
                ):
                    observation = None
                    continue
                self._finish_card(observation)
                if observation.location.owner is native.DecisionOwner.COMBAT:
                    if self._pending is None or self._minimal:
                        self._enter_role(native.DecisionOwner.COMBAT)
                        role = self._combat(
                            observation,
                            NotesView() if self._minimal else self._notes.read(),
                            reconcile_plan(
                                self._run.route_plan, observation.state.progress.position
                            ).plan,
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
                source: DecisionSource = (
                    self._card_source(observation) if isinstance(task, ActTask) else self._run
                )
                if source is self._run and isinstance(task, ActTask):
                    task = replace(task, resolved_card_rewards=frozenset(self._resolved_cards))
                deck = self._deck
                if deck is not None and source is deck:
                    deck.set_route_plan(self._run.route_plan)
                outcome: UnitResult = await self._execution.run_unit(source, task)
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
                        if isinstance(source, BattleRewardAuto):
                            self._reward_auto_finished = not source.succeeded
                            assert source.progress is not None
                            self._run.card_progress(source.progress, outcome.outcome)
                            if self._diagnostics is not None:
                                result = outcome.outcome.outcome
                                self._diagnostics.emit(
                                    AutoFinished(
                                        label=source.progress.label,
                                        outcome=result.status,
                                        reason=None
                                        if isinstance(result, ActionCompleted)
                                        else result.reason.code,
                                        role=native.DecisionOwner.RUN,
                                    )
                                )
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
