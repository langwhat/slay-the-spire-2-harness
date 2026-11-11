import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign, run_state
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import (
    ControlWriteError,
    LiveGame,
    OperationKind,
    Settlement,
    Stopped,
)
from sts2_harness.journal import Journal, Manifest, RecordedGame, SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval.sample import (
    AfterDecisions,
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


def event_state(node: str) -> native.Observation:
    observation = run_state(1, node)
    return observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "screen": native.RunScreen.EVENT,
                    "actions": (
                        native.Action(ref="choose", label="Choose", enabled=True),
                        native.Action(ref="advance", label="Continue", enabled=True),
                    ),
                }
            )
        }
    )


def test_one_decision_keeps_settlement_and_feedback_before_stopping(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        async with sample(
            tmp_path / "sample.sqlite", campaign, SampleBudget(10, 10), AfterDecisions(1)
        ) as (host, journal, session):
            host.arm()
            assert await host.run(session) == Completed(AfterDecisions(1), 1, 2)
            assert len(campaign.actions) == 1 and len(campaign.inputs) == 2
            assert len(journal.operations(kind=OperationKind.SETTLE)) == 1
            assert journal.operations()[-1].request.kind is OperationKind.OBSERVE
            assert journal.requests()[0].delivered

    asyncio.run(scenario())


def test_request_limit_does_not_split_a_two_step_model_batch(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = Campaign()
        campaign.states[0] = event_state("first")
        campaign.states.insert(1, event_state("second"))
        campaign.responses = iter(
            [
                reply(
                    (
                        "batch",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"choose"},'
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


class FailedActions(Campaign):
    def __init__(self, *, fail_control: bool) -> None:
        super().__init__()
        self.states[0] = event_state("start")
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
                    "presentation": None,
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
            content=reply(
                ("choose", "run_act", '{"steps":[{"kind":"interact","action":"choose"}]}')
            ),
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
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": "busy",
                    "receipt": {"operation_id": self.actions[-1].operation_id, "status": "pending"},
                },
            )
        return super().mod(request)


def test_pending_confirmation_expires_without_resubmit_or_batch_tail(tmp_path: Path) -> None:
    async def scenario() -> None:
        campaign = PendingCampaign()
        campaign.states[0] = event_state("first")
        campaign.responses = iter(
            [
                reply(
                    (
                        "batch",
                        "run_act",
                        '{"steps":[{"kind":"interact","action":"choose"},'
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
