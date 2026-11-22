"""Logical game/control IO; native retries and waiting have one owner."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import Field

from sts2_harness.client import ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, ClientFailureKind, InspectResult
from sts2_harness.results import Reason, ReasonCode

_INPUT_READINESS_SECONDS = 10.0
_SETTLE_CONFIRMATION_SECONDS = 30.0


class RunEndReason(StrEnum):
    TERMINAL = "terminal"
    RUN_CHANGED = "run_changed"
    NO_RUN = "no_run"


@dataclass(frozen=True, slots=True)
class RunEnded:
    reason: RunEndReason
    observation: native.Observation | None


class OperationKind(StrEnum):
    ADMIT = "admit"
    OBSERVE = "observe"
    INSPECT = "inspect"
    CATALOG = "catalog"
    SETTLE = "settle"
    PAUSE = "pause"
    PRESENT = "present"


class Admit(native.WireModel):
    kind: Literal[OperationKind.ADMIT] = OperationKind.ADMIT


class Observe(native.WireModel):
    kind: Literal[OperationKind.OBSERVE] = OperationKind.OBSERVE


class Inspect(native.WireModel):
    kind: Literal[OperationKind.INSPECT] = OperationKind.INSPECT
    query: native.InspectQuery


class Catalog(native.WireModel):
    kind: Literal[OperationKind.CATALOG] = OperationKind.CATALOG
    request: native.CatalogRequest


class Settle(native.WireModel):
    kind: Literal[OperationKind.SETTLE] = OperationKind.SETTLE
    route: native.RouteKey
    step: native.NativeStep


class Pause(native.WireModel):
    kind: Literal[OperationKind.PAUSE] = OperationKind.PAUSE
    reason: Reason | None = None


class Present(native.WireModel):
    kind: Literal[OperationKind.PRESENT] = OperationKind.PRESENT
    route: native.RouteKey
    edges: tuple[native.PresentationEdge, ...]


type GameRequest = Annotated[
    Admit | Observe | Inspect | Catalog | Settle | Pause | Present, Field(discriminator="kind")
]
type SettledReceipt = (
    native.ReceiptCompleted | native.ReceiptRejected | native.ReceiptFailed | native.ReceiptUnknown
)


class Admitted(native.WireModel):
    kind: Literal[OperationKind.ADMIT] = OperationKind.ADMIT
    allowed: bool


class Observed(native.WireModel):
    kind: Literal[OperationKind.OBSERVE] = OperationKind.OBSERVE
    observation: native.Observation


class Inspected(native.WireModel):
    kind: Literal[OperationKind.INSPECT] = OperationKind.INSPECT
    result: InspectResult


class Cataloged(native.WireModel):
    kind: Literal[OperationKind.CATALOG] = OperationKind.CATALOG
    result: CatalogResult


class Settlement(native.WireModel):
    kind: Literal[OperationKind.SETTLE] = OperationKind.SETTLE
    request: native.StepRequest
    receipt: SettledReceipt | None


class Resumed(native.WireModel):
    kind: Literal[OperationKind.PAUSE] = OperationKind.PAUSE
    observation: native.Observation


class Presented(native.WireModel):
    kind: Literal[OperationKind.PRESENT] = OperationKind.PRESENT
    request: native.PresentRequest
    receipt: (
        native.PresentationCompleted
        | native.PresentationRejected
        | native.InvalidRequestReply
        | None
    )


class FailureKind(StrEnum):
    IO = "io"
    READ = "read"
    CONTROL = "control"
    LIFETIME = "lifetime"
    STOPPED = "stopped"


class IOFailure(native.WireModel):
    kind: Literal[FailureKind.IO] = FailureKind.IO
    reason: ClientFailureKind


class ReadFailure(native.WireModel):
    kind: Literal[FailureKind.READ] = FailureKind.READ
    message: str


class ControlFailure(native.WireModel):
    kind: Literal[FailureKind.CONTROL] = FailureKind.CONTROL
    reason: Reason


class LifetimeEnded(native.WireModel):
    kind: Literal[FailureKind.LIFETIME] = FailureKind.LIFETIME
    location: native.Location | None


class Stopped(native.WireModel):
    kind: Literal[FailureKind.STOPPED] = FailureKind.STOPPED


type GameSuccess = Annotated[
    Admitted | Observed | Inspected | Cataloged | Settlement | Resumed | Presented,
    Field(discriminator="kind"),
]
type GameFailure = Annotated[
    IOFailure | ReadFailure | ControlFailure | LifetimeEnded | Stopped, Field(discriminator="kind")
]


class Endpoint(StrEnum):
    STATE = "state"
    STATUS = "status"
    INSPECT = "inspect"
    CATALOG = "catalog"
    SUBMIT = "submit"
    CONTROL = "control"
    VERIFY_PRESENTATION = "verify_presentation"
    PRESENT = "present"


type NativeReply = (
    native.Status
    | native.StateCompleted
    | native.ReadError
    | InspectResult
    | CatalogResult
    | native.StepReceipt
    | native.ControlReply
    | native.ControlFailed
    | native.PresentationPending
    | native.PresentationRejected
    | native.PresentationVerificationCompleted
    | native.PresentationCompleted
    | IOFailure
)


class NativeAttempt(native.WireModel):
    endpoint: Endpoint
    reply: NativeReply


class GameReply[T](native.WireModel):
    outcome: T | GameFailure
    observations: tuple[native.Observation, ...] = ()


class NativeAudit(Protocol):
    def submitting(self, operation_id: str) -> None: ...
    def received(self, attempt: NativeAttempt) -> None: ...


class GameIO(Protocol):
    async def admit(self) -> GameReply[Admitted]: ...
    async def observe(self) -> GameReply[Observed]: ...
    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]: ...
    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]: ...
    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]: ...
    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]: ...
    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> GameReply[Presented]: ...


class SessionStopped(Exception):
    """The backend stopped at a complete execution boundary."""


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


def unwrap[T](reply: GameReply[T]) -> T:
    outcome = reply.outcome
    if isinstance(outcome, IOFailure):
        raise ClientUnavailable(outcome.reason)
    if isinstance(outcome, ReadFailure):
        raise GameReadError(outcome.message)
    if isinstance(outcome, ControlFailure):
        raise ControlWriteError(outcome.reason)
    if isinstance(outcome, Stopped):
        raise SessionStopped
    if isinstance(outcome, LifetimeEnded):
        raise GameLifetimeEnded(outcome.location)
    return outcome


class LiveGame:
    def __init__(
        self,
        client: ModClient,
        *,
        namespace: str,
        audit: NativeAudit | None = None,
        game_instance_id: str,
        run_id: str,
        wait_for_start: bool = False,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stop_requested: Callable[[], bool] | None = None,
        on_status: Callable[[native.Status, bool], None] | None = None,
        settle_confirmation_seconds: float | None = None,
    ) -> None:
        if not namespace:
            raise ValueError("A game session namespace is required.")
        self._client = client
        self._audit = audit
        self._namespace = namespace
        self._step_number = 0
        self._presentation_number = 0
        self._game_instance_id = game_instance_id
        self._run_id = run_id
        self._wait_for_start = wait_for_start
        self._wait = wait
        self._stop_requested = stop_requested
        self._on_status = on_status
        self._settle_confirmation_seconds = (
            _SETTLE_CONFIRMATION_SECONDS
            if settle_confirmation_seconds is None
            else settle_confirmation_seconds
        )
        self._observations: list[native.Observation] = []
        self._inflight = False
        self._presentation_verifications: set[str] = set()
        self._unresolved_presentation: str | None = None

    async def admit(self) -> GameReply[Admitted]:
        return await self._run(self._admission_result)

    async def _admission_result(self) -> Admitted:
        allowed = await self._admit()
        if allowed:
            await self._fresh_observation()
        return Admitted(allowed=allowed)

    async def observe(self) -> GameReply[Observed]:
        return await self._run(self._observation_result)

    async def _observation_result(self) -> Observed:
        return Observed(observation=await self._observe())

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        return await self._run(lambda: self._inspection_result(query))

    async def _inspection_result(self, query: native.InspectQuery) -> Inspected:
        return Inspected(result=await self._inspect(query))

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        return await self._run(lambda: self._catalog_result(request))

    async def _catalog_result(self, request: native.CatalogRequest) -> Cataloged:
        return Cataloged(
            result=await self._attempt(Endpoint.CATALOG, self._client.catalog(request))
        )

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        return await self._run(lambda: self._settlement_result(route, step))

    async def _settlement_result(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> Settlement:
        if self._unresolved_presentation is not None:
            await self._ensure_presentation(await self._status())
        self._step_number += 1
        submission = native.StepRequest(
            operation_id=f"{self._namespace}:{self._step_number}", expected_route=route, step=step
        )
        if self._audit is not None:
            self._audit.submitting(submission.operation_id)
        return Settlement(request=submission, receipt=await self._settle(submission))

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        return await self._run(lambda: self._resume_result(reason))

    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> GameReply[Presented]:
        return await self._run(lambda: self._presentation_result(route, edges))

    async def _presentation_result(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> Presented:
        self._presentation_number += 1
        request = native.PresentRequest(
            operation_id=f"{self._namespace}:presentation:{self._presentation_number}",
            expected_route=route,
            edges=edges,
        )
        if self._unresolved_presentation is not None:
            return Presented(request=request, receipt=None)
        if self._audit is not None:
            self._audit.submitting(request.operation_id)
        receipt = await self._present(request)
        if receipt is None or (
            isinstance(receipt, native.PresentationCompleted)
            and receipt.gate is native.PresentationGate.LOCKED
        ):
            self._unresolved_presentation = request.operation_id
        return Presented(request=request, receipt=receipt)

    async def _resume_result(self, reason: Reason | None) -> Resumed:
        return Resumed(observation=await self._pause_until_resumed(reason))

    async def _run[T](self, operation: Callable[[], Awaitable[T]]) -> GameReply[T]:
        if self._inflight:
            raise RuntimeError("Game operations must be sequential.")
        self._inflight = True
        self._observations = []
        try:
            outcome: T | GameFailure
            try:
                outcome = await operation()
            except ClientUnavailable as error:
                outcome = IOFailure(reason=error.reason)
            except GameReadError as error:
                outcome = ReadFailure(message=str(error))
            except ControlWriteError as error:
                outcome = ControlFailure(reason=error.reason)
            except SessionStopped:
                outcome = Stopped()
            except GameLifetimeEnded as error:
                outcome = LifetimeEnded(location=error.location)
            return GameReply[T](
                outcome=outcome,
                observations=tuple(self._observations),
            )
        finally:
            self._inflight = False

    async def _attempt[T: NativeReply](self, endpoint: Endpoint, response: Awaitable[T]) -> T:
        try:
            reply = await response
        except ClientUnavailable as error:
            self._received(NativeAttempt(endpoint=endpoint, reply=IOFailure(reason=error.reason)))
            raise
        self._received(NativeAttempt(endpoint=endpoint, reply=reply))
        if isinstance(reply, native.StateCompleted):
            self._observations.append(reply.observation)
        elif (
            isinstance(
                reply, (native.InspectCompleted, native.InspectRejected, native.InspectFailed)
            )
            and reply.observation is not None
        ):
            self._observations.append(reply.observation)
        return reply

    def _received(self, attempt: NativeAttempt) -> None:
        if self._audit is not None:
            self._audit.received(attempt)

    async def _observe(self) -> native.Observation:
        result = await self._attempt(Endpoint.STATE, self._client.state())
        if isinstance(result, native.ReadError) and result.error.code is native.ErrorCode.NO_RUN:
            raise GameLifetimeEnded(None)
        if not isinstance(result, native.StateCompleted):
            raise GameReadError(result.error.message)
        return result.observation

    async def _inspect(self, query: native.InspectQuery) -> InspectResult:
        result = await self._attempt(Endpoint.INSPECT, self._client.inspect(query))
        return result

    async def _status(self, *, waiting: bool = False) -> native.Status:
        result = await self._attempt(Endpoint.STATUS, self._client.status())
        if not isinstance(result, native.Status):
            raise GameReadError(result.error.message)
        if self._on_status is not None and self._same_run(result.location):
            self._on_status(result, waiting)
        return result

    def _same_run(self, location: native.Location) -> bool:
        return (
            location.game_instance_id == self._game_instance_id and location.run_id == self._run_id
        )

    def _check_stop(self) -> None:
        if self._stop_requested is not None and self._stop_requested():
            raise SessionStopped

    async def _admit(self) -> bool:
        clock = asyncio.get_running_loop().time
        waiting_since = clock()
        while True:
            self._check_stop()
            status = await self._status(waiting=True)
            status = await self._ensure_presentation(status)
            if status.location.run_id is None:
                raise GameLifetimeEnded(status.location)
            if (
                not self._same_run(status.location)
                or status.input_readiness is native.InputReadiness.TERMINAL
            ):
                return True
            if status.control_intent is native.ControlIntent.PAUSED:
                if not self._wait_for_start:
                    return False
                waiting_since = clock()
            elif status.input_readiness in (
                native.InputReadiness.READY,
                native.InputReadiness.SELECTION,
            ):
                return True
            elif clock() - waiting_since >= _INPUT_READINESS_SECONDS:
                raise GameReadError(
                    f"Native input stayed {status.input_readiness.value} while running."
                )
            await self._wait(1.0 if status.control_intent is native.ControlIntent.PAUSED else 0.1)

    async def _fresh_observation(self) -> native.Observation:
        observation = await self._observe()
        clock = asyncio.get_running_loop().time
        deadline = clock() + _INPUT_READINESS_SECONDS
        while (
            self._same_run(observation.location)
            and observation.state.phase is not native.Phase.TERMINAL
            and observation.input_readiness
            not in (
                native.InputReadiness.READY,
                native.InputReadiness.SELECTION,
            )
        ):
            self._check_stop()
            await self._wait(0.1)
            status = await self._status()
            status = await self._ensure_presentation(status)
            if status.location.run_id is None:
                raise GameLifetimeEnded(status.location)
            if not self._same_run(status.location):
                return await self._observe()
            if status.input_readiness in (
                native.InputReadiness.BUSY,
                native.InputReadiness.UNAVAILABLE,
            ):
                if clock() >= deadline:
                    raise GameReadError(
                        f"Native input stayed {status.input_readiness.value} after admission."
                    )
                continue
            observation = await self._observe()
            if (
                clock() >= deadline
                and self._same_run(observation.location)
                and observation.state.phase is not native.Phase.TERMINAL
                and observation.input_readiness
                not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
            ):
                raise GameReadError("Native input and observation did not agree after admission.")
        return observation

    async def _ensure_presentation(
        self, status: native.Status, *, retry_verification: bool = False
    ) -> native.Status:
        presentation = status.presentation
        if self._unresolved_presentation is not None and (
            presentation is None or presentation.operation_id != self._unresolved_presentation
        ):
            raise GameReadError("The current map presentation could not be confirmed.")
        if presentation is None:
            return status
        if presentation.phase is native.PresentationPhase.RESTORED:
            self._unresolved_presentation = None
            return status
        operation_id = presentation.operation_id
        deadline = asyncio.get_running_loop().time() + 10.0
        while presentation.phase in (
            native.PresentationPhase.RUNNING,
            native.PresentationPhase.VERIFYING,
        ):
            self._check_stop()
            if asyncio.get_running_loop().time() >= deadline:
                raise GameReadError(
                    "The map presentation is still active; native input remains locked."
                )
            await self._wait(0.1)
            status = await self._status()
            if status.presentation is None or status.presentation.operation_id != operation_id:
                raise GameReadError("The current map presentation could not be confirmed.")
            presentation = status.presentation
        if presentation.phase is native.PresentationPhase.RESTORED:
            self._unresolved_presentation = None
            return status
        if operation_id in self._presentation_verifications and not retry_verification:
            raise GameReadError("Map restoration remains unconfirmed; native input is locked.")
        location = status.location
        if location.run_id is None or location.node_id is None or location.owner is None:
            raise GameReadError("The original map presentation location is unavailable.")
        self._presentation_verifications.add(operation_id)
        request = native.VerifyPresentationRequest(
            operation_id=operation_id,
            expected_route=native.RouteKey(
                game_instance_id=location.game_instance_id,
                run_id=location.run_id,
                node_id=location.node_id,
                owner=location.owner,
            ),
        )
        reply = await self._attempt(
            Endpoint.VERIFY_PRESENTATION, self._client.verify_presentation(request)
        )
        if (
            not isinstance(
                reply, (native.PresentationPending, native.PresentationVerificationCompleted)
            )
            or reply.operation_id != operation_id
        ):
            raise GameReadError("Map restoration could not be confirmed; native input is locked.")
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise GameReadError(
                    "Map restoration verification did not settle; native input is locked."
                )
            status = await self._status()
            presentation = status.presentation
            if presentation is None or presentation.operation_id != operation_id:
                raise GameReadError("The current map presentation could not be confirmed.")
            if isinstance(reply, native.PresentationVerificationCompleted) and (
                presentation.restoration is not reply.restoration
                or presentation.gate is not reply.gate
            ):
                raise GameReadError("Map restoration response and status disagree.")
            if presentation.phase is native.PresentationPhase.RESTORED:
                self._unresolved_presentation = None
                return status
            if presentation.phase is native.PresentationPhase.SETTLED_UNVERIFIED:
                raise GameReadError("Map restoration remains unconfirmed; native input is locked.")
            if presentation.phase is not native.PresentationPhase.VERIFYING:
                raise GameReadError("The current map verification could not be confirmed.")
            await self._wait(0.1)

    async def _pause_until_resumed(self, reason: Reason | None) -> native.Observation:
        """Write Pause, await explicit Start, then refresh; a dead run raises GameLifetimeEnded."""
        clock = asyncio.get_running_loop().time
        while True:
            await self._pause()
            resume_deadline: float | None = None
            while True:
                self._check_stop()
                try:
                    status = await self._status(waiting=True)
                except (ClientUnavailable, GameReadError):
                    if resume_deadline is not None and clock() >= resume_deadline:
                        break
                    await self._wait(1.0)
                    continue
                if (
                    not self._same_run(status.location)
                    or status.input_readiness is native.InputReadiness.TERMINAL
                ):
                    raise GameLifetimeEnded(status.location)
                if status.control_intent is native.ControlIntent.RUNNING:
                    if resume_deadline is None:
                        resume_deadline = clock() + _INPUT_READINESS_SECONDS
                    try:
                        await self._ensure_presentation(status, retry_verification=True)
                    except (ClientUnavailable, GameReadError):
                        break
                    try:
                        observation = await self._fresh_observation()
                    except (ClientUnavailable, GameReadError):
                        if clock() >= resume_deadline:
                            break
                        await self._wait(1.0)
                        continue
                    if (
                        not self._same_run(observation.location)
                        or observation.state.phase is native.Phase.TERMINAL
                    ):
                        raise GameLifetimeEnded(observation.location)
                    return observation
                resume_deadline = None
                await self._wait(1.0)

    async def _pause(self) -> None:
        while True:
            self._check_stop()
            try:
                result = await self._attempt(
                    Endpoint.CONTROL, self._client.control(native.ControlIntent.PAUSED)
                )
            except ClientUnavailable as error:
                if error.reason not in (ClientFailureKind.CONNECTION, ClientFailureKind.TIMEOUT):
                    raise ControlWriteError(
                        Reason(
                            code=ReasonCode.CONTROL_UNAVAILABLE,
                            message=f"Pause could not be confirmed: {error}",
                        )
                    ) from error
                while True:
                    self._check_stop()
                    await self._wait(1.0)
                    try:
                        status = await self._status()
                    except (ClientUnavailable, GameReadError):
                        continue
                    if (
                        not self._same_run(status.location)
                        or status.input_readiness is native.InputReadiness.TERMINAL
                    ):
                        raise GameLifetimeEnded(status.location) from error
                    break
                continue
            if not isinstance(result, native.ControlReply):
                raise ControlWriteError(
                    Reason(
                        code=ReasonCode.CONTROL_UNAVAILABLE,
                        message=result.error.message,
                    )
                )
            if result.mode is not native.ControlIntent.PAUSED:
                raise ControlWriteError(
                    Reason(
                        code=ReasonCode.CONTROL_UNAVAILABLE,
                        message="The Mod did not confirm the requested Pause control intent.",
                    )
                )
            break

    async def _settle(self, request: native.StepRequest) -> SettledReceipt | None:
        try:
            reply = await self._attempt(Endpoint.SUBMIT, self._client.submit(request))
        except ClientUnavailable:
            reply = None
        clock = asyncio.get_running_loop().time
        confirmation_deadline = clock() + self._settle_confirmation_seconds
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
            if clock() >= confirmation_deadline:
                return None
            try:
                status = await self._attempt(Endpoint.STATUS, self._client.status())
            except ClientUnavailable:
                return None
            if not isinstance(status, native.Status):
                return None
            if self._on_status is not None:
                self._on_status(status, False)
            receipt = status.receipt
            if receipt is None or receipt.operation_id != request.operation_id:
                return None
            if not isinstance(receipt, native.ReceiptPending):
                return receipt
            await self._wait(0.1)

    async def _present(
        self, request: native.PresentRequest
    ) -> (
        native.PresentationCompleted
        | native.PresentationRejected
        | native.InvalidRequestReply
        | None
    ):
        try:
            reply = await self._attempt(Endpoint.PRESENT, self._client.present(request))
        except ClientUnavailable:
            reply = None
        if isinstance(reply, native.InvalidRequestReply):
            return reply
        if (
            isinstance(reply, native.PresentationRejected)
            and reply.operation_id == request.operation_id
        ):
            return reply
        terminal = (
            reply
            if isinstance(reply, native.PresentationCompleted)
            and reply.operation_id == request.operation_id
            else None
        )
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            self._check_stop()
            try:
                status = await self._status()
            except (ClientUnavailable, GameReadError):
                return None
            presentation = status.presentation
            if presentation is None or presentation.operation_id != request.operation_id:
                return None
            if presentation.phase in (
                native.PresentationPhase.RESTORED,
                native.PresentationPhase.SETTLED_UNVERIFIED,
            ):
                resolved = native.PresentationCompleted.model_validate(
                    {
                        "operation_id": request.operation_id,
                        "status": native.ResultStatus.COMPLETED,
                        "drawing": presentation.drawing,
                        "restoration": presentation.restoration,
                        "gate": presentation.gate,
                    }
                )
                if terminal is not None and resolved != terminal:
                    return None
                return resolved
            await self._wait(0.1)
        return None
