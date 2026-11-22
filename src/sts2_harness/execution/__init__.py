"""Admit and close one model-independent decision and its ordered actions."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import MISSING, ValidationError

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, DisplayResult, InspectResult
from sts2_harness.decisions import (
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    CardRewardPlan,
    DecisionSource,
    DecisionTask,
    ReviewTask,
    SelectStep,
    TaskComplete,
)
from sts2_harness.diagnostics import BatchSettled, BatchStatus, BatchSubmitted, Diagnostics
from sts2_harness.execution.recovery import DecisionInterrupted, Interrupted, ToolRecoveryBudget
from sts2_harness.execution.rewards import reward_steps, reward_stopped
from sts2_harness.fights import FightHistory
from sts2_harness.game import (
    ControlWriteError as ControlWriteError,
)
from sts2_harness.game import (
    GameIO,
    Presented,
    RunEnded,
    RunEndReason,
    unwrap,
)
from sts2_harness.game import (
    GameLifetimeEnded as GameLifetimeEnded,
)
from sts2_harness.game import (
    GameReadError as GameReadError,
)
from sts2_harness.results import (
    ActionCompleted,
    ActionOutcome,
    ActionStopped,
    ActionToolResult,
    ActionUnknown,
    CardRewardResult,
    Reason,
    ReasonCode,
    Suggestion,
)
from sts2_harness.results import (
    Rejected as OutcomeRejected,
)


@dataclass(frozen=True, slots=True)
class Executed:
    proposal: ActionProposal
    outcome: ActionToolResult
    pause_required: bool = False
    handoff_processed: bool = False
    ended: RunEnded | None = None


@dataclass(frozen=True, slots=True)
class RouteChanged:
    observation: native.Observation


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: Reason
    observation: native.Observation | None
    pause_required: bool = False


type UnitResult = Executed | TaskComplete | RouteChanged | Rejected | Interrupted


class Execution:
    def __init__(
        self,
        game: GameIO,
        *,
        game_instance_id: str,
        run_id: str,
        recovery: ToolRecoveryBudget | None = None,
        diagnostics: Diagnostics | None = None,
        display: Callable[[str], Awaitable[DisplayResult]] | None = None,
    ) -> None:
        self._diagnostics = diagnostics
        self._game = game
        self._recovery = recovery
        self._game_instance_id = game_instance_id
        self._run_id = run_id
        self._display = display
        self._last_displayed_reason: str | None = None
        self.fights = FightHistory()
        self._observed: Callable[[native.Observation], None] | None = None

    async def display_reason(self, reason: str | None) -> None:
        if self._display is None or reason is None:
            return
        text = reason.strip()
        if not text or text == self._last_displayed_reason:
            return
        try:
            reply = await self._display(text)
        except ClientUnavailable:
            return
        if isinstance(reply, native.DisplayReply):
            self._last_displayed_reason = text

    def end_reason(self, location: native.Location | None) -> RunEndReason:
        """Classify a confirmed lifetime end against this execution's bound run."""
        if location is None or location.run_id is None:
            return RunEndReason.NO_RUN
        if location.game_instance_id != self._game_instance_id or location.run_id != self._run_id:
            return RunEndReason.RUN_CHANGED
        return RunEndReason.TERMINAL

    def run_ended(self, observation: native.Observation) -> RunEnded | None:
        reason = self.end_reason(observation.location)
        if reason is not RunEndReason.TERMINAL or observation.state.phase is native.Phase.TERMINAL:
            return RunEnded(reason, observation)
        return None

    async def observe(self) -> native.Observation:
        result = unwrap(await self._game.observe())
        self._record_observation(result.observation)
        return result.observation

    async def inspect(self, query: native.InspectQuery) -> InspectResult:
        reply = await self._game.inspect(query)
        for observation in reply.observations:
            self._record_observation(observation)
        return unwrap(reply).result

    async def catalog(self, request: native.CatalogRequest) -> CatalogResult:
        return unwrap(await self._game.catalog(request)).result

    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> Presented:
        reply = await self._game.present(route, edges)
        for observation in reply.observations:
            self._record_observation(observation)
        return unwrap(reply)

    async def pause_until_resumed(self, reason: Reason | None) -> None:
        unwrap(await self._game.pause_until_resumed(reason))
        if self._recovery is not None:
            self._recovery.reset()

    def _record_observation(self, observation: native.Observation) -> None:
        self.fights.observe(observation)
        if self._observed is not None:
            self._observed(observation)

    async def run_unit(
        self,
        source: DecisionSource,
        task: DecisionTask,
        *,
        observed: Callable[[native.Observation], None] | None = None,
    ) -> UnitResult:
        self._observed = observed
        try:
            return await self._run_unit(source, task)
        finally:
            self._observed = None

    async def admit(self) -> native.Observation | RouteChanged | Rejected | Interrupted:
        """Admit initialization or routing with the shared gate and read-failure budget."""
        try:
            admission = await self._game.admit()
            for observation in admission.observations:
                self._record_observation(observation)
            result = unwrap(admission)
            if not result.allowed:
                return Rejected(
                    Reason(
                        code=ReasonCode.CONTROL_UNAVAILABLE,
                        message="The game is paused. Start it before submitting another action.",
                    ),
                    None,
                )
            observation = admission.observations[-1]
        except (ClientUnavailable, GameReadError) as error:
            return Interrupted(
                Reason(code=ReasonCode.QUERY_FAILED, message=str(error)),
                self._recovery.record_failure() if self._recovery is not None else False,
            )
        if self.run_ended(observation) is not None:
            return RouteChanged(observation)
        return observation

    async def _run_unit(self, source: DecisionSource, task: DecisionTask) -> UnitResult:
        """Close feedback; no-run raises GameLifetimeEnded without retry cost."""
        admitted = await self.admit()
        if not isinstance(admitted, native.Observation):
            return admitted
        observation = admitted
        location = observation.location
        if isinstance(task, ActTask) and location != native.Location.model_validate(
            task.expected_route.model_dump()
        ):
            return RouteChanged(observation)
        try:
            decision = await source.decide(task, observation)
        except DecisionInterrupted as error:
            return Interrupted(error.reason, error.pause_required)
        if isinstance(decision, TaskComplete):
            if isinstance(task, ActTask):
                raise TypeError("An action task requires an action proposal.")
            return decision
        if isinstance(task, ActTask):
            problem = _proposal_problem(decision.proposal, task.expected_route.owner)
            suggestion = "Submit a complete valid action proposal."
        else:
            task_name = "review" if isinstance(task, ReviewTask) else "handoff"
            problem = Reason(
                code=ReasonCode.WRONG_ROLE,
                message=f"This {task_name} cannot take game actions.",
            )
            suggestion = f"Complete this {task_name} without taking game actions."
        if problem is not None:
            result = ActionToolResult(
                OutcomeRejected(
                    reason=problem,
                    suggested_actions=(Suggestion(message=suggestion),),
                ),
                observation,
            )
            await source.accept(result)
            pause_required = (
                self._recovery.record_outcome(result) if self._recovery is not None else False
            )
            return Rejected(problem, observation, pause_required)
        assert isinstance(task, ActTask)
        if isinstance(decision.proposal, CardRewardPlan):
            result, final_read_failed, ended = await self._execute_rewards(
                decision.proposal, task.expected_route
            )
        else:
            result, final_read_failed, ended = await self._execute_plan(
                decision.proposal, task.expected_route
            )
        await source.accept(result)
        pause_required = False
        if self._recovery is not None:
            pause_required = (
                self._recovery.record_failure()
                if final_read_failed
                else self._recovery.record_outcome(result)
            )
        return Executed(
            decision.proposal, result, pause_required, decision.handoff_processed, ended
        )

    async def _execute_rewards(
        self, plan: CardRewardPlan, route: native.RouteKey
    ) -> tuple[ActionToolResult, bool, RunEnded | None]:
        reports: list[CardRewardResult] = []
        current: native.Observation | None = plan.before
        for index, choice in enumerate(plan.choices):
            if current is None:
                break
            before = current
            steps = reward_steps(plan, index, before)
            if steps is None:
                break
            if not steps:
                await self.display_reason(choice.reason)
                reports.append(
                    CardRewardResult(before, before, ActionCompleted(completed_steps=0), True)
                )
                continue
            result, read_failed, ended = await self._execute_plan(
                ActionProposal(steps, choice.reason, allow_map_travel=False),
                route,
                reward=(plan, index),
            )
            current = result.observation
            outcome = result.outcome
            reports.append(CardRewardResult(before, current, outcome))
            if isinstance(outcome, ActionCompleted):
                if ended is not None or index + 1 == len(plan.choices):
                    completed = ActionCompleted(completed_steps=index + 1)
                    return ActionToolResult(completed, current, tuple(reports)), read_failed, ended
                if read_failed:
                    return (
                        ActionToolResult(reward_stopped(index + 1), current, tuple(reports)),
                        True,
                        None,
                    )
                if current is not None and current.input_readiness not in (
                    native.InputReadiness.READY,
                    native.InputReadiness.SELECTION,
                ):
                    admitted = await self.admit()
                    if isinstance(admitted, RouteChanged):
                        current = admitted.observation
                        ended = self.run_ended(current)
                        return (
                            ActionToolResult(reward_stopped(index + 1), current, tuple(reports)),
                            False,
                            ended,
                        )
                    if not isinstance(admitted, native.Observation):
                        return (
                            ActionToolResult(reward_stopped(index + 1), current, tuple(reports)),
                            isinstance(admitted, Interrupted),
                            None,
                        )
                    current = admitted
                continue
            if isinstance(outcome, ActionUnknown):
                batch_outcome: ActionOutcome = outcome.model_copy(
                    update={"confirmed_steps": index, "uncertain_step": index + 1}
                )
            elif isinstance(outcome, ActionStopped):
                batch_outcome = outcome.model_copy(
                    update={"completed_steps": index, "next_step": index + 1}
                )
            else:
                batch_outcome = outcome
            return ActionToolResult(batch_outcome, current, tuple(reports)), read_failed, ended
        outcome = (
            ActionCompleted(completed_steps=len(reports))
            if len(reports) == len(plan.choices)
            else reward_stopped(len(reports))
        )
        return ActionToolResult(outcome, current, tuple(reports)), False, None

    async def _execute_plan(
        self,
        proposal: ActionProposal,
        route: native.RouteKey,
        *,
        reward: tuple[CardRewardPlan, int] | None = None,
    ) -> tuple[ActionToolResult, bool, RunEnded | None]:
        steps = tuple(_native_step(step) for step in proposal.steps)
        confirmed = 0
        status = BatchStatus.INTERRUPTED
        reason: ReasonCode | None = None
        uncertain_step: int | None = None
        ended: RunEnded | None = None
        if self._diagnostics is not None:
            self._diagnostics.emit(BatchSubmitted(planned=len(steps)))
        try:
            outcome: ActionOutcome = ActionCompleted(completed_steps=len(steps))
            for index, step in enumerate(steps):
                uncertain_step = index + 1
                if reward is not None and isinstance(step, native.Select):
                    plan, group = reward
                    admitted = await self.admit()
                    ready = (
                        reward_steps(plan, group, admitted)
                        if isinstance(admitted, native.Observation)
                        else None
                    )
                    if ready != (SelectStep(step.refs),):
                        outcome = reward_stopped(index)
                        reason = outcome.reason.code
                        status = BatchStatus.STOPPED
                        uncertain_step = None
                        break
                if not proposal.allow_map_travel and isinstance(step, native.Interact):
                    try:
                        before_step = await self.observe()
                    except (ClientUnavailable, GameReadError):
                        before_step = None
                    if reward is not None:
                        plan, group = reward
                        verified = reward_steps(plan, group, before_step) if before_step else None
                        if verified != proposal.steps:
                            outcome = reward_stopped(index)
                            reason = outcome.reason.code
                            status = BatchStatus.STOPPED
                            uncertain_step = None
                            break
                    state = before_step.state if before_step is not None else None
                    action = (
                        next((item for item in state.actions if item.ref == step.action), None)
                        if isinstance(state, native.RunState)
                        else None
                    )
                    if (
                        not isinstance(state, native.RunState)
                        or (state.screen is native.RunScreen.MAP and action is None)
                        or (action is not None and action.destination is not MISSING)
                    ):
                        outcome = ActionStopped(
                            completed_steps=index,
                            next_step=index + 1,
                            reason=Reason(
                                code=ReasonCode.ILLEGAL_ACTION,
                                message=(
                                    "This interaction could not be verified as a non-map action; "
                                    "use run_next for map travel."
                                ),
                            ),
                            suggested_actions=(
                                Suggestion(
                                    message="Inspect the current state and use run_next for travel."
                                ),
                            ),
                        )
                        reason = outcome.reason.code
                        status = BatchStatus.STOPPED
                        uncertain_step = None
                        break
                if index == 0:
                    await self.display_reason(proposal.reason)
                settlement = await self._game.settle(route, step)
                for observation in settlement.observations:
                    self.fights.observe(observation)
                settled = unwrap(settlement)
                receipt = settled.receipt
                if isinstance(receipt, native.ReceiptCompleted):
                    confirmed += 1
                    uncertain_step = None
                    if receipt.boundary is native.InputReadiness.TERMINAL:
                        outcome = ActionCompleted(completed_steps=confirmed)
                        status = BatchStatus.COMPLETED
                        ended = RunEnded(RunEndReason.TERMINAL, None)
                        break
                    continue
                if receipt is None or isinstance(receipt, native.ReceiptUnknown):
                    outcome = ActionUnknown(
                        confirmed_steps=index,
                        uncertain_step=index + 1,
                        reason=Reason(
                            code=ReasonCode.EXECUTION_UNCONFIRMED,
                            message=(
                                receipt.error.message
                                if isinstance(receipt, native.ReceiptUnknown)
                                else "The step's effects could not be confirmed."
                            ),
                        ),
                        suggested_actions=(
                            Suggestion(
                                message=(
                                    "Wait for native input to be ready, inspect the current state, "
                                    "and make a new decision; do not blindly replay this step "
                                    "or the remaining batch."
                                )
                            ),
                        ),
                    )
                else:
                    outcome = ActionStopped(
                        completed_steps=index,
                        next_step=index + 1,
                        reason=Reason(
                            code=_reason_code(receipt.error.code), message=receipt.error.message
                        ),
                        suggested_actions=(
                            Suggestion(
                                message=(
                                    "Use the current state to choose a new action; "
                                    "the remaining steps were not run."
                                )
                            ),
                        ),
                    )
                reason = outcome.reason.code
                status = (
                    BatchStatus.UNKNOWN
                    if isinstance(outcome, ActionUnknown)
                    else BatchStatus.STOPPED
                )
                if isinstance(outcome, ActionStopped):
                    uncertain_step = None
                break
            else:
                status = BatchStatus.COMPLETED
            final_read_failed = False
            try:
                final_observation = await self.observe()
            except GameLifetimeEnded as error:
                final_observation = None
                if ended is not None:
                    ended = RunEnded(self.end_reason(error.location), None)
            except (ClientUnavailable, GameReadError):
                final_observation = None
                final_read_failed = True
            if ended is not None and final_observation is not None:
                ended = self.run_ended(final_observation) or ended
            return ActionToolResult(outcome, final_observation), final_read_failed, ended
        finally:
            if self._diagnostics is not None:
                self._diagnostics.emit(
                    BatchSettled(
                        planned=len(steps),
                        confirmed=confirmed,
                        outcome=status,
                        reason=reason,
                        uncertain_step=uncertain_step,
                    )
                )


def _reason_code(code: native.ErrorCode) -> ReasonCode:
    if code is native.ErrorCode.INVALID_REQUEST:
        return ReasonCode.INVALID_ARGUMENTS
    if code is native.ErrorCode.ROUTE_CHANGED:
        return ReasonCode.ROLE_CHANGED
    if code is native.ErrorCode.INVALID_REFS:
        return ReasonCode.REFERENCE_EXPIRED
    if code is native.ErrorCode.WRONG_OWNER:
        return ReasonCode.WRONG_ROLE
    if code is native.ErrorCode.SELECTION_REQUIRED:
        return ReasonCode.SELECTION_REQUIRED
    if code in (native.ErrorCode.DISPATCH_FAILED, native.ErrorCode.IDENTITY_UNREGISTERED):
        return ReasonCode.CONTROL_UNAVAILABLE
    return ReasonCode.ILLEGAL_ACTION


def _native_step(step: ActionStep | SelectStep) -> native.NativeStep:
    if isinstance(step, SelectStep):
        return native.Select(kind=native.StepKind.SELECT, refs=step.refs)
    if step.kind is ActionKind.END_TURN:
        return native.EndTurn(
            kind=native.StepKind.END_TURN,
        )
    assert step.ref is not None
    if step.kind is ActionKind.PLAY_CARD:
        return native.PlayCard(
            kind=native.StepKind.PLAY_CARD,
            card=step.ref,
            target=step.target if step.target is not None else MISSING,
        )
    if step.kind is ActionKind.USE_POTION:
        return native.UsePotion(
            kind=native.StepKind.USE_POTION,
            potion=step.ref,
            target=step.target if step.target is not None else MISSING,
        )
    if step.kind is ActionKind.DISCARD_POTION:
        return native.DiscardPotion(kind=native.StepKind.DISCARD_POTION, potion=step.ref)
    return native.Interact(kind=native.StepKind.INTERACT, action=step.ref)


def _proposal_problem(proposal: ActionProposal, owner: native.DecisionOwner) -> Reason | None:
    if isinstance(proposal, CardRewardPlan):
        if owner is not native.DecisionOwner.RUN or not proposal.choices:
            return Reason(
                code=ReasonCode.WRONG_ROLE, message="Card rewards require a Run decision."
            )
        return None
    if not proposal.steps:
        return Reason(code=ReasonCode.INVALID_ARGUMENTS, message="An action proposal needs steps.")
    for step in proposal.steps:
        if isinstance(step, ActionStep):
            if owner is native.DecisionOwner.RUN and step.kind in (
                ActionKind.PLAY_CARD,
                ActionKind.END_TURN,
            ):
                return Reason(
                    code=ReasonCode.WRONG_ROLE, message="This action is only available to Combat."
                )
            needs_ref = step.kind is not ActionKind.END_TURN
            permits_target = step.kind in (ActionKind.PLAY_CARD, ActionKind.USE_POTION)
            if needs_ref != (step.ref is not None) or (
                step.target is not None and not permits_target
            ):
                return Reason(
                    code=ReasonCode.INVALID_ARGUMENTS,
                    message="The action has missing or disallowed arguments.",
                )
        try:
            _native_step(step)
        except ValidationError:
            return Reason(
                code=ReasonCode.INVALID_ARGUMENTS,
                message="Use nonempty references and distinct selection candidates.",
            )
    return None
