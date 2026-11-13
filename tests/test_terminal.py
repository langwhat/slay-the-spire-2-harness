import asyncio
import json
from io import StringIO
from typing import Literal

import httpx2 as httpx
import pytest
from test_execution import Source
from test_external import FakeMod
from test_mod_client import COMBAT, RUN

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatTask
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    Decision,
    DecisionTask,
)
from sts2_harness.deck_role import DeckAgent
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.execution import Executed, Execution, ToolRecoveryBudget
from sts2_harness.external import ExternalBackend
from sts2_harness.external_http import create_app
from sts2_harness.game import LiveGame, RunEnded, RunEndReason
from sts2_harness.loop import DefaultFlow
from sts2_harness.notes import Notes
from sts2_harness.results import ActionCompleted, ActionToolResult
from sts2_harness.run import ActStart


class TerminalMod:
    def __init__(
        self,
        observation: native.Observation,
        final: Literal["failed", "same", "changed", "no_run"] = "failed",
    ) -> None:
        self.observation = observation
        self.final = final
        self.actions: list[native.StepRequest] = []
        self.final_reads = 0

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            pytest.fail("A confirmed terminal must end before recovery Pause.")
        if request.url.path == "/status":
            return httpx.Response(
                200,
                content=native.Status(
                    presentation=None,
                    location=self.observation.location,
                    control_intent=native.ControlIntent.RUNNING,
                    input_readiness=native.InputReadiness.READY,
                    receipt=None,
                ).to_json(),
            )
        if request.url.path == "/action":
            step = native.StepRequest.model_validate_json(request.content)
            self.actions.append(step)
            assert len(self.actions) == 1, "The batch tail must not be submitted."
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )
        assert request.url.path == "/state"
        observation = self.observation
        if self.actions:
            self.final_reads += 1
            if self.final == "failed":
                raise httpx.ReadTimeout("Synthetic terminal observation failure.")
            if self.final == "no_run":
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "no_run", "message": "No active run."},
                    },
                )
            if self.final == "changed":
                observation = observation.model_copy(
                    update={
                        "location": observation.location.model_copy(update={"run_id": "next-run"})
                    }
                )
        return httpx.Response(
            200,
            content=native.StateCompleted(
                status=native.ResultStatus.COMPLETED, observation=observation
            ).to_json(),
        )


class TerminalSource(Source):
    def deck(self, observation: native.Observation, offer: native.Offer) -> DeckAgent:
        raise AssertionError("This fixture has no card rewards.")

    def card_progress(self, progress: AutomaticProgress, result: ActionToolResult) -> None:
        raise AssertionError("This fixture has no card rewards.")

    def __init__(self, owner: native.DecisionOwner) -> None:
        step = (
            ActionStep(ActionKind.END_TURN)
            if owner is native.DecisionOwner.COMBAT
            else ActionStep(ActionKind.INTERACT, "finish")
        )
        super().__init__(ActionDecision(ActionProposal((step, step))))

    async def enter_act(self, start: ActStart) -> None:
        assert start.observation.location.owner is native.DecisionOwner.RUN

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        assert not self.observations, "No decision may follow a confirmed terminal."
        return await super().decide(task, observation)


@pytest.mark.parametrize("final", ["failed", "same", "changed", "no_run"])
def test_terminal_receipt_closes_only_the_confirmed_prefix(
    final: Literal["failed", "same", "changed", "no_run"],
) -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(RUN)
        mod = TerminalMod(observation, final)
        source = TerminalSource(native.DecisionOwner.RUN)
        console = StringIO()
        diagnostics = Diagnostics("terminal", console=console)
        async with ModClient("http://mod", transport=httpx.MockTransport(mod.respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="terminal", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                diagnostics=diagnostics,
            )
            result = await execution.run_unit(
                source, ActTask(native.RouteKey.model_validate(observation.location.model_dump()))
            )
        assert isinstance(result, Executed)
        assert result.outcome.outcome == ActionCompleted(completed_steps=1)
        assert source.outcomes == [result.outcome]
        assert result.ended is not None
        assert result.ended.reason is (
            RunEndReason.RUN_CHANGED
            if final == "changed"
            else RunEndReason.NO_RUN
            if final == "no_run"
            else RunEndReason.TERMINAL
        )
        if final in ("failed", "same"):
            assert result.ended.observation is None
        assert mod.final_reads == 1
        batches = [
            json.loads(line)
            for line in console.getvalue().splitlines()
            if json.loads(line)["event"] == "batch_settled"
        ]
        assert len(batches) == 1
        assert batches[0]["planned"] == 2 and batches[0]["confirmed"] == 1
        assert batches[0]["outcome"] == "completed"
        diagnostics.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", [native.DecisionOwner.RUN, native.DecisionOwner.COMBAT])
def test_terminal_read_failure_ends_roles_before_recovery_pause(
    owner: native.DecisionOwner,
) -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(
            COMBAT if owner is native.DecisionOwner.COMBAT else RUN
        )
        mod = TerminalMod(observation)
        source = TerminalSource(owner)
        budget = ToolRecoveryBudget()
        for _ in range(3):
            budget.record_failure()
        async with ModClient("http://mod", transport=httpx.MockTransport(mod.respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="terminal", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            if owner is native.DecisionOwner.COMBAT:
                result = await CombatTask(execution, source, observation, budget).run()
            else:
                result = await DefaultFlow(
                    execution,
                    budget,
                    Notes(""),
                    source,
                    lambda observation, notes: source,
                    new_run=lambda: source,
                    deck=source.deck,
                ).run()
        assert result == RunEnded(RunEndReason.TERMINAL, None)
        assert len(source.observations) == 1 and len(source.outcomes) == 1
        assert source.outcomes[0].observation is None
        assert source.outcomes[0].outcome == ActionCompleted(completed_steps=1)
        assert budget.failures == 4 and mod.final_reads == 1

    asyncio.run(scenario())


def test_external_terminal_returns_existing_completed_result_to_waiter() -> None:
    class TerminalExternal(FakeMod):
        async def respond(self, request: httpx.Request) -> httpx.Response:
            response = await super().respond(request)
            if request.url.path == "/action":
                step = self.actions[-1]
                assert len(self.actions) == 1
                return httpx.Response(
                    200,
                    json={
                        "operation_id": step.operation_id,
                        "status": "completed",
                        "boundary": "terminal",
                    },
                )
            return response

    async def scenario() -> None:
        mod = TerminalExternal()
        async with ExternalBackend(mod.client()) as backend:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(backend)), base_url="http://tools"
            ) as caller:
                await caller.post("/tools/run_inspect", json={"area": "state"})
                mod.control = native.ControlIntent.RUNNING
                response = await caller.post(
                    "/tools/run_act",
                    json={"steps": [{"kind": "interact", "action": "travel"}] * 2},
                )
                assert response.status_code == 200
                assert json.loads(response.content)["outcome"] == {
                    "status": "completed",
                    "completed_steps": 1,
                }
                assert len(mod.actions) == 1

    asyncio.run(scenario())
