import asyncio
from pathlib import Path

import httpx2 as httpx
from test_combat import reply
from test_responses import configuration
from test_run import RunHTTP, handoff

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import HandoffTask, TaskComplete
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.llm.client import Llm
from sts2_harness.notes import Notes
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


def test_assembled_run_request_contains_planning_contract(tmp_path: Path) -> None:
    http = RunHTTP([reply(text="Report assessed.")])

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="planning-text", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=Notes("Plan for the Boss."),
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(lambda: llm, tools, budget)
                result = await execution.run_unit(agent, HandoffTask(handoff()))
                assert isinstance(result, TaskComplete)

    asyncio.run(scenario())
    request = http.inputs[0]
    messages = request["input"]
    assert isinstance(messages, list)
    system = str(messages[0])
    assert (
        "Use run_plan to set or replace a route and run_next to enter its unique next node"
        in system
    )
    assert "context_baseline is a historical snapshot" in system
    assert "supplementary knowledge is optional" in system
    assert "Use at most one tool call per response" in system
    assert "`current.map_topology`" in system and "`current.dynamic_map`" in system
    assert (
        "Map planning results separately report plan acceptance, presentation and restoration"
        in system
    )
    assert "Use the publicly identified act Boss" in system

    tools = request["tools"]
    assert isinstance(tools, list)
    by_name = {tool["name"]: tool for tool in tools if isinstance(tool, dict)}
    assert set(by_name) == {
        "run_act",
        "run_inspect",
        "run_knowledge",
        "run_skill",
        "run_notes",
        "run_plan",
        "run_next",
    }
    plan = by_name["run_plan"]
    next_tool = by_name["run_next"]
    assert "records the plan without displaying it" in str(plan["description"])
    assert "Map destination interactions are not accepted here" in str(
        by_name["run_act"]["description"]
    )
    assert plan["parameters"] == {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
            "edges": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string", "minLength": 1},
                        "to": {"type": "string", "minLength": 1},
                    },
                    "required": ["from", "to"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reason", "edges"],
        "additionalProperties": False,
    }
    assert next_tool["parameters"] == {
        "type": "object",
        "properties": {"reason": {"type": "string", "minLength": 1}},
        "required": ["reason"],
        "additionalProperties": False,
    }
