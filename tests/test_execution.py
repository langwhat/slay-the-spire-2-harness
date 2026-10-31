import asyncio

import httpx2 as httpx
import pytest
from test_mod_client import COMBAT, RUN

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    Decision,
    DecisionTask,
    SelectStep,
)
from sts2_harness.execution import Executed, Execution, ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.results import ActionCompleted, ActionToolResult


class Source:
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.observations: list[native.Observation] = []
        self.outcomes: list[ActionToolResult] = []

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        self.observations.append(observation)
        return self.decision

    async def accept(self, outcome: ActionToolResult) -> None:
        self.outcomes.append(outcome)


def test_anticipated_selection_finishes_and_accepts_the_complete_unit() -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(COMBAT)
        state_reads = 0
        initial = observation.model_copy(
            update={
                "input_readiness": native.InputReadiness.READY,
                "state": observation.state.model_copy(
                    update={
                        "selection": None,
                        "screen": native.CombatScreen.COMBAT,
                    }
                ),
            }
        )
        submitted: list[str] = []
        routes: list[native.RouteKey] = []

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal state_reads
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": observation.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "input_readiness": "ready",
                        "receipt": None,
                    },
                )
            if request.url.path == "/state":
                state_reads += 1
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED,
                        observation=initial if state_reads == 1 else observation,
                    ).to_json(),
                )
            assert request.url.path == "/action"
            body = native.StepRequest.model_validate_json(request.content)
            submitted.append(body.step.kind)
            routes.append(body.expected_route)
            return httpx.Response(
                200,
                json={
                    "operation_id": body.operation_id,
                    "status": "completed",
                    "boundary": "selection" if len(submitted) == 1 else "ready",
                },
            )

        source = Source(
            ActionDecision(
                ActionProposal(
                    (
                        ActionStep(ActionKind.PLAY_CARD, "c1", "e1"),
                        SelectStep(("c2",)),
                    )
                )
            )
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=False,
                ),
                game_instance_id="game",
                run_id="run",
            )
            route = native.RouteKey(
                game_instance_id="game",
                run_id="run",
                node_id="node",
                owner=native.DecisionOwner.COMBAT,
            )
            result = await execution.run_unit(source, ActTask(route))
        assert isinstance(result, Executed)
        assert result.outcome.outcome == ActionCompleted(completed_steps=2)
        assert result.outcome.observation == observation
        assert source.outcomes == [result.outcome]
        assert submitted == ["play_card", "select"]
        assert routes == [route, route]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode",
    (
        "rejected",
        "route_changed",
        "dispatch_failed",
        "identity_unregistered",
        "invalid_request",
        "lost_completed",
        "lost_pending",
        "lost_missing",
        "wrong_receipt",
    ),
)
def test_partial_failure_and_lost_responses_never_resubmit_or_execute_the_tail(mode: str) -> None:
    async def scenario() -> None:
        from sts2_harness.results import ActionStopped, ActionUnknown

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        submitted: list[native.StepRequest] = []
        polls = 0
        budget = ToolRecoveryBudget()

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal polls
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                    },
                )
            if request.url.path == "/status":
                receipt: native.StepReceipt | None = None
                if len(submitted) == 2:
                    polls += 1
                    operation = submitted[-1].operation_id
                    if mode == "lost_pending" and polls == 1:
                        receipt = native.ReceiptPending(
                            operation_id=operation, status=native.ResultStatus.PENDING
                        )
                    elif mode in ("lost_pending", "lost_completed"):
                        receipt = native.ReceiptCompleted(
                            operation_id=operation,
                            status=native.ResultStatus.COMPLETED,
                            boundary=native.InputReadiness.READY,
                        )
                status = native.Status(
                    presentation=None,
                    location=observation.location,
                    control_intent=native.ControlIntent.RUNNING,
                    input_readiness=native.InputReadiness.BUSY
                    if isinstance(receipt, native.ReceiptPending)
                    else native.InputReadiness.READY,
                    receipt=receipt,
                )
                return httpx.Response(200, content=status.to_json())
            assert request.url.path == "/action"
            body = native.StepRequest.model_validate_json(request.content)
            submitted.append(body)
            if len(submitted) == 2:
                if mode in ("dispatch_failed", "identity_unregistered"):
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": body.operation_id,
                            "status": "failed",
                            "error": {
                                "code": mode,
                                "message": "Native dispatch was not performed.",
                            },
                        },
                    )
                if mode == "route_changed":
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": body.operation_id,
                            "status": "rejected",
                            "error": {
                                "code": "route_changed",
                                "message": "The game moved to another node.",
                                "actual_location": observation.location.model_copy(
                                    update={"node_id": "next-node"}
                                ).model_dump(mode="json"),
                            },
                        },
                    )
                if mode == "invalid_request":
                    return httpx.Response(
                        400,
                        json={
                            "status": "rejected",
                            "error": {
                                "code": "invalid_request",
                                "message": "Invalid action arguments.",
                                "fields": ["step"],
                            },
                        },
                    )
                if mode == "rejected":
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": body.operation_id,
                            "status": "rejected",
                            "error": {
                                "code": "action_unavailable",
                                "message": "The current operation is no longer available.",
                            },
                        },
                    )
                if mode == "wrong_receipt":
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": "different",
                            "status": "failed",
                            "error": {
                                "code": "dispatch_failed",
                                "message": "An unrelated operation failed.",
                            },
                        },
                    )
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(
                200,
                json={
                    "operation_id": body.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        source = Source(
            ActionDecision(
                ActionProposal(
                    tuple(
                        ActionStep(ActionKind.INTERACT, ref) for ref in ("first", "second", "tail")
                    )
                )
            )
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            result = await execution.run_unit(source, ActTask(route))
        assert isinstance(result, Executed)
        outcome = result.outcome.outcome
        if mode in (
            "rejected",
            "route_changed",
            "dispatch_failed",
            "identity_unregistered",
            "invalid_request",
        ):
            assert isinstance(outcome, ActionStopped)
            assert (outcome.completed_steps, outcome.next_step) == (1, 2)
            assert len(submitted) == 2
        elif mode in ("lost_missing", "wrong_receipt"):
            assert isinstance(outcome, ActionUnknown)
            assert (outcome.confirmed_steps, outcome.uncertain_step) == (1, 2)
            assert len(submitted) == 2
        else:
            assert outcome == ActionCompleted(completed_steps=3)
            assert len(submitted) == 3
        assert len({body.operation_id for body in submitted}) == len(submitted)
        assert source.outcomes == [result.outcome]
        assert all(body.expected_route == route for body in submitted)
        assert budget.failures == (
            1
            if mode
            in (
                "dispatch_failed",
                "identity_unregistered",
                "invalid_request",
                "lost_missing",
                "wrong_receipt",
            )
            else 0
        )

    asyncio.run(scenario())


def test_route_and_task_permissions_are_checked_before_actions() -> None:
    async def scenario() -> None:
        from sts2_harness.decisions import (
            BattleLocation,
            BattleReport,
            BoundaryValues,
            ReviewTask,
            TaskComplete,
        )
        from sts2_harness.execution import Rejected, RouteChanged

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        report = BattleReport(
            location=BattleLocation(act=1, floor=1, room_type="monster"),
            result=native.TerminalResult.VICTORY,
            last_observed_turn=1,
            before=BoundaryValues(hp=10, max_hp=10, gold=0),
            after=BoundaryValues(hp=8, max_hp=10, gold=1),
        )
        posted: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            posted.append(request.url.path) if request.method == "POST" else None
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": observation.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "input_readiness": observation.input_readiness,
                        "receipt": None,
                    },
                )
            assert request.url.path == "/state"
            return httpx.Response(
                200,
                json={"status": "completed", "observation": observation.model_dump(mode="json")},
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=False,
                ),
                game_instance_id="game",
                run_id="run",
            )
            source = Source(ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "x"),))))
            mismatch = route.model_copy(update={"node_id": "old"})
            assert isinstance(await execution.run_unit(source, ActTask(mismatch)), RouteChanged)
            assert source.observations == []
            assert isinstance(await execution.run_unit(source, ReviewTask(report)), Rejected)
            assert len(source.outcomes) == 1
            source = Source(TaskComplete())
            with pytest.raises(TypeError, match="action task requires an action proposal"):
                await execution.run_unit(source, ActTask(route))
            assert await execution.run_unit(source, ReviewTask(report)) == TaskComplete()
            for proposal in (
                ActionProposal(()),
                ActionProposal(
                    (
                        ActionStep(ActionKind.INTERACT, "valid"),
                        ActionStep(ActionKind.END_TURN, "invalid"),
                    )
                ),
                ActionProposal((ActionStep(ActionKind.PLAY_CARD, "card"),)),
            ):
                source = Source(ActionDecision(proposal))
                assert isinstance(await execution.run_unit(source, ActTask(route)), Rejected)
                assert len(source.outcomes) == 1
            observation = native.Observation.model_validate_json(COMBAT)
            observation = observation.model_copy(
                update={
                    "location": observation.location.model_copy(update={"node_id": "next-room"})
                }
            )
            source = Source(TaskComplete())
            assert await execution.run_unit(source, ReviewTask(report)) == TaskComplete()
            assert source.observations == [observation]
            observation = observation.model_copy(
                update={"location": observation.location.model_copy(update={"run_id": "other"})}
            )
            source = Source(TaskComplete())
            assert isinstance(await execution.run_unit(source, ReviewTask(report)), RouteChanged)
            assert source.observations == []
            observation = native.Observation.model_validate_json(RUN)
            terminal = native.TerminalState(
                phase=native.Phase.TERMINAL,
                screen=native.TerminalScreen.RUN_COMPLETE,
                control=native.StateControl.UNAVAILABLE,
                progress=observation.state.progress,
                player=native.Player.model_validate(observation.state.player.model_dump()),
                relics=(),
                potions=(),
                actions=(),
                selection=None,
                result=native.TerminalResult.VICTORY,
                deck=(),
            )
            observation = observation.model_copy(
                update={
                    "state": terminal,
                    "input_readiness": native.InputReadiness.TERMINAL,
                    "location": observation.location.model_copy(update={"owner": None}),
                }
            )
            assert isinstance(await execution.run_unit(source, ReviewTask(report)), RouteChanged)
            assert source.observations == []
        assert posted == []

    asyncio.run(scenario())


def test_pause_waits_for_the_next_unit_and_explicit_start_after_programmatic_pause() -> None:
    async def scenario() -> None:
        from sts2_harness.execution import Rejected, ToolRecoveryBudget
        from sts2_harness.results import Reason, ReasonCode

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        mode = native.ControlIntent.RUNNING
        start = asyncio.Event()
        waiting = asyncio.Event()
        deciding = asyncio.Event()
        finish_deciding = asyncio.Event()
        steps: list[native.StepRequest] = []
        controls: list[str] = []

        async def wait(seconds: float) -> None:
            waiting.set()
            await start.wait()

        class PausingSource(Source):
            async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
                deciding.set()
                await finish_deciding.wait()
                return await super().decide(task, observation)

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal mode
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": observation.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": mode,
                        "input_readiness": "ready",
                        "receipt": None,
                    },
                )
            if request.url.path == "/control":
                body = native.ControlRequest.model_validate_json(request.content)
                mode = body.mode
                controls.append(mode)
                return httpx.Response(200, json={"mode": mode})
            if request.url.path == "/action":
                body = native.StepRequest.model_validate_json(request.content)
                steps.append(body)
                return httpx.Response(
                    200,
                    json={
                        "operation_id": body.operation_id,
                        "status": "completed",
                        "boundary": "ready",
                    },
                )
            assert request.url.path == "/state"
            return httpx.Response(
                200,
                json={"status": "completed", "observation": observation.model_dump(mode="json")},
            )

        proposal = ActionDecision(
            ActionProposal((ActionStep(ActionKind.INTERACT, "x"), SelectStep(())))
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            budget = ToolRecoveryBudget()
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                    wait=wait,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            source = PausingSource(proposal)
            admitted = asyncio.create_task(execution.run_unit(source, ActTask(route)))
            await deciding.wait()
            mode = native.ControlIntent.PAUSED
            finish_deciding.set()
            result = await admitted
            assert isinstance(result, Executed)
            assert len(steps) == 2 and len(source.outcomes) == 1
            assert budget.record_failure() is False
            following = Source(proposal)
            next_unit = asyncio.create_task(execution.run_unit(following, ActTask(route)))
            await waiting.wait()
            assert following.observations == []
            assert budget.failures == 1
            mode = native.ControlIntent.RUNNING
            start.set()
            assert isinstance(await next_unit, Executed)
            assert budget.failures == 0
            assert controls == []
            mode = native.ControlIntent.PAUSED
            external = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=False,
                    wait=wait,
                ),
                game_instance_id="game",
                run_id="run",
            )
            assert isinstance(await external.run_unit(following, ActTask(route)), Rejected)
            budget.record_failure()
            start.clear()
            waiting.clear()
            paused = asyncio.create_task(
                execution.pause_until_resumed(
                    Reason(code=ReasonCode.QUERY_FAILED, message="A required read failed.")
                )
            )
            await waiting.wait()
            assert controls == ["paused"]
            assert not paused.done() and budget.failures == 1
            mode = native.ControlIntent.RUNNING
            start.set()
            await paused
            assert budget.failures == 0
            assert await execution.observe() == observation

    asyncio.run(scenario())


def test_shared_budget_counts_failures_once_after_feedback_and_preserves_read_success() -> None:
    async def scenario() -> None:
        from sts2_harness.execution import DecisionInterrupted, Interrupted, ToolRecoveryBudget
        from sts2_harness.results import Reason, ReasonCode

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        budget = ToolRecoveryBudget()
        mode = "unknown"
        state_reads = 0

        class AccountingSource(Source):
            async def accept(self, outcome: ActionToolResult) -> None:
                assert budget.failures == len(self.outcomes)
                await super().accept(outcome)

        class InterruptedSource(Source):
            async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
                pause = budget.record_failure()
                raise DecisionInterrupted(
                    Reason(code=ReasonCode.QUERY_FAILED, message="An instance detail read failed."),
                    pause,
                )

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal state_reads
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": observation.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "input_readiness": "ready",
                        "receipt": None,
                    },
                )
            if request.url.path == "/state":
                state_reads += 1
                if mode == "final_read_no_run" and state_reads % 2 == 0:
                    return httpx.Response(
                        200,
                        json={
                            "status": "failed",
                            "error": {"code": "no_run", "message": "There is no active run."},
                        },
                    )
                if mode == "read_failed" or mode == "final_read_failed" and state_reads % 2 == 0:
                    raise httpx.ConnectError("disconnected", request=request)
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                    },
                )
            if request.url.path == "/inspect":
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "observation": observation.model_dump(mode="json"),
                        "data": {
                            "cards_played_this_combat": [],
                            "cards_played_this_turn": [],
                            "cards_exhausted_this_turn": [],
                        },
                    },
                )
            assert request.url.path == "/action"
            body = native.StepRequest.model_validate_json(request.content)
            if mode == "unknown":
                return httpx.Response(
                    200,
                    json={
                        "operation_id": body.operation_id,
                        "status": "unknown",
                        "error": {
                            "code": "execution_uncertain",
                            "message": "The effects could not be confirmed.",
                        },
                    },
                )
            if mode == "stopped":
                return httpx.Response(
                    200,
                    json={
                        "operation_id": body.operation_id,
                        "status": "rejected",
                        "error": {
                            "code": "invalid_refs",
                            "message": "The referenced action expired.",
                            "invalid_refs": ["old"],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "operation_id": body.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        proposal = ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "x"),)))
        source = AccountingSource(proposal)
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            for expected_pause in (False, False, False, True):
                result = await execution.run_unit(source, ActTask(route))
                assert isinstance(result, Executed) and result.pause_required is expected_pause
            assert budget.failures == 4
            await execution.observe()
            query = native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)
            await execution.inspect(query)
            assert budget.failures == 4
            mode = "stopped"
            result = await execution.run_unit(Source(proposal), ActTask(route))
            assert isinstance(result, Executed) and not result.pause_required
            assert budget.failures == 4
            mode = "completed"
            assert isinstance(await execution.run_unit(Source(proposal), ActTask(route)), Executed)
            assert budget.failures == 0
            interrupted = await execution.run_unit(InterruptedSource(proposal), ActTask(route))
            assert isinstance(interrupted, Interrupted) and not interrupted.pause_required
            assert budget.failures == 1
            mode = "final_read_failed"
            state_reads = 0
            result = await execution.run_unit(Source(proposal), ActTask(route))
            assert isinstance(result, Executed)
            assert result.outcome.outcome == ActionCompleted(completed_steps=1)
            assert result.outcome.observation is None and budget.failures == 2
            mode = "final_read_no_run"
            state_reads = 0
            source = Source(proposal)
            result = await execution.run_unit(source, ActTask(route))
            assert isinstance(result, Executed) and len(source.outcomes) == 1
            assert result.outcome.observation is None and budget.failures == 0
            mode = "read_failed"
            result = await execution.run_unit(Source(proposal), ActTask(route))
            assert isinstance(result, Interrupted) and budget.failures == 1

    asyncio.run(scenario())


def test_unconfirmed_pending_work_blocks_the_next_decision_until_native_readiness() -> None:
    async def scenario() -> None:
        from sts2_harness.results import ActionUnknown

        observation = native.Observation.model_validate_json(RUN)
        busy_observation = observation.model_copy(
            update={
                "input_readiness": native.InputReadiness.BUSY,
                "state": observation.state.model_copy(update={"control": native.StateControl.BUSY}),
            }
        )
        route = native.RouteKey.model_validate(observation.location.model_dump())
        submitted: list[native.StepRequest] = []
        fail_settlement = True
        recovered = False
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def wait(seconds: float) -> None:
            waiting.set()
            await release.wait()

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal fail_settlement
            busy = len(submitted) == 1 and not recovered
            if request.url.path == "/status":
                if busy and fail_settlement:
                    fail_settlement = False
                    raise httpx.ReadError("settlement disconnected", request=request)
                receipt = (
                    native.ReceiptPending(
                        operation_id=submitted[0].operation_id, status=native.ResultStatus.PENDING
                    )
                    if busy
                    else None
                )
                return httpx.Response(
                    200,
                    content=native.Status(
                        presentation=None,
                        location=observation.location,
                        control_intent=native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.BUSY
                        if busy
                        else native.InputReadiness.READY,
                        receipt=receipt,
                    ).to_json(),
                )
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED,
                        observation=busy_observation if busy else observation,
                    ).to_json(),
                )
            assert request.url.path == "/action"
            body = native.StepRequest.model_validate_json(request.content)
            submitted.append(body)
            return httpx.Response(
                200,
                json={"operation_id": body.operation_id, "status": "pending"}
                if len(submitted) == 1
                else {
                    "operation_id": body.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        source = Source(ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "x"),))))
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=False,
                    wait=wait,
                ),
                game_instance_id="game",
                run_id="run",
            )
            result = await execution.run_unit(source, ActTask(route))
            assert isinstance(result, Executed) and isinstance(
                result.outcome.outcome, ActionUnknown
            )
            assert result.outcome.observation == busy_observation
            following = Source(source.decision)
            pending = asyncio.create_task(execution.run_unit(following, ActTask(route)))
            await waiting.wait()
            assert following.observations == [] and len(submitted) == 1
            recovered = True
            release.set()
            assert isinstance(await pending, Executed)
            assert len(submitted) == 2 and len(source.outcomes) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ("semantic", "mismatch", "protocol", "connection", "changed_run")
)
def test_recovery_pause_reports_write_failure_or_run_end_without_reset(failure: str) -> None:
    async def scenario() -> None:
        from sts2_harness.execution import ControlWriteError, GameLifetimeEnded, ToolRecoveryBudget
        from sts2_harness.results import Reason, ReasonCode

        observation = native.Observation.model_validate_json(RUN)
        budget = ToolRecoveryBudget()
        budget.record_failure()
        calls: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/control":
                if failure == "semantic":
                    return httpx.Response(
                        200,
                        json={
                            "status": "failed",
                            "error": {
                                "code": "control_unavailable",
                                "message": "Control UI unavailable.",
                            },
                        },
                    )
                if failure == "mismatch":
                    return httpx.Response(200, json={"mode": "running"})
                if failure == "protocol":
                    return httpx.Response(200, content=b"invalid control reply")
                if failure == "connection":
                    raise httpx.ConnectError("disconnected", request=request)
                return httpx.Response(200, json={"mode": "paused"})
            assert request.url.path == "/status"
            return httpx.Response(
                200,
                content=native.Status(
                    presentation=None,
                    location=observation.location.model_copy(update={"run_id": "new-run"}),
                    control_intent=native.ControlIntent.RUNNING,
                    input_readiness=native.InputReadiness.READY,
                    receipt=None,
                ).to_json(),
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            expected_error = (
                GameLifetimeEnded if failure in ("changed_run", "connection") else ControlWriteError
            )
            with pytest.raises(expected_error):
                await execution.pause_until_resumed(
                    Reason(code=ReasonCode.QUERY_FAILED, message="A required read failed.")
                )
        assert budget.failures == 1
        assert calls == (
            ["/control", "/status"] if failure in ("changed_run", "connection") else ["/control"]
        )

    asyncio.run(scenario())


def test_fresh_observation_readiness_and_pending_settlement_precede_new_decisions() -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        ready = False
        waiting = asyncio.Event()
        release = asyncio.Event()
        settling = asyncio.Event()
        finish_settlement = asyncio.Event()
        paused = False
        operation: str | None = None

        async def wait(seconds: float) -> None:
            waiting.set()
            await release.wait()

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal operation
            if request.url.path == "/status":
                if operation is not None:
                    settling.set()
                    await finish_settlement.wait()
                return httpx.Response(
                    200,
                    json={
                        "location": observation.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "paused" if paused else "running",
                        "input_readiness": "ready",
                        "receipt": None
                        if operation is None
                        else {
                            "operation_id": operation,
                            "status": "completed",
                            "boundary": "ready",
                        },
                    },
                )
            if request.url.path == "/state":
                current = (
                    observation
                    if ready
                    else observation.model_copy(
                        update={
                            "input_readiness": native.InputReadiness.BUSY,
                            "state": observation.state.model_copy(
                                update={"control": native.StateControl.BUSY}
                            ),
                        }
                    )
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current
                    ).to_json(),
                )
            assert request.url.path == "/action"
            operation = native.StepRequest.model_validate_json(request.content).operation_id
            return httpx.Response(200, json={"operation_id": operation, "status": "pending"})

        class ReadySource(Source):
            async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
                assert observation.input_readiness is native.InputReadiness.READY
                return await super().decide(task, observation)

        source = ReadySource(
            ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "x"),)))
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=False,
                    wait=wait,
                ),
                game_instance_id="game",
                run_id="run",
            )
            pending = asyncio.create_task(execution.run_unit(source, ActTask(route)))
            waiter = asyncio.create_task(waiting.wait())
            done, _ = await asyncio.wait((pending, waiter), return_when=asyncio.FIRST_COMPLETED)
            if pending in done:
                waiter.cancel()
                await pending
                pytest.fail("A busy fresh observation must not reach the decision source.")
            assert source.observations == []
            ready = True
            paused = True
            release.set()
            await settling.wait()
            assert source.outcomes == []
            finish_settlement.set()
            result = await pending
            assert isinstance(result, Executed) and len(source.outcomes) == 1
            assert source.observations == [observation]

    asyncio.run(scenario())


@pytest.mark.parametrize("no_run_at", ("status", "state"))
def test_no_run_ends_the_old_lifetime_without_observation_or_recovery_failure(
    no_run_at: str,
) -> None:
    async def scenario() -> None:
        from sts2_harness.execution import GameLifetimeEnded

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        budget = ToolRecoveryBudget()
        budget.record_failure()
        source = Source(ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "old"),))))

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/state":
                assert no_run_at == "state"
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "no_run", "message": "There is no active run."},
                    },
                )
            assert request.url.path == "/status"
            if no_run_at == "state":
                return httpx.Response(
                    200,
                    content=native.Status(
                        presentation=None,
                        location=observation.location,
                        control_intent=native.ControlIntent.RUNNING,
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            return httpx.Response(
                200,
                content=native.Status(
                    presentation=None,
                    location=native.Location(
                        game_instance_id="game", run_id=None, node_id=None, owner=None
                    ),
                    control_intent=native.ControlIntent.PAUSED,
                    input_readiness=native.InputReadiness.UNAVAILABLE,
                    receipt=None,
                ).to_json(),
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            with pytest.raises(GameLifetimeEnded):
                await execution.run_unit(source, ActTask(route))
        assert source.observations == [] and source.outcomes == []
        assert budget.failures == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("no_run", (False, True))
def test_recovery_pause_keeps_waiting_through_status_and_started_observation_failures(
    no_run: bool,
) -> None:
    async def scenario() -> None:
        from sts2_harness.execution import GameLifetimeEnded
        from sts2_harness.results import Reason, ReasonCode

        observation = native.Observation.model_validate_json(RUN)
        budget = ToolRecoveryBudget()
        budget.record_failure()
        waits: asyncio.Queue[int] = asyncio.Queue()
        advances: asyncio.Queue[None] = asyncio.Queue()
        status_reads = 0
        state_reads = 0
        controls = 0
        wait_count = 0

        async def wait(seconds: float) -> None:
            nonlocal wait_count
            wait_count += 1
            await waits.put(wait_count)
            await advances.get()

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal status_reads, state_reads, controls
            if request.url.path == "/control":
                controls += 1
                return httpx.Response(200, json={"mode": "paused"})
            if request.url.path == "/status":
                status_reads += 1
                if status_reads == 1:
                    raise httpx.ConnectError("temporary disconnect", request=request)
                if status_reads == 2:
                    return httpx.Response(
                        500,
                        json={
                            "status": "failed",
                            "error": {
                                "code": "internal_error",
                                "message": "Status temporarily unavailable.",
                            },
                        },
                    )
                return httpx.Response(
                    200,
                    content=native.Status(
                        presentation=None,
                        location=observation.location,
                        control_intent=(
                            native.ControlIntent.PAUSED
                            if status_reads == 3
                            else native.ControlIntent.RUNNING
                        ),
                        input_readiness=native.InputReadiness.READY,
                        receipt=None,
                    ).to_json(),
                )
            assert request.url.path == "/state"
            state_reads += 1
            if state_reads == 1:
                raise httpx.ReadTimeout("temporary timeout", request=request)
            if state_reads == 2 or no_run:
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {
                            "code": "no_run" if no_run and state_reads == 3 else "read_unavailable",
                            "message": "No active run."
                            if no_run and state_reads == 3
                            else "State temporarily unavailable.",
                        },
                    },
                )
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=observation
                ).to_json(),
            )

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(
                    client,
                    namespace="test",
                    game_instance_id="game",
                    run_id="run",
                    wait_for_start=True,
                    wait=wait,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            paused = asyncio.create_task(
                execution.pause_until_resumed(
                    Reason(code=ReasonCode.QUERY_FAILED, message="A required read failed.")
                )
            )
            for expected_wait in range(1, 6):
                waiting = asyncio.create_task(waits.get())
                completed, _ = await asyncio.wait(
                    (paused, waiting), return_when=asyncio.FIRST_COMPLETED
                )
                if paused in completed:
                    waiting.cancel()
                    await paused
                    pytest.fail("Recovery returned before obtaining a current observation.")
                assert await waiting == expected_wait
                assert not paused.done() and budget.failures == 1 and controls == 1
                await advances.put(None)
            if no_run:
                with pytest.raises(GameLifetimeEnded):
                    await paused
                assert budget.failures == 1
            else:
                await paused
                assert budget.failures == 0
            assert controls == 1 and state_reads == 3

    asyncio.run(scenario())
