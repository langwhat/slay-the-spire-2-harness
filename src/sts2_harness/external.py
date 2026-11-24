"""Own external gameplay resources independently of built-in roles and histories."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from uuid import uuid4

from sts2_harness.client import ClientFailureKind, ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActionDecision, ActionProposal, ActTask, DecisionTask
from sts2_harness.diagnostics import Diagnostics, Event, EventKind, ExceptionDetails, Lifecycle
from sts2_harness.execution import Execution, RouteChanged
from sts2_harness.execution import Rejected as UnitRejected
from sts2_harness.game import Endpoint, GameLifetimeEnded, GameReadError, LiveGame
from sts2_harness.llm.messages import ToolCall
from sts2_harness.notes import Notes
from sts2_harness.raw import RawResult, render
from sts2_harness.results import (
    ActionToolResult,
    ExecutionResult,
    Failed,
    QueryToolResult,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
from sts2_harness.tools import QueryView, ToolCatalog, ToolName


@dataclass(frozen=True, slots=True)
class ExternalReply:
    result: RawResult
    route: native.Location | None


@dataclass(frozen=True, slots=True)
class ExternalRun:
    identity: tuple[str, str]
    execution: Execution
    run_tools: ToolCatalog
    combat_tools: ToolCatalog


class _SubmittedDecision:
    def __init__(self, proposal: ActionProposal) -> None:
        self._proposal = proposal
        self.feedback: ActionToolResult | None = None

    async def decide(self, task: DecisionTask, observation: native.Observation) -> ActionDecision:
        return ActionDecision(self._proposal)

    async def accept(self, outcome: ExecutionResult) -> None:
        assert isinstance(outcome, ActionToolResult)
        self.feedback = outcome


class ExternalBackend:
    def __init__(
        self,
        client: ModClient,
        *,
        diagnostics: Diagnostics | None = None,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
        display_reasons: bool = False,
    ) -> None:
        self._client = client
        self._diagnostics = diagnostics
        self._wait = wait
        self._display_reasons = display_reasons
        self._status_lock = asyncio.Lock()
        self._run: ExternalRun | None = None
        self._paused_instance: str | None = None
        self._last_exposed: native.Location | None = None
        self._active: asyncio.Task[ExternalReply] | None = None
        self._closing = False
        self._discovery_failures = 0
        self._discovery_cause: ClientFailureKind | ReasonCode | None = None

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closing = True
        if self._active is not None:
            await asyncio.shield(self._active)
        await self._client.__aexit__(exc_type, exc_value, traceback)

    def exposed(self, reply: ExternalReply) -> None:
        if reply.result.state is not None:
            self._last_exposed = reply.route

    def accept(self, name: ToolName, arguments: str) -> asyncio.Task[ExternalReply] | ExternalReply:
        if self._active is not None or self._closing:
            reply = reject(ReasonCode.BUSY, "Another call is active or the backend is stopping.")
            if self._diagnostics is not None:
                self._diagnostics.tool_finished(name.value, reply.result.outcome)
            return reply
        task = asyncio.create_task(self._call_boundary(name, arguments))
        self._active = task
        task.add_done_callback(self._settled)
        return task

    def _settled(self, task: asyncio.Task[ExternalReply]) -> None:
        try:
            task.result()
        finally:
            self._active = None

    def stop_accepting(self) -> None:
        self._closing = True

    async def maintain(self) -> None:
        while not self._closing:
            if self._active is None:
                try:
                    await self._status()
                except (ClientUnavailable, GameReadError) as error:
                    self._discovery_failures += 1
                    self._discovery_cause = (
                        error.reason
                        if isinstance(error, ClientUnavailable)
                        else ReasonCode.QUERY_FAILED
                    )
                    if self._diagnostics is not None:
                        self._diagnostics.lifecycle(Lifecycle.WAITING_GAME)
                        self._diagnostics.emit(
                            Event(
                                event=EventKind.DISCOVERY_RETRY,
                                endpoint=Endpoint.STATUS,
                                error=self._discovery_cause.value,
                                failures=self._discovery_failures,
                            )
                        )
            await self._wait(1.0)

    async def _status(self) -> native.Status:
        async with self._status_lock:
            return await self._connected_status()

    async def _connected_status(self) -> native.Status:
        status = await self._client.status()
        if not isinstance(status, native.Status):
            raise GameReadError("The Mod status is unavailable.")
        instance = status.location.game_instance_id
        if self._paused_instance != instance:
            control = await self._client.control(native.ControlIntent.PAUSED)
            if (
                not isinstance(control, native.ControlReply)
                or control.mode is not native.ControlIntent.PAUSED
            ):
                raise GameReadError("The Mod did not confirm Pause.")
            self._paused_instance = instance
            status = status.model_copy(update={"control_intent": native.ControlIntent.PAUSED})
        if self._diagnostics is not None:
            self._diagnostics.bind_recording(None, status.location, None)
            self._diagnostics.native_status(status, True)
            if status.location.run_id is None:
                self._diagnostics.lifecycle(Lifecycle.WAITING_RUN)
            elif status.input_readiness is native.InputReadiness.TERMINAL:
                self._diagnostics.lifecycle(Lifecycle.TERMINAL)
        if self._discovery_failures:
            if self._diagnostics is not None:
                self._diagnostics.emit(
                    Event(
                        event=EventKind.DISCOVERY_RECOVERED,
                        endpoint=Endpoint.STATUS,
                        failures=self._discovery_failures,
                        recovered_from=self._discovery_cause.value
                        if self._discovery_cause is not None
                        else None,
                    )
                )
            self._discovery_failures = 0
            self._discovery_cause = None
        return status

    async def _call_boundary(self, name: ToolName, arguments: str) -> ExternalReply:
        exception: ExceptionDetails | None = None
        try:
            reply = await self._call(name, arguments)
        except (ClientUnavailable, GameReadError, GameLifetimeEnded):
            reply = self._failed_call("The current game could not be read.")
        except Exception as error:
            exception = ExceptionDetails.capture(error)
            reply = self._failed_call("The external tool could not finish.")
        if self._diagnostics is not None:
            self._diagnostics.tool_finished(name.value, reply.result.outcome, exception=exception)
        return reply

    @staticmethod
    def _failed_call(message: str) -> ExternalReply:
        return ExternalReply(
            render(
                QueryToolResult(
                    Failed(
                        reason=Reason(code=ReasonCode.QUERY_FAILED, message=message),
                        suggested_actions=(
                            Suggestion(message="Inspect the current state before deciding again."),
                        ),
                    ),
                    None,
                )
            ),
            None,
        )

    def _bind(self, location: native.Location) -> ExternalRun:
        if location.run_id is None:
            raise GameReadError("There is no current run.")
        identity = (location.game_instance_id, location.run_id)
        current = self._run
        if current is not None and current.identity == identity:
            return current
        execution = Execution(
            LiveGame(
                self._client,
                namespace=uuid4().hex,
                game_instance_id=identity[0],
                run_id=identity[1],
                wait=self._wait,
                on_status=self._diagnostics.native_status
                if self._diagnostics is not None
                else None,
            ),
            game_instance_id=identity[0],
            run_id=identity[1],
            diagnostics=self._diagnostics,
            display=self._client.display if self._display_reasons else None,
        )
        current = ExternalRun(
            identity,
            execution,
            ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=Notes(""),
                query_view=QueryView.PUBLIC,
                notes_write_guard=self._notes_write_guard,
            ),
            ToolCatalog(
                native.DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                query_view=QueryView.PUBLIC,
            ),
        )
        self._run = current
        return current

    async def _call(self, name: ToolName, arguments: str) -> ExternalReply:
        status = await self._status()
        current = self._bind(status.location)
        tools = current.run_tools if name.value.startswith("run_") else current.combat_tools
        call = ToolCall("external", name.value, arguments)
        if name not in (ToolName.RUN_ACT, ToolName.COMBAT_ACT):
            result = await tools.dispatch(call)
            if result.observation is not None and current.identity != (
                result.observation.location.game_instance_id,
                result.observation.location.run_id,
            ):
                self._bind(result.observation.location)
                return reject(
                    ReasonCode.REFERENCE_EXPIRED, "The current run changed.", result.observation
                )
            return _reply(result)
        observation = await current.execution.observe()
        refused = self._write_problem(status, observation)
        if refused is not None:
            return _reply(refused)
        owner = (
            native.DecisionOwner.RUN if name is ToolName.RUN_ACT else native.DecisionOwner.COMBAT
        )
        location = observation.location
        if location.owner is not owner:
            return reject(
                ReasonCode.WRONG_ROLE, "This action tool belongs to the other role.", observation
            )
        assert location.run_id is not None and location.node_id is not None
        task = ActTask(
            native.RouteKey(
                game_instance_id=location.game_instance_id,
                run_id=location.run_id,
                node_id=location.node_id,
                owner=owner,
            )
        )
        proposal = await tools.dispatch_action(call, task, observation)
        if isinstance(proposal, QueryToolResult):
            return _reply(proposal)
        source = _SubmittedDecision(proposal)
        unit = await current.execution.run_unit(source, task)
        if source.feedback is not None:
            return _reply(source.feedback)
        if isinstance(unit, RouteChanged):
            return reject(
                ReasonCode.REFERENCE_EXPIRED, "The current decision changed.", unit.observation
            )
        if isinstance(unit, UnitRejected):
            return reject(unit.reason.code, unit.reason.message, await current.execution.observe())
        raise GameReadError("The action could not be admitted.")

    def _write_problem(
        self, status: native.Status, observation: native.Observation
    ) -> QueryToolResult | None:
        if status.control_intent is native.ControlIntent.PAUSED:
            return _refusal(
                ReasonCode.CONTROL_UNAVAILABLE,
                "The game is paused. Use in-game Start first.",
                observation,
            )
        if (
            self._last_exposed != observation.location
            or status.location != observation.location
            or self._run is None
            or self._run.identity
            != (observation.location.game_instance_id, observation.location.run_id)
        ):
            return _refusal(
                ReasonCode.REFERENCE_EXPIRED,
                "The decision changed or has not been exposed.",
                observation,
            )
        return None

    async def _notes_write_guard(
        self, observation: native.Observation | None
    ) -> QueryToolResult | None:
        status = await self._status()
        refreshed = observation is None or status.location != observation.location
        if refreshed:
            state = await self._client.state()
            observation = state.observation if isinstance(state, native.StateCompleted) else None
        if observation is None:
            return QueryToolResult(
                Failed(
                    reason=Reason(
                        code=ReasonCode.QUERY_FAILED, message="The current game could not be read."
                    ),
                    suggested_actions=(
                        Suggestion(message="Inspect the current state before deciding again."),
                    ),
                ),
                None,
            )
        if refreshed:
            return _refusal(
                ReasonCode.REFERENCE_EXPIRED,
                "Inspect the refreshed state before editing notes.",
                observation,
            )
        return self._write_problem(status, observation)


def _reply(result: QueryToolResult | ActionToolResult) -> ExternalReply:
    return ExternalReply(
        render(result), result.observation.location if result.observation else None
    )


def reject(
    code: ReasonCode, message: str, observation: native.Observation | None = None
) -> ExternalReply:
    return _reply(_refusal(code, message, observation))


def _refusal(
    code: ReasonCode, message: str, observation: native.Observation | None
) -> QueryToolResult:
    return QueryToolResult(
        Rejected(
            reason=Reason(code=code, message=message),
            suggested_actions=(
                Suggestion(message="Inspect the current state before deciding again."),
            ),
        ),
        observation,
    )
