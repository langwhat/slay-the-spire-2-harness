"""Bound one live sample around the recorded dependencies of a run session."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from sts2_harness.client import models as native
from sts2_harness.game import (
    Admitted,
    Cataloged,
    GameIO,
    GameReply,
    Inspected,
    Observed,
    Resumed,
    SessionStopped,
    Settlement,
)
from sts2_harness.journal import ArchiveError, Journal
from sts2_harness.llm.messages import ModelClient, ModelInput, ModelReply, ToolDefinition
from sts2_harness.results import Reason
from sts2_harness.session import ModelFactory, RunSession
from sts2_harness_eval.results import Rejected


@dataclass(frozen=True, slots=True)
class SampleBudget:
    requests: int
    seconds: float


@dataclass(frozen=True, slots=True)
class AfterDecisions:
    count: int


@dataclass(frozen=True, slots=True)
class BattleEnd:
    node_id: str


type Endpoint = AfterDecisions | BattleEnd


class StopCause(StrEnum):
    REQUEST_BUDGET = "request_budget"
    TIME_BUDGET = "time_budget"
    CORE_PAUSED = "core_paused"
    NATIVE_UNCONFIRMED = "native_unconfirmed"
    RECORDING_UNAVAILABLE = "recording_unavailable"
    RUN_ENDED = "run_ended"


@dataclass(frozen=True, slots=True)
class Completed:
    endpoint: Endpoint
    decisions: int
    requests: int


@dataclass(frozen=True, slots=True)
class Incomplete:
    cause: StopCause
    decisions: int
    requests: int


@dataclass(frozen=True, slots=True)
class Unsupported:
    rejection: Rejected


type SampleResult = Completed | Incomplete | Unsupported

# Evaluation assembly opts in; ordinary LiveGame sessions keep their unbounded default.
SETTLE_CONFIRMATION_SECONDS = 30.0


class _SampleStopped(SessionStopped):
    pass


class SampleHost:
    """Wrap recorded live IO, then explicitly arm before releasing the live branch."""

    def __init__(self, budget: SampleBudget, endpoint: Endpoint, journal: Journal) -> None:
        self._budget = budget
        self._endpoint = endpoint
        self._journal = journal
        self._armed = False
        self._started = False
        self._decisions = 0
        self._requests = 0
        self._unit_model = False
        self._unit_settle = False
        self._deadline: float | None = None
        self._budget_cause: StopCause | None = None
        self._native_unconfirmed = False
        self._pausing = False
        self._core_paused = False
        self._admitting = False
        self._last: native.Observation | None = None
        self._battle: native.Location | None = None
        self._recording_unavailable = False
        self._terminal_candidate = False
        self._terminal = False

    def game(self, recorded: GameIO) -> GameIO:
        host = self

        class Game:
            async def admit(self) -> GameReply[Admitted]:
                host._close_unit()
                host._gate()
                host._admitting = True
                try:
                    reply = await recorded.admit()
                    host._remember(reply.observations)
                    return reply
                finally:
                    host._admitting = False

            async def observe(self) -> GameReply[Observed]:
                host._require_armed()
                reply = await recorded.observe()
                host._remember(reply.observations)
                return reply

            async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
                host._require_armed()
                reply = await recorded.inspect(query)
                host._remember(reply.observations)
                return reply

            async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
                host._require_armed()
                reply = await recorded.catalog(request)
                host._remember(reply.observations)
                return reply

            async def settle(
                self, route: native.RouteKey, step: native.NativeStep
            ) -> GameReply[Settlement]:
                host._require_armed()
                host._unit_settle = True
                reply = await recorded.settle(route, step)
                host._remember(reply.observations)
                if not isinstance(reply.outcome, Settlement):
                    return reply
                settlement = reply.outcome
                receipt = settlement.receipt
                if receipt is None or isinstance(receipt, native.ReceiptUnknown):
                    host._native_unconfirmed = True
                    return reply
                if (
                    isinstance(receipt, native.ReceiptCompleted)
                    and receipt.boundary is native.InputReadiness.TERMINAL
                    and receipt.operation_id == settlement.request.operation_id
                    and settlement.request.expected_route == route
                    and native.Location.model_validate(route.model_dump()) == host._battle
                ):
                    host._terminal_candidate = True
                return reply

            async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
                host._require_armed()
                host._close_unit()
                host._pausing = True
                try:
                    reply = await recorded.pause_until_resumed(reason)
                    host._remember(reply.observations)
                    return reply
                finally:
                    host._pausing = False

        return Game()

    def models(self, recorded: ModelFactory) -> ModelFactory:
        host = self

        def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
            client = recorded(role, stream)

            class Model:
                async def complete(
                    self, input: ModelInput, tools: tuple[ToolDefinition, ...]
                ) -> ModelReply:
                    host._gate()
                    host._requests += 1
                    host._unit_model = True
                    host._check_budget()
                    deadline = asyncio.timeout_at(host._deadline)
                    try:
                        async with deadline:
                            return await client.complete(input, tools)
                    except TimeoutError:
                        if not deadline.expired():
                            raise
                        host._budget_cause = StopCause.TIME_BUDGET
                        raise _SampleStopped from None

            return Model()

        return factory

    def arm(self, current: native.Observation | None = None) -> None:
        if self._armed:
            raise RuntimeError("A sample can only be armed once.")
        self._armed = True
        self._unit_model = False
        self._unit_settle = False
        self._deadline = asyncio.get_running_loop().time() + self._budget.seconds
        if current is not None:
            self._remember((current,))

    def _require_armed(self) -> None:
        if not self._armed:
            raise RuntimeError("Arm the sample before calling a live dependency.")

    def _close_unit(self) -> None:
        # Admission, Pause and session return occur after the core's in-memory acceptance.
        if self._unit_model and self._unit_settle:
            self._decisions += 1
        self._unit_model = False
        self._unit_settle = False
        self._terminal = self._terminal or self._terminal_candidate
        self._terminal_candidate = False

    def _reached(self) -> bool:
        if isinstance(self._endpoint, AfterDecisions):
            return self._decisions >= self._endpoint.count
        if self._terminal:
            return True
        if self._battle is None or self._last is None:
            return False
        location = self._last.location
        return (
            location.game_instance_id == self._battle.game_instance_id
            and location.run_id == self._battle.run_id
            and (
                self._last.state.phase is native.Phase.TERMINAL
                or location.node_id != self._battle.node_id
                or location.owner is not native.DecisionOwner.COMBAT
            )
        )

    def _remember(self, observations: tuple[native.Observation, ...]) -> None:
        for observation in observations:
            if isinstance(self._endpoint, BattleEnd) and self._battle is None:
                if (
                    observation.location.owner is not native.DecisionOwner.COMBAT
                    or observation.location.node_id != self._endpoint.node_id
                    or not isinstance(observation.state, native.CombatState)
                ):
                    raise ValueError("The battle endpoint requires its opening combat observation.")
                self._battle = observation.location
            self._last = observation

    def _gate(self) -> None:
        self._require_armed()
        self._check_budget()
        self._check_recording()
        if (
            self._reached()
            or self._budget_cause is not None
            or self._native_unconfirmed
            or self._recording_unavailable
        ):
            raise _SampleStopped

    def _check_recording(self) -> None:
        try:
            if self._journal.gaps or self._journal.availability.error is not None:
                self._recording_unavailable = True
        except ArchiveError:
            self._recording_unavailable = True

    def _check_budget(self) -> None:
        if self._budget_cause is not None:
            return
        if self._deadline is not None and asyncio.get_running_loop().time() >= self._deadline:
            self._budget_cause = StopCause.TIME_BUDGET
        elif self._requests >= self._budget.requests:
            self._budget_cause = StopCause.REQUEST_BUDGET

    def stop_requested(self) -> bool:
        if self._admitting:
            self._check_budget()
            return self._budget_cause is StopCause.TIME_BUDGET
        return self._core_paused

    def native_status(self, status: native.Status, waiting: bool) -> None:
        if self._pausing and waiting and status.control_intent is native.ControlIntent.PAUSED:
            self._core_paused = True

    async def run(self, session: RunSession) -> SampleResult:
        if self._started:
            raise RuntimeError("A sample can only run one session coroutine.")
        self._started = True
        try:
            await session.run()
        except _SampleStopped:
            pass
        except SessionStopped:
            if not self._core_paused and self._budget_cause is None:
                raise
        self._require_armed()
        self._close_unit()
        self._check_budget()
        self._check_recording()
        if self._native_unconfirmed:
            return Incomplete(StopCause.NATIVE_UNCONFIRMED, self._decisions, self._requests)
        if self._recording_unavailable:
            return Incomplete(StopCause.RECORDING_UNAVAILABLE, self._decisions, self._requests)
        if self._core_paused:
            return Incomplete(StopCause.CORE_PAUSED, self._decisions, self._requests)
        if self._reached():
            return Completed(self._endpoint, self._decisions, self._requests)
        return Incomplete(
            self._budget_cause or StopCause.RUN_ENDED, self._decisions, self._requests
        )
