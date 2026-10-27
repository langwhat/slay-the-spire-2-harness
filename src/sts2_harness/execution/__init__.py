"""Admit and close one model-independent decision and its ordered actions."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from pydantic import MISSING, ValidationError

from sts2_harness.client import ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import InspectResult
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


type SettledReceipt = (
    native.ReceiptCompleted | native.ReceiptRejected | native.ReceiptFailed | native.ReceiptUnknown
)


@dataclass(frozen=True, slots=True)
class RouteChanged:
    observation: native.Observation


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: Reason
    observation: native.Observation | None
    pause_required: bool = False


type UnitResult = Executed | TaskComplete | RouteChanged | Rejected | Interrupted


class _Admission(StrEnum):
    WAIT_FOR_START = "wait_for_start"
    REJECT_IF_PAUSED = "reject_if_paused"


async def _poll_wait() -> None:
    await asyncio.sleep(0.1)


class Execution:
    def __init__(
        self,
        client: ModClient,
        *,
        game_instance_id: str,
        run_id: str,
        recovery: ToolRecoveryBudget | None = None,
        wait: Callable[[], Awaitable[None]] = _poll_wait,
    ) -> None:
        """A shared budget selects built-in waiting; no budget selects external rejection."""
        self._client = client
        self._recovery = recovery
        self._admission = (
            _Admission.WAIT_FOR_START if recovery is not None else _Admission.REJECT_IF_PAUSED
        )
        self._wait = wait
        self._game_instance_id = game_instance_id
        self._run_id = run_id
        self._observation: native.Observation | None = None

    async def observe(self) -> native.Observation:
        result = await self._client.state()
        if isinstance(result, native.ReadError) and result.error.code is native.ErrorCode.NO_RUN:
            raise GameLifetimeEnded(None)
        if not isinstance(result, native.StateCompleted):
            raise GameReadError(result.error.message)
        self._observation = result.observation
        return result.observation

    async def inspect(self, query: native.InspectQuery) -> InspectResult:
        result = await self._client.inspect(query)
        if isinstance(
            result, (native.InspectCompleted, native.InspectRejected, native.InspectFailed)
        ):
            if result.observation is not None:
                self._observation = result.observation
        return result

    async def _status(self) -> native.Status:
        result = await self._client.status()
        if not isinstance(result, native.Status):
            raise GameReadError(result.error.message)
        return result

    def _same_run(self, location: native.Location) -> bool:
        return (
            location.game_instance_id == self._game_instance_id and location.run_id == self._run_id
        )

    async def _admit(self) -> bool:
        while True:
            status = await self._status()
            if status.location.run_id is None:
                raise GameLifetimeEnded(status.location)
            if (
                not self._same_run(status.location)
                or status.input_readiness is native.InputReadiness.TERMINAL
            ):
                return True
            if status.control_intent is native.ControlIntent.PAUSED:
                if self._admission is _Admission.REJECT_IF_PAUSED:
                    return False
            elif status.input_readiness in (
                native.InputReadiness.READY,
                native.InputReadiness.SELECTION,
            ):
                return True
            await self._wait()

    async def _fresh_observation(self) -> native.Observation:
        observation = await self.observe()
        while (
            self._same_run(observation.location)
            and observation.state.phase is not native.Phase.TERMINAL
            and observation.input_readiness
            not in (
                native.InputReadiness.READY,
                native.InputReadiness.SELECTION,
            )
        ):
            await self._wait()
            status = await self._status()
            if status.location.run_id is None:
                raise GameLifetimeEnded(status.location)
            if not self._same_run(status.location):
                return await self.observe()
            if status.input_readiness in (
                native.InputReadiness.BUSY,
                native.InputReadiness.UNAVAILABLE,
            ):
                continue
            observation = await self.observe()
        return observation

    async def pause_until_resumed(self, reason: Reason) -> None:
        """Write Pause, await explicit Start, then refresh; a dead run raises GameLifetimeEnded."""
        try:
            result = await self._client.control(native.ControlIntent.PAUSED)
        except ClientUnavailable as error:
            raise ControlWriteError(
                Reason(
                    code=ReasonCode.CONTROL_UNAVAILABLE,
                    message=f"Recovery Pause could not be confirmed: {error}",
                )
            ) from error
        if not isinstance(result, native.ControlReply):
            raise ControlWriteError(
                Reason(code=ReasonCode.CONTROL_UNAVAILABLE, message=result.error.message)
            )
        if result.mode is not native.ControlIntent.PAUSED:
            raise ControlWriteError(
                Reason(
                    code=ReasonCode.CONTROL_UNAVAILABLE,
                    message="The Mod did not confirm the requested Pause control intent.",
                )
            )
        while True:
            try:
                status = await self._status()
                if (
                    not self._same_run(status.location)
                    or status.input_readiness is native.InputReadiness.TERMINAL
                ):
                    raise GameLifetimeEnded(status.location)
                if status.control_intent is native.ControlIntent.RUNNING:
                    observation = await self.observe()
                    if (
                        not self._same_run(observation.location)
                        or observation.state.phase is native.Phase.TERMINAL
                    ):
                        raise GameLifetimeEnded(observation.location)
                    if self._recovery is not None:
                        self._recovery.reset()
                    return
            except (ClientUnavailable, GameReadError):
                await self._wait()
                continue
            await self._wait()

    async def run_unit(self, source: DecisionSource, task: DecisionTask) -> UnitResult:
        """Close feedback; no-run raises GameLifetimeEnded without retry cost."""
        try:
            if not await self._admit():
                return Rejected(
                    Reason(
                        code=ReasonCode.CONTROL_UNAVAILABLE,
                        message="The game is paused. Start it before submitting another action.",
                    ),
                    None,
                )
            observation = await self._fresh_observation()
        except (ClientUnavailable, GameReadError) as error:
            return Interrupted(
                Reason(code=ReasonCode.QUERY_FAILED, message=str(error)),
                self._recovery.record_failure() if self._recovery is not None else False,
            )
        location = observation.location
        if (
            location.game_instance_id != self._game_instance_id
            or location.run_id != self._run_id
            or observation.state.phase is native.Phase.TERMINAL
        ):
            return RouteChanged(observation)
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
            receipt = await self._settle(
                native.StepRequest(
                    operation_id=uuid4().hex,
                    expected_route=route,
                    step=step,
                )
            )
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

    async def _settle(self, request: native.StepRequest) -> SettledReceipt | None:
        try:
            reply = await self._client.submit(request)
        except ClientUnavailable:
            reply = None
        if isinstance(reply, native.InvalidRequestReply):
            return native.ReceiptRejected(
                operation_id=request.operation_id,
                status=native.ResultStatus.REJECTED,
                error=reply.error,
            )
        if isinstance(reply, native.TechnicalError):
            reply = None
        if reply is not None and reply.operation_id != request.operation_id:
            reply = None
        if reply is not None and not isinstance(reply, native.ReceiptPending):
            return reply
        while True:
            try:
                status = await self._client.status()
            except ClientUnavailable:
                return None
            if not isinstance(status, native.Status):
                return None
            receipt = status.receipt
            if receipt is None or receipt.operation_id != request.operation_id:
                return None
            if not isinstance(receipt, native.ReceiptPending):
                return receipt
            await self._wait()


class GameReadError(Exception):
    pass


class ControlWriteError(Exception):
    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__(reason.message)


class GameLifetimeEnded(Exception):
    def __init__(self, location: native.Location | None) -> None:
        self.location = location
        super().__init__("The bound run has ended or changed.")


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
