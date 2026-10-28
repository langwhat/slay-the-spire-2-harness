import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign, battle_state, run_state, terminal
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.execution import Execution, ToolRecoveryBudget
from sts2_harness.game import (
    ControlWriteError,
    GameIO,
    LiveGame,
    OperationKind,
    RunEnded,
    RunEndReason,
    SessionStopped,
    Settlement,
    Stopped,
    unwrap,
)
from sts2_harness.journal import Journal, Manifest, RecordedGame, SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient, TextMessage, TextRole
from sts2_harness.loop import CombatFactory, RunFlow
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.notes import Notes
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.run import RunRole
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval.sample import (
    AfterDecisions,
    BattleEnd,
    Completed,
    Endpoint,
    Incomplete,
    SampleBudget,
    SampleHost,
    StopCause,
)


@asynccontextmanager
async def sample(
    path: Path,
    campaign: Campaign,
    budget: SampleBudget,
    endpoint: Endpoint,
    *,
    model_transport: httpx.AsyncBaseTransport | None = None,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    confirmation_seconds: float | None = None,
    operator_stop: Callable[[], bool] | None = None,
) -> AsyncGenerator[tuple[SampleHost, Journal, RunSession]]:
    manifest = Manifest(
        materials=SessionMaterials(
            entry_documents_enabled=False, act_guidance={}, battle_guidance=()
        ),
        recording_id="sample",
        core_version="test",
        game_instance_id="game",
        run_id="run",
        initial_notes="",
    )
    journal = Journal.create(path, manifest)
    try:
        async with (
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)) as client,
            Llm(
                configuration(path.parent),
                transport=model_transport or httpx.MockTransport(campaign.model),
            ) as model,
        ):
            host = SampleHost(budget, endpoint, journal)

            def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
                return RecordedLlm(model, journal, role=role, stream=stream)

            game = RecordedGame(
                LiveGame(
                    client,
                    namespace=manifest.recording_id,
                    audit=journal,
                    game_instance_id=manifest.game_instance_id,
                    run_id=manifest.run_id,
                    wait_for_start=True,
                    stop_requested=operator_stop or host.stop_requested,
                    on_status=host.native_status,
                    wait=wait,
                    settle_confirmation_seconds=confirmation_seconds,
                ),
                journal,
            )
            dependencies = SessionDependencies(
                host.game(game),
                RecordedPrompts(PackagedPrompts(), journal),
                RecordedDocuments(FileDocuments(), journal),
                host.models(factory),
            )
            async with RunSession(manifest, dependencies) as session:
                yield host, journal, session
    finally:
        journal.close()


def test_one_decision_keeps_settlement_and_feedback_before_stopping(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        async with sample(
            tmp_path / "sample.sqlite", campaign, SampleBudget(10, 10), AfterDecisions(1)
        ) as (host, journal, session):
            host.arm()
            assert await host.run(session) == Completed(AfterDecisions(1), 1, 1)
            assert len(campaign.actions) == len(campaign.inputs) == 1
            assert len(journal.operations(kind=OperationKind.SETTLE)) == 1
            assert journal.operations()[-1].request.kind is OperationKind.OBSERVE
            assert journal.requests()[0].delivered

    asyncio.run(scenario())


class TargetFlow:
    def __init__(
        self, game: GameIO, model: ModelClient, current: native.Observation, *, observe: bool = True
    ) -> None:
        self.game = game
        self.model = model
        self.current = current
        self.observe = observe
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def run(self) -> RunEnded:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        await self.model.complete((TextMessage(TextRole.USER, "Synthetic target request."),), ())
        unwrap(
            await self.game.settle(
                native.RouteKey.model_validate(self.current.location.model_dump()),
                native.EndTurn(kind=native.StepKind.END_TURN),
            )
        )
        observation = unwrap(await self.game.observe()).observation if self.observe else None
        return RunEnded(RunEndReason.TERMINAL, observation)


@pytest.mark.parametrize("ending", ["observed", "terminal", "foreign_run", "foreign_node"])
def test_explicit_arm_releases_one_target_request_in_the_same_session_coroutine(
    tmp_path: Path,
    ending: str,
) -> None:
    async def scenario() -> None:
        campaign = BattleCampaign() if ending == "observed" else TerminalReceipt()
        manifest = Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="target",
            core_version="test",
            game_instance_id="game",
            run_id="run",
            initial_notes="",
        )
        journal = Journal.create(tmp_path / "target.sqlite", manifest)
        try:
            async with (
                ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)) as client,
                Llm(
                    configuration(tmp_path), transport=httpx.MockTransport(campaign.model)
                ) as model,
            ):
                endpoint = AfterDecisions(1) if ending == "observed" else BattleEnd("battle")
                host = SampleHost(SampleBudget(1, 10), endpoint, journal)
                game = host.game(
                    RecordedGame(
                        LiveGame(
                            client,
                            namespace="target",
                            audit=journal,
                            game_instance_id="game",
                            run_id="run",
                        ),
                        journal,
                    )
                )

                def models(role: native.DecisionOwner, stream: str) -> ModelClient:
                    return RecordedLlm(model, journal, role=role, stream=stream)

                factory = host.models(models)
                target_observation = campaign.current
                if ending in ("foreign_run", "foreign_node"):
                    field = "run_id" if ending == "foreign_run" else "node_id"
                    target_observation = campaign.current.model_copy(
                        update={
                            "location": campaign.current.location.model_copy(
                                update={field: "other"}
                            )
                        }
                    )
                target = TargetFlow(
                    game,
                    factory(native.DecisionOwner.COMBAT, "combat:1"),
                    target_observation,
                    observe=ending == "observed",
                )

                def flow(
                    execution: Execution,
                    recovery: ToolRecoveryBudget,
                    notes: Notes,
                    run: RunRole,
                    combat: CombatFactory,
                ) -> RunFlow:
                    return target

                dependencies = SessionDependencies(
                    game, PackagedPrompts(), FileDocuments(), factory
                )
                async with RunSession(manifest, dependencies, flow_factory=flow) as session:
                    task = asyncio.create_task(host.run(session))
                    await asyncio.wait_for(target.entered.wait(), 1)
                    with pytest.raises(RuntimeError, match="Arm the sample"):
                        await game.observe()
                    assert not journal.requests() and not campaign.actions
                    host.arm(campaign.current)
                    with pytest.raises(RuntimeError, match="only be armed once"):
                        host.arm(campaign.current)
                    target.release.set()
                    expected = (
                        Incomplete(StopCause.REQUEST_BUDGET, 1, 1)
                        if ending in ("foreign_run", "foreign_node")
                        else Completed(endpoint, 1, 1)
                    )
                    assert await task == expected
                    with pytest.raises(RuntimeError, match="only run one"):
                        await host.run(session)
                    assert target.calls == 1
                    assert journal.requests()[0].delivered
        finally:
            journal.close()

    asyncio.run(scenario())


class StorageCampaign(Campaign):
    def __init__(self, directory: Path, *, recover: bool) -> None:
        super().__init__()
        self.directory = directory
        self.relocated = directory.with_name("relocated")
        self.recover = recover
        self.broken = False

    def restore(self) -> None:
        if self.broken:
            self.directory.unlink()
            self.relocated.rename(self.directory)
            self.broken = False

    def mod(self, request: httpx.Request) -> httpx.Response:
        if self.broken and self.recover and request.url.path == "/state":
            self.restore()
        response = super().mod(request)
        if request.url.path == "/action" and len(self.actions) == 1:
            self.directory.rename(self.relocated)
            self.directory.write_text("Synthetic unavailable storage.")
            self.broken = True
        return response


@pytest.mark.parametrize("recover", [False, True])
def test_recording_failure_prevents_completion_even_after_storage_recovers(
    tmp_path: Path, recover: bool
) -> None:
    async def scenario() -> None:
        directory = tmp_path / "recording"
        campaign = StorageCampaign(directory, recover=recover)
        try:
            async with sample(
                directory / "sample.sqlite", campaign, SampleBudget(10, 10), AfterDecisions(1)
            ) as (host, journal, session):
                host.arm()
                assert await host.run(session) == Incomplete(StopCause.RECORDING_UNAVAILABLE, 1, 1)
                assert journal.gaps
                assert (journal.availability.error is None) is recover
                assert len(campaign.actions) == 1
        finally:
            campaign.restore()

    asyncio.run(scenario())


def test_request_limit_does_not_split_a_two_step_model_batch(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        campaign.states.insert(1, campaign.states[0])
        campaign.responses = iter(
            [
                reply(
                    (
                        "batch",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"travel"},'
                        '{"kind":"interact","action":"advance"}]}',
                    )
                )
            ]
        )
        async with sample(
            tmp_path / "batch.sqlite", campaign, SampleBudget(1, 10), AfterDecisions(1)
        ) as (host, journal, session):
            host.arm()
            assert await host.run(session) == Completed(AfterDecisions(1), 1, 1)
            assert len(campaign.actions) == 2 and len(campaign.inputs) == 1
            assert len(journal.operations(kind=OperationKind.SETTLE)) == 2
            assert journal.operations()[-1].request.kind is OperationKind.OBSERVE

    asyncio.run(scenario())


def automatic(node: str) -> native.Observation:
    observation = run_state(1, node)
    return observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "actions": (
                        native.Action(
                            ref="next",
                            label="Continue",
                            enabled=True,
                            operation=native.ActionOperation.PROCEED,
                        ),
                    ),
                }
            )
        }
    )


def test_automatic_units_do_not_consume_the_decision_count(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        campaign.states = [run_state(1, "first"), automatic("auto"), run_state(1, "last")]
        campaign.states.append(terminal(campaign.states[-1]))
        campaign.responses = iter(
            [
                reply(("first", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
                reply(("last", "run_act", '{"steps":[{"kind":"interact","action":"advance"}]}')),
            ]
        )
        async with sample(
            tmp_path / "auto.sqlite", campaign, SampleBudget(10, 10), AfterDecisions(2)
        ) as (host, _journal, session):
            host.arm()
            assert await host.run(session) == Completed(AfterDecisions(2), 2, 2)
            assert len(campaign.actions) == 3 and len(campaign.inputs) == 2

    asyncio.run(scenario())


def test_automatic_only_progress_still_stops_at_the_time_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        campaign.states = [automatic(f"auto-{index}") for index in range(100)]
        async with sample(
            tmp_path / "auto-time.sqlite", campaign, SampleBudget(10, 0.05), AfterDecisions(1)
        ) as (host, _journal, session):
            host.arm()
            assert await asyncio.wait_for(host.run(session), 1) == Incomplete(
                StopCause.TIME_BUDGET, 0, 0
            )
            assert campaign.actions and not campaign.inputs

    asyncio.run(scenario())


class BattleCampaign(Campaign):
    def __init__(self, *, final_read_fails: bool = False, changed_run: bool = False) -> None:
        super().__init__()
        self.states = [battle_state(1, "battle"), run_state(1, "reward")]
        if changed_run:
            end = terminal(self.states[0])
            self.states[1] = end.model_copy(
                update={
                    "location": end.location.model_copy(update={"run_id": "another"}),
                }
            )
        self.final_read_fails = final_read_fails
        self.responses = iter([reply(("act", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))])

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state" and self.actions and self.final_read_fails:
            self.final_read_fails = False
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {
                        "code": "read_unavailable",
                        "message": "Synthetic missing final read.",
                    },
                },
            )
        return super().mod(request)


class TerminalReceipt(BattleCampaign):
    def __init__(self, receipt: str = "completed") -> None:
        super().__init__()
        self.receipt = receipt

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state" and self.actions:
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {
                        "code": "read_unavailable",
                        "message": "Synthetic final read unavailable.",
                    },
                },
            )
        response = super().mod(request)
        if request.url.path != "/action":
            return response
        operation = self.actions[-1].operation_id
        if self.receipt == "completed":
            return httpx.Response(
                200,
                json={
                    "operation_id": operation,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )
        code = "execution_uncertain" if self.receipt == "unknown" else "dispatch_failed"
        return httpx.Response(
            200,
            json={
                "operation_id": operation,
                "status": self.receipt,
                "error": {"code": code, "message": "Synthetic nonterminal result."},
            },
        )


@pytest.mark.parametrize("receipt", ["completed", "failed", "unknown"])
def test_terminal_receipt_survives_failed_final_observation_at_the_closed_unit(
    tmp_path: Path, receipt: str
) -> None:
    async def scenario() -> None:
        campaign = TerminalReceipt(receipt)
        async with sample(
            tmp_path / "terminal.sqlite", campaign, SampleBudget(1, 10), BattleEnd("battle")
        ) as (host, journal, session):
            host.arm()
            cause = (
                StopCause.NATIVE_UNCONFIRMED if receipt == "unknown" else StopCause.REQUEST_BUDGET
            )
            expected = (
                Completed(BattleEnd("battle"), 1, 1)
                if receipt == "completed"
                else Incomplete(cause, 1, 1)
            )
            assert await host.run(session) == expected
            assert len(campaign.inputs) == len(campaign.actions) == 1
            (record,) = journal.operations(kind=OperationKind.SETTLE)
            assert record.reply is not None and isinstance(record.reply.outcome, Settlement)
            assert record.reply.outcome.receipt is not None
            assert record.reply.outcome.receipt.status.value == receipt
            assert journal.operations()[-1].request.kind is OperationKind.OBSERVE
            assert journal.operations()[-1].reply is not None
            assert not journal.gaps

    asyncio.run(scenario())


@pytest.mark.parametrize("final_read_fails", [False, True])
def test_battle_end_stops_before_optional_review_even_if_exit_is_seen_at_next_admit(
    tmp_path: Path, final_read_fails: bool
) -> None:
    async def scenario() -> None:
        campaign = BattleCampaign(final_read_fails=final_read_fails)
        async with sample(
            tmp_path / "battle.sqlite", campaign, SampleBudget(10, 10), BattleEnd("battle")
        ) as (host, journal, session):
            host.arm()
            assert await host.run(session) == Completed(BattleEnd("battle"), 1, 1)
            assert len(campaign.inputs) == len(campaign.actions) == 1
            assert journal.requests()[0].role is native.DecisionOwner.COMBAT

    asyncio.run(scenario())


def test_run_change_does_not_complete_a_battle_endpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with sample(
            tmp_path / "changed.sqlite",
            BattleCampaign(changed_run=True),
            SampleBudget(10, 10),
            BattleEnd("battle"),
        ) as (host, _journal, session):
            host.arm()
            assert await host.run(session) == Incomplete(StopCause.RUN_ENDED, 1, 1)

    asyncio.run(scenario())


class FailedActions(Campaign):
    def __init__(self, *, fail_control: bool) -> None:
        super().__init__()
        self.fail_control = fail_control
        self.paused = False
        self.controls = 0

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            self.controls += 1
            if self.fail_control:
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {
                            "code": "control_unavailable",
                            "message": "Synthetic Pause failure.",
                        },
                    },
                )
            self.paused = True
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status" and self.paused:
            return httpx.Response(
                200,
                json={
                    "location": self.current.location.model_dump(mode="json"),
                    "control_intent": "paused",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        if request.url.path == "/action":
            self.actions.append(native.StepRequest.model_validate_json(request.content))
            return httpx.Response(
                200,
                json={
                    "operation_id": self.actions[-1].operation_id,
                    "status": "failed",
                    "error": {"code": "dispatch_failed", "message": "Synthetic action failure."},
                },
            )
        return super().mod(request)

    def model(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append({})
        return httpx.Response(
            200,
            content=reply(("move", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            headers={"Content-Type": "text/event-stream"},
        )


@pytest.mark.parametrize("fail_control", [False, True])
def test_core_pause_is_recorded_before_stopping_and_outranks_endpoint(
    tmp_path: Path, fail_control: bool
) -> None:
    async def scenario() -> None:
        campaign = FailedActions(fail_control=fail_control)

        async def wait(seconds: float) -> None:
            await asyncio.sleep(0)

        async with sample(
            tmp_path / "pause.sqlite", campaign, SampleBudget(4, 10), AfterDecisions(4), wait=wait
        ) as (host, journal, session):
            host.arm()
            if fail_control:
                with pytest.raises(ControlWriteError):
                    await host.run(session)
            else:
                assert await asyncio.wait_for(host.run(session), 1) == Incomplete(
                    StopCause.CORE_PAUSED, 4, 4
                )
            assert campaign.controls == 1 and len(campaign.actions) == 4
            (record,) = journal.operations(kind=OperationKind.PAUSE)
            assert record.attempts[0].endpoint.value == "control"
            assert record.reply is not None
            if not fail_control:
                assert isinstance(record.reply.outcome, Stopped)

    asyncio.run(scenario())


@pytest.mark.parametrize("requests", [1, 10])
def test_request_budget_counts_logical_requests_and_closes_the_last_unit(
    tmp_path: Path, requests: int
) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        async with sample(
            tmp_path / "sample.sqlite", campaign, SampleBudget(requests, 10), AfterDecisions(2)
        ) as (host, journal, session):
            host.arm()
            expected = (
                Incomplete(StopCause.REQUEST_BUDGET, 1, 1)
                if requests == 1
                else Completed(AfterDecisions(2), 2, 2)
            )
            assert await host.run(session) == expected
            assert len(campaign.actions) == len(campaign.inputs) == expected.decisions
            assert len(journal.requests()) == expected.requests

    asyncio.run(scenario())


@pytest.mark.parametrize("requests", [10, 1])
def test_model_deadline_keeps_an_unfinished_record_and_external_cancel_propagates(
    tmp_path: Path,
    requests: int,
) -> None:
    async def scenario(cancel: bool) -> None:
        entered = asyncio.Event()

        async def waiting(request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Future[None]()
            raise AssertionError("The model should be cancelled.")

        async with sample(
            tmp_path / f"{cancel}.sqlite",
            Campaign(),
            SampleBudget(requests, 10 if cancel else 0.2),
            AfterDecisions(1),
            model_transport=httpx.MockTransport(waiting),
        ) as (host, journal, session):
            host.arm()
            task = asyncio.create_task(host.run(session))
            await asyncio.wait_for(entered.wait(), 1)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await asyncio.wait_for(task, 1) == Incomplete(StopCause.TIME_BUDGET, 0, 1)
            (record,) = journal.requests()
            assert not record.delivered and record.failure is None
            assert journal.gaps == ()

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_external_operator_stop_propagates_with_its_recorded_stopped_admission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with sample(
            tmp_path / "stopped.sqlite",
            Campaign(),
            SampleBudget(10, 10),
            AfterDecisions(1),
            operator_stop=lambda: True,
        ) as (host, journal, session):
            host.arm()
            with pytest.raises(SessionStopped):
                await host.run(session)
            (record,) = journal.operations()
            assert record.reply is not None and isinstance(record.reply.outcome, Stopped)

    asyncio.run(scenario())


def test_unreadable_gap_index_is_incomplete_before_new_live_io(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        async with sample(
            tmp_path / "unreadable.sqlite", campaign, SampleBudget(10, 10), AfterDecisions(1)
        ) as (host, journal, session):
            relocated = journal.path.with_suffix(".relocated")
            journal.path.rename(relocated)
            try:
                host.arm()
                assert await host.run(session) == Incomplete(StopCause.RECORDING_UNAVAILABLE, 0, 0)
                assert not campaign.inputs and not campaign.actions
            finally:
                relocated.rename(journal.path)

    asyncio.run(scenario())


class PendingCampaign(Campaign):
    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/action":
            self.actions.append(native.StepRequest.model_validate_json(request.content))
            return httpx.Response(
                200, json={"operation_id": self.actions[-1].operation_id, "status": "pending"}
            )
        if request.url.path == "/status" and self.actions:
            return httpx.Response(
                200,
                json={
                    "location": self.current.location.model_dump(mode="json"),
                    "control_intent": "running",
                    "input_readiness": "busy",
                    "receipt": {"operation_id": self.actions[-1].operation_id, "status": "pending"},
                },
            )
        return super().mod(request)


def test_pending_confirmation_expires_without_resubmit_or_batch_tail(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = PendingCampaign()
        campaign.responses = iter(
            [
                reply(
                    (
                        "batch",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"travel"},'
                        '{"kind":"interact","action":"advance"}]}',
                    )
                ),
            ]
        )

        async def wait(seconds: float) -> None:
            await asyncio.sleep(0.02)

        async with sample(
            tmp_path / "pending.sqlite",
            campaign,
            SampleBudget(1, 10),
            AfterDecisions(1),
            confirmation_seconds=0.01,
            wait=wait,
        ) as (host, journal, session):
            host.arm()
            assert await asyncio.wait_for(host.run(session), 1) == Incomplete(
                StopCause.NATIVE_UNCONFIRMED, 1, 1
            )
            assert len(campaign.actions) == 1
            (record,) = journal.operations(kind=OperationKind.SETTLE)
            assert record.reply is not None and isinstance(record.reply.outcome, Settlement)
            assert record.reply.outcome.receipt is None
            assert journal.operations()[-1].request.kind is OperationKind.OBSERVE

    asyncio.run(scenario())


class WaitingAdmission(Campaign):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.polls = 0
        self.states[0] = self.current.model_copy(
            update={
                "input_readiness": native.InputReadiness.BUSY,
                "state": self.current.state.model_copy(
                    update={"control": native.StateControl.BUSY}
                ),
            }
        )

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            self.polls += 1
            return httpx.Response(
                200,
                json={
                    "location": self.current.location.model_dump(mode="json"),
                    "control_intent": "paused" if self.mode == "paused" else "running",
                    "input_readiness": "ready"
                    if self.mode == "fresh" and self.polls == 1
                    else "busy",
                    "receipt": None,
                },
            )
        return super().mod(request)


@pytest.mark.parametrize("mode", ["busy", "paused", "fresh"])
def test_admission_waits_observe_time_budget_without_a_model_request(
    tmp_path: Path, mode: str
) -> None:
    async def scenario() -> None:
        campaign = WaitingAdmission(mode)

        async def wait(seconds: float) -> None:
            await asyncio.sleep(0.03)

        async with sample(
            tmp_path / "wait.sqlite", campaign, SampleBudget(10, 0.02), AfterDecisions(1), wait=wait
        ) as (host, journal, session):
            host.arm()
            assert await asyncio.wait_for(host.run(session), 1) == Incomplete(
                StopCause.TIME_BUDGET, 0, 0
            )
            assert campaign.polls > 0 and not campaign.inputs and not campaign.actions
            (record,) = journal.operations()
            assert record.reply is not None and isinstance(record.reply.outcome, Stopped)

    asyncio.run(scenario())
