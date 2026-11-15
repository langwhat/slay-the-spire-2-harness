import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import BattleHTTP, reply
from test_responses import configuration
from test_run import RunHTTP, handoff

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActTask, CombatHandoff, HandoffTask, TaskComplete
from sts2_harness.execution import Executed, Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.llm.client import Llm
from sts2_harness.notes import Notes
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
                    "map": {"bosses": [], "nodes": [], "legal_destinations": None},
                },
            },
        )


def test_inspection_and_final_action_execute_without_an_intermediate_request(
    tmp_path: Path,
) -> None:
    battle = InspectBattle(
        [
            reply(
                ("inspect", "combat_inspect", '{"area":"map"}'),
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


def test_failed_query_keeps_read_only_calls_and_aborts_game_action(
    tmp_path: Path,
) -> None:
    battle = InspectBattle(
        [
            reply(
                ("completed-query", "combat_inspect", '{"area":"map"}'),
                ("bad", "combat_skill", '{"mode":"read","id":"missing"}'),
                ("cancelled-query", "combat_inspect", '{"area":"map"}'),
                ("cancelled-action", "combat_act", '{"steps":[{"kind":"end_turn"}]}'),
            ),
            reply(("retry", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Reviewed."),
        ]
    )
    assert isinstance(asyncio.run(battle.run(tmp_path)), CombatHandoff)
    assert [query.area for query in battle.queries] == [
        native.InspectArea.MAP,
        native.InspectArea.MAP,
    ]
    assert len(battle.actions) == 1
    assert battle.model_budget[:2] == [0, 0]
    items = battle.inputs[1]["input"]
    assert isinstance(items, list)
    results = items[-5:-1]
    assert [item["call_id"] for item in results if isinstance(item, dict)] == [
        "completed-query",
        "bad",
        "cancelled-query",
        "cancelled-action",
    ]
    query_result = results[2]
    assert isinstance(query_result, dict)
    query_output = query_result["output"]
    assert isinstance(query_output, str)
    assert json.loads(query_output)["outcome"]["status"] == "completed"
    for item in results[3:]:
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
                ("before", "combat_inspect", '{"area":"map"}'),
                ("nonfinal", action_name, '{"steps":[{"kind":"end_turn"}]}'),
                ("after", "combat_inspect", '{"area":"map"}'),
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
                    '{"reason":"Rest.","stretches":["s2"]}',
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
                '{"position":"map:1:0","room_type":"restsite","children":[]},'
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
