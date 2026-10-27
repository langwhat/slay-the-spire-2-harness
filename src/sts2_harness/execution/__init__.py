"""Admit and close one model-independent decision and its ordered actions."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import MISSING, ValidationError

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, InspectResult
from sts2_harness.decisions import (
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    DecisionSource,
    DecisionTask,
    ReviewTask,
    SelectStep,
    TaskComplete,
)
from sts2_harness.execution.recovery import DecisionInterrupted, Interrupted, ToolRecoveryBudget
from sts2_harness.game import (
    ControlWriteError as ControlWriteError,
)
from sts2_harness.game import (
    GameIO,
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
    ) -> None:
        self._game = game
        self._recovery = recovery
        self._game_instance_id = game_instance_id
        self._run_id = run_id
        self._observed: Callable[[native.Observation], None] | None = None

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

    async def pause_until_resumed(self, reason: Reason) -> None:
        unwrap(await self._game.pause_until_resumed(reason))
        if self._recovery is not None:
            self._recovery.reset()

    def _record_observation(self, observation: native.Observation) -> None:
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
        result, final_read_failed = await self._execute_plan(decision.proposal, task.expected_route)
        await source.accept(result)
        pause_required = False
        if self._recovery is not None:
            pause_required = (
                self._recovery.record_failure()
                if final_read_failed
                else self._recovery.record_outcome(result)
            )
        return Executed(decision.proposal, result, pause_required, decision.handoff_processed)

    async def _execute_plan(
        self,
        proposal: ActionProposal,
        route: native.RouteKey,
    ) -> tuple[ActionToolResult, bool]:
        steps = tuple(_native_step(step) for step in proposal.steps)
        outcome: ActionOutcome = ActionCompleted(completed_steps=len(steps))
        for index, step in enumerate(steps):
            settled = unwrap(await self._game.settle(route, step))
            receipt = settled.receipt
            if isinstance(receipt, native.ReceiptCompleted):
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
            break
        final_read_failed = False
        try:
            final_observation = await self.observe()
        except GameLifetimeEnded:
            final_observation = None
        except (ClientUnavailable, GameReadError):
            final_observation = None
            final_read_failed = True
        return ActionToolResult(outcome, final_observation), final_read_failed


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
