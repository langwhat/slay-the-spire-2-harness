import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import BattleHTTP, reply
from test_compaction_full_replay import response
from test_responses import configuration
from test_run import RunHTTP, handoff

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent
from sts2_harness.config import Protocol
from sts2_harness.decisions import ActTask, CombatHandoff, HandoffTask, ReviewTask, TaskComplete
from sts2_harness.execution import Executed, Execution
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.client import Llm
from sts2_harness.notes import Notes
from sts2_harness.results import Reason, ReasonCode
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


class InspectBattle(BattleHTTP):
    def __init__(self, responses: list[bytes | int]) -> None:
        super().__init__(responses)
        self.queries: list[native.InspectQuery] = []
        self.inspected: native.Observation | None = None

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/inspect":
            return super().mod(request)
        query = native.InspectRequest.model_validate_json(request.content).query
        self.queries.append(query)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "observation": (self.inspected or self.opening).model_dump(mode="json"),
                "data": {
                    "objects": {},
                    "descriptions": {},
                    "modifier_descriptions": {},
                    "generated": {},
                    "mechanisms": [],
                },
            },
        )


@asynccontextmanager
async def combat_role(battle: InspectBattle, tmp_path: Path) -> AsyncGenerator[CombatAgent]:
    async with ModClient("http://mod", transport=httpx.MockTransport(battle.mod)) as client:
        execution = Execution(
            LiveGame(client, namespace="queries", game_instance_id="game", run_id="run"),
            game_instance_id="game",
            run_id="run",
            recovery=battle.budget,
        )
        tools = ToolCatalog(
            native.DecisionOwner.COMBAT,
            observe=execution.observe,
            inspect=execution.inspect,
            catalog=execution.catalog,
        )
        async with Llm(
            configuration(tmp_path), transport=httpx.MockTransport(battle.model)
        ) as llm:
            yield CombatAgent(llm, tools, battle.budget, opening_notes="Opening notes.")


@pytest.mark.parametrize("minimal", [False, True])
@pytest.mark.parametrize("protocol", list(Protocol))
def test_two_inspections_return_together_before_the_next_decision(
    tmp_path: Path, minimal: bool, protocol: Protocol
) -> None:
    battle = InspectBattle(
        [
            response(
                protocol,
                ("enemies", "combat_inspect", '{"area":"enemies"}'),
                ("powers", "combat_inspect", '{"area":"powers"}'),
            ),
            response(protocol, ("act", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            response(protocol, text="Reviewed."),
        ]
    )
    material_reads: list[int] = []

    async def materials(_: native.Observation) -> tuple[DocumentView, ...]:
        material_reads.append(len(battle.queries))
        return ()

    result = asyncio.run(
        battle.run(tmp_path, minimal=minimal, protocol=protocol, materials=materials)
    )
    assert isinstance(result, CombatHandoff)
    assert [query.area for query in battle.queries] == [
        native.InspectArea.ENEMIES,
        native.InspectArea.POWERS,
    ]
    assert battle.model_budget[:2] == [0, 0]
    assert material_reads == ([0, 2] if minimal else [0, 2, 2])
    field = "input" if protocol is Protocol.RESPONSES else "messages"
    first = battle.inputs[0][field]
    following = battle.inputs[1][field]
    assert isinstance(first, list) and isinstance(following, list)
    assert following[: len(first)] == first
    if protocol is Protocol.MESSAGES:
        message = following[-2]
        assert isinstance(message, dict) and message["role"] == "user"
        outputs = message["content"]
        assert isinstance(outputs, list)
        id_field, content_field = "tool_use_id", "content"
    else:
        outputs = following[-3:-1]
        id_field, content_field = (
            ("call_id", "output")
            if protocol is Protocol.RESPONSES
            else ("tool_call_id", "content")
        )
    assert [item[id_field] for item in outputs if isinstance(item, dict)] == ["enemies", "powers"]
    for item in outputs:
        assert isinstance(item, dict) and isinstance(content := item[content_field], str)
        result = json.loads(content)
        assert result["outcome"] == {"status": "completed"}
        assert result["observation"] is None
        assert "data" in result


def test_inspection_and_final_action_execute_without_an_intermediate_request(
    tmp_path: Path,
) -> None:
    battle = InspectBattle(
        [
            reply(
                ("inspect", "combat_inspect", '{"area":"enemies"}'),
                ("act", "combat_act", '{"steps":[{"kind":"end_turn"}]}'),
            ),
            reply(text="Reviewed."),
        ]
    )
    result = asyncio.run(battle.run(tmp_path))
    assert isinstance(result, CombatHandoff)
    assert len(battle.queries) == len(battle.actions) == 1
    assert len(battle.inputs) == 2
    items = battle.inputs[1]["input"]
    assert isinstance(items, list)
    assert [item["call_id"] for item in items[-3:-1] if isinstance(item, dict)] == [
        "inspect",
        "act",
    ]


@pytest.mark.parametrize("minimal", [False, True])
def test_failed_query_cancels_remaining_queries_and_action_without_extra_recovery(
    tmp_path: Path, minimal: bool
) -> None:
    battle = InspectBattle(
        [
            reply(
                ("completed-query", "combat_inspect", '{"area":"enemies"}'),
                ("bad", "combat_skill", '{"mode":"read","id":"missing"}'),
                ("cancelled-query", "combat_inspect", '{"area":"powers"}'),
                ("cancelled-action", "combat_act", '{"steps":[{"kind":"end_turn"}]}'),
            ),
            reply(("retry", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Reviewed."),
        ]
    )
    assert isinstance(asyncio.run(battle.run(tmp_path, minimal=minimal)), CombatHandoff)
    assert [query.area for query in battle.queries] == [native.InspectArea.ENEMIES]
    assert len(battle.actions) == 1
    assert battle.model_budget[:2] == [0, 1]
    items = battle.inputs[1]["input"]
    assert isinstance(items, list)
    results = items[-5:-1]
    assert [item["call_id"] for item in results if isinstance(item, dict)] == [
        "completed-query",
        "bad",
        "cancelled-query",
        "cancelled-action",
    ]
    for item in results[2:]:
        assert isinstance(item, dict) and isinstance(content := item["output"], str)
        result = json.loads(content)
        assert result == {
            "outcome": {
                "status": "rejected",
                "reason": {
                    "code": "batch_aborted",
                    "message": "Not executed: stopped after call 2 (combat_skill).",
                },
                "suggested_actions": [
                    {
                        "message": "Decide again from the results above; "
                        "resubmit this call only if it is still needed."
                    }
                ],
            },
            "observation": None,
        }


@pytest.mark.parametrize("action_name", ["combat_act", "run_act", "run_next"])
def test_nonfinal_action_rejects_the_whole_reply_before_any_inspection(
    tmp_path: Path, action_name: str
) -> None:
    battle = InspectBattle(
        [
            reply(
                ("before", "combat_inspect", '{"area":"enemies"}'),
                ("nonfinal", action_name, '{"steps":[{"kind":"end_turn"}]}'),
                ("after", "combat_inspect", '{"area":"powers"}'),
            ),
            reply(("retry", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Reviewed."),
        ]
    )
    assert isinstance(asyncio.run(battle.run(tmp_path)), CombatHandoff)
    assert battle.queries == [] and len(battle.actions) == 1
    assert battle.model_budget[:2] == [0, 1]
    items = battle.inputs[1]["input"]
    assert isinstance(items, list)
    for item in items[-3:]:
        assert isinstance(item, dict) and isinstance(content := item["output"], str)
        assert json.loads(content)["outcome"]["reason"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("changed_role", [False, True])
def test_interrupted_query_closes_unexecuted_suffix_before_context_is_reused(
    tmp_path: Path, changed_role: bool
) -> None:
    battle = InspectBattle(
        [
            reply(
                (
                    "first",
                    "combat_inspect" if changed_role else "combat_skill",
                    '{"area":"enemies"}' if changed_role else '{"mode":"read","id":"missing"}',
                ),
                ("cancelled-query", "combat_inspect", '{"area":"powers"}'),
                ("cancelled-action", "combat_act", '{"steps":[{"kind":"end_turn"}]}'),
            ),
            reply(text="Reviewed."),
        ]
    )
    if changed_role:
        battle.inspected = battle.ending
    else:
        for _ in range(3):
            battle.budget.record_failure()

    async def scenario() -> None:
        async with combat_role(battle, tmp_path) as role:
            route = native.RouteKey.model_validate(battle.opening.location.model_dump())
            with pytest.raises(DecisionInterrupted) as error:
                await role.decide(ActTask(route), battle.opening)
            assert error.value.pause_required is not changed_role
            assert isinstance(error.value.reason, Reason)
            assert error.value.reason.code is (
                ReasonCode.ROLE_CHANGED if changed_role else ReasonCode.INVALID_ARGUMENTS
            )
            assert battle.budget.failures == (0 if changed_role else 4)
            assert isinstance(
                await role.decide(ReviewTask(handoff().report), battle.ending), TaskComplete
            )
        assert len(battle.queries) == (1 if changed_role else 0)
        assert battle.actions == []
        items = battle.inputs[1]["input"]
        assert isinstance(items, list)
        outputs = [
            item
            for item in items
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        assert [item["call_id"] for item in outputs] == [
            "first",
            "cancelled-query",
            "cancelled-action",
        ]
        for output in outputs[1:]:
            assert isinstance(content := output["output"], str)
            assert json.loads(content)["outcome"]["reason"]["code"] == "batch_aborted"

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_plan_then_next_uses_the_new_plan_in_the_same_response(
    tmp_path: Path, minimal: bool
) -> None:
    http = RunHTTP(
        [
            reply(
                (
                    "plan",
                    "run_plan",
                    '{"reason":"Rest.","edges":[{"from":"map:0:0","to":"map:1:0"}]}',
                ),
                ("next", "run_next", '{"reason":"Follow the new plan."}'),
            ),
            reply(text="Assessed."),
        ]
    )
    state = http.observation.state
    assert isinstance(state, native.RunState)
    http.observation = http.observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "progress": state.progress.model_copy(update={"position": "map:0:0"}),
                    "actions": (
                        native.Action(
                            ref="travel-rest", label="Rest", enabled=True, destination="map:1:0"
                        ),
                        native.Action(
                            ref="travel-monster",
                            label="Monster",
                            enabled=True,
                            destination="map:1:1",
                        ),
                    ),
                }
            ),
            "map": native.PublicMap.model_validate_json(
                '{"bosses":[],"nodes":['
                '{"position":"map:0:0","room_type":"ancient",'
                '"children":["map:1:0","map:1:1"]},'
                '{"position":"map:1:0","room_type":"rest","children":[]},'
                '{"position":"map:1:1","room_type":"monster","children":[]}],'
                '"legal_destinations":["map:1:0","map:1:1"]}'
            ),
        }
    )

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="plan", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=Notes(""),
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                minimal=minimal,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                role = RunAgent(lambda: llm, tools, budget)
                route = native.RouteKey.model_validate(http.observation.location.model_dump())
                assert isinstance(await execution.run_unit(role, ActTask(route)), Executed)
                assert len(http.inputs) == 1
                assert isinstance(
                    await execution.run_unit(role, HandoffTask(handoff())), TaskComplete
                )
                assert budget.failures == 0
        assert len(http.actions) == 1
        assert isinstance(http.actions[0].step, native.Interact)
        assert http.actions[0].step.action == "travel-rest"
        messages = http.inputs[1]["input"]
        assert isinstance(messages, list)
        assert [item["call_id"] for item in messages[-3:-1] if isinstance(item, dict)] == [
            "plan",
            "next",
        ]

    asyncio.run(scenario())
