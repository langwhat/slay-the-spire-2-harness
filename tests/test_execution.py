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


@pytest.mark.parametrize("mode", ("rejected", "lost_completed", "lost_pending"))
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
            BattleResult,
            BoundaryValues,
            ReviewTask,
            TaskComplete,
        )
        from sts2_harness.execution import Rejected, RouteChanged

        observation = native.Observation.model_validate_json(RUN)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        report = BattleReport(
            location=BattleLocation(act=1, floor=1, room_type="monster"),
            result=BattleResult.WON,
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


@pytest.mark.parametrize("busy_observation", [False, True])
def test_admission_reports_stalled_native_input(
    monkeypatch: pytest.MonkeyPatch, busy_observation: bool
) -> None:
    from sts2_harness import game

    monkeypatch.setattr(game, "_INPUT_READINESS_SECONDS", 0.01)

    async def scenario() -> None:
        observation = native.Observation.model_validate_json(COMBAT)
        data = observation.model_dump(mode="json", exclude_unset=True)
        data["input_readiness"] = "busy"
        data["state"]["control"] = "busy"
        status_reads = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal status_reads
            if request.url.path == "/state" and busy_observation:
                return httpx.Response(200, json={"status": "completed", "observation": data})
            assert request.url.path == "/status"
            status_reads += 1
            return httpx.Response(
                200,
                json={
                    "location": data["location"],
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": (
                        "selection" if busy_observation and status_reads == 1 else "busy"
                    ),
                    "receipt": None,
                },
            )

        async def wait(seconds: float) -> None:
            await asyncio.sleep(0.02)

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            live = LiveGame(
                client, namespace="test", game_instance_id="game", run_id="run", wait=wait
            )
            result = await asyncio.wait_for(live.admit(), 0.3)
        assert isinstance(result.outcome, game.ReadFailure)
        assert "native input" in result.outcome.message.lower()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "step",
    [
        native.Select(kind=native.StepKind.SELECT, refs=("c2",)),
        native.Interact(kind=native.StepKind.INTERACT, action="open-selector"),
    ],
)
def test_live_selection_confirmation_expires_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch, step: native.NativeStep
) -> None:
    from sts2_harness import game

    monkeypatch.setattr(game, "_SETTLE_CONFIRMATION_SECONDS", 0.01)

    async def scenario() -> None:
        observation = native.Observation.model_validate_json(
            RUN if isinstance(step, native.Interact) else COMBAT
        )
        requests: list[native.StepRequest] = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/action":
                requests.append(native.StepRequest.model_validate_json(request.content))
                return httpx.Response(200, json={"operation_id": "test:1", "status": "pending"})
            assert request.url.path == "/status"
            return httpx.Response(
                200,
                json={
                    "location": observation.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": "busy",
                    "receipt": {"operation_id": "test:1", "status": "pending"},
                },
            )

        async def wait(seconds: float) -> None:
            await asyncio.sleep(0.02)

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            live = LiveGame(
                client, namespace="test", game_instance_id="game", run_id="run", wait=wait
            )
            route = native.RouteKey.model_validate(observation.location.model_dump())
            reply = await asyncio.wait_for(live.settle(route, step), 0.3)
        assert isinstance(reply.outcome, game.Settlement)
        assert reply.outcome.receipt is None
        assert [request.step for request in requests] == [step]

    asyncio.run(scenario())


@pytest.mark.parametrize(("native_ready_at", "expected_confirmation_at"), [(20, 20), (35, 30)])
def test_live_selection_waits_for_a_matching_receipt_up_to_the_confirmation_deadline(
    monkeypatch: pytest.MonkeyPatch, native_ready_at: int, expected_confirmation_at: int
) -> None:
    from sts2_harness import game

    async def scenario() -> None:
        observation = native.Observation.model_validate_json(COMBAT)
        route = native.RouteKey.model_validate(observation.location.model_dump())
        step = native.Select(kind=native.StepKind.SELECT, refs=("c2",))
        requests: list[native.StepRequest] = []
        polls = 0

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/action":
                requests.append(native.StepRequest.model_validate_json(request.content))
                return httpx.Response(200, json={"operation_id": "test:1", "status": "pending"})
            assert request.url.path == "/status"
            ready = polls >= native_ready_at * 10
            receipt = (
                {"operation_id": "test:1", "status": "completed", "boundary": "ready"}
                if ready
                else {"operation_id": "test:1", "status": "pending"}
            )
            return httpx.Response(
                200,
                json={
                    "location": observation.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": "ready" if ready else "busy",
                    "receipt": receipt,
                },
            )

        async def wait(seconds: float) -> None:
            nonlocal polls
            assert seconds == 0.1
            polls += 1

        with monkeypatch.context() as clock:
            clock.setattr(asyncio.get_running_loop(), "time", lambda: polls / 10)
            async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
                live = LiveGame(
                    client, namespace="test", game_instance_id="game", run_id="run", wait=wait
                )
                reply = await live.settle(route, step)
        assert isinstance(reply.outcome, game.Settlement)
        assert requests == [reply.outcome.request]
        assert reply.outcome.request.step == step
        assert polls == expected_confirmation_at * 10
        if native_ready_at <= expected_confirmation_at:
            receipt = reply.outcome.receipt
            assert isinstance(receipt, native.ReceiptCompleted)
            assert receipt.operation_id == reply.outcome.request.operation_id
            assert receipt.boundary is native.InputReadiness.READY
        else:
            assert reply.outcome.receipt is None

    asyncio.run(scenario())


def test_start_returns_to_pause_while_native_input_is_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sts2_harness import game

    monkeypatch.setattr(game, "_INPUT_READINESS_SECONDS", 0.01)

    async def scenario() -> None:
        observation = native.Observation.model_validate_json(COMBAT)
        selectable = observation.model_dump(mode="json", exclude_unset=True)
        busy = observation.model_dump(mode="json", exclude_unset=True)
        busy["input_readiness"] = "busy"
        busy["state"]["control"] = "busy"
        pauses = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal pauses
            if request.url.path == "/control" and request.method == "POST":
                pauses += 1
                return httpx.Response(200, json={"mode": "paused"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": selectable["location"],
                        "presentation": None,
                        "control_intent": "running",
                        "input_readiness": "busy" if pauses == 1 else "selection",
                        "receipt": None,
                    },
                )
            assert request.url.path == "/state"
            return httpx.Response(
                200,
                json={"status": "completed", "observation": busy if pauses == 1 else selectable},
            )

        async def wait(seconds: float) -> None:
            await asyncio.sleep(0.02)

        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            live = LiveGame(
                client, namespace="test", game_instance_id="game", run_id="run", wait=wait
            )
            result = await asyncio.wait_for(live.pause_until_resumed(None), 0.3)
        assert isinstance(result.outcome, game.Resumed)
        assert result.outcome.observation == observation
        assert pauses == 2

    asyncio.run(scenario())
