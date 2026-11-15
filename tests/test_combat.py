import asyncio
import json
from pathlib import Path

import httpx2 as httpx
from pydantic import JsonValue
from test_mod_client import COMBAT, RUN
from test_responses import configuration

from sts2_harness.agent import MaterialReader
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent, CombatTask
from sts2_harness.config import Protocol
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    BattleResult,
    CombatHandoff,
    Decision,
    DecisionSource,
    DecisionTask,
    TaskComplete,
)
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame, RunEnded
from sts2_harness.llm.client import Llm
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.note_values import NotesView
from sts2_harness.results import ActionToolResult
from sts2_harness.tools import ToolCatalog


def reply(*calls: tuple[str, str, str], text: str | None = None) -> bytes:
    output: list[dict[str, JsonValue]] = [
        {"type": "function_call", "call_id": id, "name": name, "arguments": arguments}
        for id, name, arguments in calls
    ]
    if text is not None:
        output.append(
            {
                "type": "message",
                "id": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": text, "annotations": []},
                ],
            }
        )
    return (
        "data: "
        + json.dumps(
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": {
                    "id": "response",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "fake",
                    "output": output,
                },
            }
        )
        + "\n\n"
    ).encode()


def test_combat_task_queries_executes_ordered_batch_and_reviews_in_same_context(
    tmp_path: Path,
) -> None:
    opening = native.Observation.model_validate_json(COMBAT)
    ending = native.Observation.model_validate_json(RUN)
    ending = ending.model_copy(
        update={
            "state": ending.state.model_copy(update={"screen": native.RunScreen.REWARDS}),
            "location": ending.location.model_copy(
                update={
                    "game_instance_id": opening.location.game_instance_id,
                    "run_id": opening.location.run_id,
                    "node_id": "after-battle",
                }
            ),
        }
    )
    path = tmp_path / "battle.txt"
    path.write_text("Synthetic battle guidance.", encoding="utf-8")
    files = FileDocuments(
        (
            DocumentSource(
                id="battle",
                name="Battle",
                description="Battle guidance",
                path=path,
            ),
        )
    )
    actions: list[native.NativeStep] = []
    requests: list[dict[str, JsonValue]] = []
    replies = iter(
        (
            reply(("inspect", "combat_inspect", '{"area":"state"}')),
            reply(("skill", "combat_skill", '{"mode":"read","id":"battle"}')),
            reply(
                (
                    "act",
                    "combat_act",
                    '{"steps":[{"kind":"play_card","card":"c1","target":"e1"},'
                    '{"kind":"select","refs":["c2"]},{"kind":"end_turn"}]}',
                )
            ),
            reply(text="Observed loss and useful preparation for the next battle."),
        )
    )

    def mod(request: httpx.Request) -> httpx.Response:
        current = ending if len(actions) == 3 else opening
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": current.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=current
                ).to_json(),
            )
        assert request.url.path == "/action"
        submitted = native.StepRequest.model_validate_json(request.content)
        actions.append(submitted.step)
        return httpx.Response(
            200,
            json={
                "operation_id": submitted.operation_id,
                "status": "completed",
                "boundary": "ready",
            },
        )

    def model(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=next(replies), headers={"Content-Type": "text/event-stream"}
        )

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(mod)) as client:
            game = LiveGame(
                client,
                namespace="battle",
                game_instance_id=opening.location.game_instance_id,
                run_id="run",
                wait_for_start=True,
            )
            execution = Execution(
                game,
                game_instance_id=opening.location.game_instance_id,
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                skills=files,
            )
            async with Llm(configuration(tmp_path), transport=httpx.MockTransport(model)) as llm:
                role = CombatAgent(
                    llm, tools, budget, opening_notes=NotesView(other="Fixed opening strategy.")
                )
                result = await CombatTask(execution, role, opening, budget).run()
        assert isinstance(result, CombatHandoff)
        assert result.review == "Observed loss and useful preparation for the next battle."
        assert result.report.result is BattleResult.WON
        assert result.report.before.hp == opening.state.player.hp
        assert result.report.after.hp == ending.state.player.hp
        assert [step.kind for step in actions] == [
            native.StepKind.PLAY_CARD,
            native.StepKind.SELECT,
            native.StepKind.END_TURN,
        ]
        assert budget.failures == 0
        assert len(requests) == 4
        tools_wire = requests[0]["tools"]
        assert isinstance(tools_wire, list)
        assert {
            item["name"]
            for item in tools_wire
            if isinstance(item, dict) and isinstance(item["name"], str)
        } == {
            "combat_act",
            "combat_inspect",
            "combat_knowledge",
            "combat_skill",
        }
        wire = requests[-1]["input"]
        assert isinstance(wire, list)
        outputs = [
            item
            for item in wire
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        assert [item["call_id"] for item in outputs] == ["inspect", "skill", "act"]
        assert isinstance(outputs[-1]["output"], str)
        assert json.loads(outputs[-1]["output"])["outcome"] == {
            "status": "completed",
            "completed_steps": 3,
        }

    asyncio.run(scenario())


class BattleHTTP:
    def __init__(self, responses: list[bytes | int], *, steps_to_end: int = 1) -> None:
        self.opening = native.Observation.model_validate_json(COMBAT)
        ending = native.Observation.model_validate_json(RUN)
        self.ending = ending.model_copy(
            update={
                "location": ending.location.model_copy(
                    update={
                        "game_instance_id": self.opening.location.game_instance_id,
                        "run_id": self.opening.location.run_id,
                        "node_id": "after-battle",
                    }
                )
            }
        )
        self.responses = iter(responses)
        self.steps_to_end = steps_to_end
        self.actions: list[native.StepRequest] = []
        self.inputs: list[dict[str, JsonValue]] = []
        self.paused = False
        self.busy = False
        self.failed_first = False
        self.unknown_first = False
        self.manual_pause_after_first = False
        self.pause_written = asyncio.Event()
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.review_read_failures = 0
        self.model_budget: list[int] = []
        self.budget = ToolRecoveryBudget()

    @property
    def current(self) -> native.Observation:
        return self.ending if len(self.actions) >= self.steps_to_end else self.opening

    async def wait(self, seconds: float) -> None:
        self.waiting.set()
        await self.release.wait()
        self.release.clear()

    def start(self) -> None:
        self.paused = False
        self.busy = False
        self.release.set()

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            self.paused = True
            self.pause_written.set()
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status":
            if len(self.actions) >= self.steps_to_end and self.review_read_failures:
                self.review_read_failures -= 1
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "location": self.current.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "paused" if self.paused else "running",
                    "input_readiness": self.current.input_readiness.value
                    if self.current.state.phase is native.Phase.TERMINAL
                    else "busy"
                    if self.busy
                    else "ready",
                    "receipt": None,
                },
            )
        if request.url.path == "/state":
            if self.current.location.run_id is None:
                return httpx.Response(
                    200,
                    json={
                        "status": "failed",
                        "error": {"code": "no_run", "message": "No active run."},
                    },
                )
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=self.current,
                ).to_json(),
            )
        assert request.url.path == "/action"
        submitted = native.StepRequest.model_validate_json(request.content)
        self.actions.append(submitted)
        if len(self.actions) == 1 and self.failed_first:
            return httpx.Response(
                200,
                json={
                    "operation_id": submitted.operation_id,
                    "status": "failed",
                    "error": {"code": "dispatch_failed", "message": "Dispatch failed."},
                },
            )
        if len(self.actions) == 1 and self.unknown_first:
            self.busy = True
            return httpx.Response(
                200,
                json={
                    "operation_id": submitted.operation_id,
                    "status": "unknown",
                    "error": {"code": "execution_uncertain", "message": "Unconfirmed."},
                },
            )
        if len(self.actions) == 1 and self.manual_pause_after_first:
            self.paused = True
        return httpx.Response(
            200,
            json={
                "operation_id": submitted.operation_id,
                "status": "completed",
                "boundary": "ready",
            },
        )

    def model(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.content))
        self.model_budget.append(self.budget.failures)
        response = next(self.responses)
        if isinstance(response, int):
            return httpx.Response(
                response, json={"error": {"message": "Unavailable", "type": "authentication_error"}}
            )
        return httpx.Response(200, content=response, headers={"Content-Type": "text/event-stream"})

    async def run(
        self,
        tmp_path: Path,
        *,
        materials: MaterialReader | None = None,
        source: DecisionSource | None = None,
        protocol: Protocol = Protocol.RESPONSES,
        minimal: bool = False,
    ) -> CombatHandoff | RunEnded:
        async with ModClient("http://mod", transport=httpx.MockTransport(self.mod)) as client:
            assert self.opening.location.run_id is not None
            game = LiveGame(
                client,
                namespace="battle",
                game_instance_id=self.opening.location.game_instance_id,
                run_id=self.opening.location.run_id,
                wait_for_start=True,
                wait=self.wait,
            )
            execution = Execution(
                game,
                game_instance_id=self.opening.location.game_instance_id,
                run_id=self.opening.location.run_id,
                recovery=self.budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                minimal=minimal,
            )
            async with Llm(
                configuration(tmp_path, LLM_PROTOCOL=protocol.value),
                transport=httpx.MockTransport(self.model),
            ) as llm:
                role = source or CombatAgent(
                    llm,
                    tools,
                    self.budget,
                    opening_notes=NotesView(other="Opening notes."),
                    materials=materials,
                )
                return await CombatTask(
                    execution, role, self.opening, self.budget, minimal=minimal
                ).run()


def test_reply_protocol_closes_all_calls_and_resets_query_recovery(tmp_path: Path) -> None:
    act = '{"steps":[{"kind":"end_turn"}]}'
    battle = BattleHTTP(
        [
            reply(("first", "combat_act", act), ("second", "combat_act", act)),
            reply(
                (
                    "invalid",
                    "combat_act",
                    '{"steps":[{"kind":"end_turn"},{"kind":"play_card","ref":"c1"}]}',
                )
            ),
            reply(text="I will act."),
            reply(("list", "combat_skill", '{"mode":"list"}')),
            reply(("action", "combat_act", act)),
            reply(("forbidden", "combat_act", act)),
            reply(text="Review complete."),
        ]
    )
    result = asyncio.run(battle.run(tmp_path))
    assert isinstance(result, CombatHandoff) and result.review == "Review complete."
    assert len(battle.actions) == 1
    assert battle.model_budget == [0, 1, 2, 3, 0, 0, 1]
    assert battle.budget.failures == 0
    wire = battle.inputs[-1]["input"]
    assert isinstance(wire, list)
    ids = [
        item["call_id"]
        for item in wire
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert ids == ["first", "second", "invalid", "list", "action", "forbidden"]


def test_review_admission_read_failure_recovers_without_losing_same_run_handoff(
    tmp_path: Path,
) -> None:
    battle = BattleHTTP(
        [
            reply(("action", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Review after a recovered read."),
        ]
    )
    battle.review_read_failures = 1
    result = asyncio.run(battle.run(tmp_path))
    assert isinstance(result, CombatHandoff) and result.review == "Review after a recovered read."
    assert len(battle.actions) == 1


class PlainCombat:
    def __init__(self) -> None:
        self.accepted: list[ActionToolResult] = []
        self.tasks: list[DecisionTask] = []

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        self.tasks.append(task)
        if isinstance(task, ActTask):
            return ActionDecision(ActionProposal((ActionStep(ActionKind.END_TURN),)))
        return TaskComplete()

    async def accept(self, outcome: ActionToolResult) -> None:
        self.accepted.append(outcome)


def test_plain_code_role_needs_no_model_and_does_not_use_new_battle_boundary(
    tmp_path: Path,
) -> None:
    battle = BattleHTTP([])
    battle.ending = battle.opening.model_copy(
        update={
            "location": battle.opening.location.model_copy(update={"node_id": "different-battle"}),
            "state": battle.opening.state.model_copy(
                update={"player": battle.opening.state.player.model_copy(update={"hp": 1})}
            ),
        }
    )
    source = PlainCombat()
    result = asyncio.run(battle.run(tmp_path, source=source))
    assert isinstance(result, CombatHandoff) and result.review is None
    assert result.report.after.hp == battle.opening.state.player.hp
    assert len(source.tasks) == 2 and len(source.accepted) == 1 and battle.inputs == []
