import json

from sts2_harness.client import models as native


def test_planning_keeps_previous_route_after_rejection_and_travels_to_unique_next(
    tmp_path: object,
) -> None:
    import asyncio
    from pathlib import Path

    import httpx2 as httpx
    from test_combat import reply
    from test_responses import configuration
    from test_run import RunHTTP

    from sts2_harness.client import ModClient
    from sts2_harness.decisions import ActTask
    from sts2_harness.execution import Executed, Execution
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.game import LiveGame
    from sts2_harness.journal import Journal, Manifest, SessionMaterials
    from sts2_harness.journal_models import PlanArchiveData, ResultSnapshot
    from sts2_harness.llm.client import Llm
    from sts2_harness.model_journal import RecordedLlm
    from sts2_harness.notes import Notes
    from sts2_harness.run import RunAgent
    from sts2_harness.tools import ToolCatalog

    assert isinstance(tmp_path, Path)
    http = RunHTTP(
        [
            reply(
                (
                    "plan",
                    "run_plan",
                    '{"reason":"rest", "edges":[{"from":"map:0:0","to":"map:1:0"}]}',
                )
            ),
            reply(
                (
                    "bad",
                    "run_plan",
                    '{"reason":"ambiguous", "edges":[{"from":"map:0:0","to":"map:1:0"},'
                    '{"from":"map:0:0","to":"map:1:1"}]}',
                )
            ),
            reply(("next", "run_next", '{"reason":"continue"}')),
        ]
    )
    state = http.observation.state
    assert isinstance(state, native.RunState)
    raw_map = native.PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"map:0:0","room_type":"ancient","children":["map:1:0","map:1:1"]},'
        '{"position":"map:1:0","room_type":"rest","children":[]},'
        '{"position":"map:1:1","room_type":"monster","children":[]}],'
        '"legal_destinations":["map:1:0","map:1:1"]}'
    )
    state = state.model_copy(
        update={
            "progress": state.progress.model_copy(update={"position": "map:0:0"}),
            "actions": (
                native.Action(ref="travel-0", label="Rest", enabled=True, destination="map:1:0"),
                native.Action(ref="travel-1", label="Monster", enabled=True, destination="map:1:1"),
            ),
        }
    )
    http.observation = http.observation.model_copy(update={"state": state, "map": raw_map})

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        journal = Journal.create(
            tmp_path / "route.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="route",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="route", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=Notes(""),
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                recorded = RecordedLlm(llm, journal, role=native.DecisionOwner.RUN, stream="run:1")
                agent = RunAgent(lambda: recorded, tools, budget)
                route = native.RouteKey.model_validate(http.observation.location.model_dump())
                result = await execution.run_unit(agent, ActTask(route))
                assert isinstance(result, Executed)
        journal.close()
        archive = Journal.open(tmp_path / "route.sqlite")
        recorded_results = [
            message.data
            for request in archive.requests()
            for message in request.input
            if isinstance(message, ResultSnapshot) and isinstance(message.data, PlanArchiveData)
        ]
        assert any(data.value.plan.status == "accepted" for data in recorded_results)
        assert any(data.value.plan.status == "rejected" for data in recorded_results)
        archive.close()
        assert len(http.actions) == 1
        assert isinstance(http.actions[0].step, native.Interact)
        assert http.actions[0].step.action == "travel-0"
        outputs: dict[str, str] = {}
        for request in http.inputs:
            items = request["input"]
            assert isinstance(items, list)
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "function_call_output":
                    continue
                call_id, output = item.get("call_id"), item.get("output")
                assert isinstance(call_id, str) and isinstance(output, str)
                outputs[call_id] = output
        accepted = json.loads(outputs["plan"])
        rejected = json.loads(outputs["bad"])
        assert accepted["data"] == {
            "plan": {"status": "accepted", "next": "map:1:0"},
            "presentation": {"status": "not_available"},
            "restoration": {"status": "not_needed"},
        }
        assert accepted["observation"] is None
        planned = http.inputs[1]["input"]
        assert isinstance(planned, list) and isinstance(task := planned[-1], dict)
        assert isinstance(content := task["content"], str)
        initial = http.inputs[0]["input"]
        assert isinstance(initial, list) and isinstance(initial_task := initial[-1], dict)
        initial_content = initial_task["content"]
        assert isinstance(initial_content, str)
        assert json.loads(initial_content)["observation"]["card_rarity_offset"] == -0.05
        assert json.loads(initial_content)["observation"]["progress"]["acts"] == 3
        assert json.loads(content)["observation"]["dynamic_map"]["plan"] == {
            "edges": [{"from": "map:0:0", "to": "map:1:0"}]
        }
        assert rejected["outcome"]["reason"]["code"] == "ambiguous_next"

    asyncio.run(scenario())


def test_pending_reward_blocks_next_and_does_not_select_the_reward(tmp_path: object) -> None:
    import asyncio
    from pathlib import Path

    import httpx2 as httpx
    from test_combat import reply
    from test_responses import configuration
    from test_run import RunHTTP

    from sts2_harness.client import ModClient
    from sts2_harness.decisions import ActTask
    from sts2_harness.execution import Executed, Execution
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.game import LiveGame
    from sts2_harness.llm.client import Llm
    from sts2_harness.notes import Notes
    from sts2_harness.run import RunAgent
    from sts2_harness.tools import ToolCatalog

    assert isinstance(tmp_path, Path)
    http = RunHTTP(
        [
            reply(
                (
                    "plan",
                    "run_plan",
                    '{"reason":"rest","edges":[{"from":"map:0:0","to":"map:1:0"}]}',
                )
            ),
            reply(("next", "run_next", '{"reason":"move"}')),
            reply(
                (
                    "reward",
                    "run_act",
                    '{"reason":"take gold","steps":[{"kind":"interact","action":"reward"}]}',
                )
            ),
        ]
    )
    state = http.observation.state
    assert isinstance(state, native.RunState)
    raw_map = native.PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"map:0:0","room_type":"ancient","children":["map:1:0"]},'
        '{"position":"map:1:0","room_type":"rest","children":[]}],'
        '"legal_destinations":["map:1:0"]}'
    )
    state = state.model_copy(
        update={
            "screen": native.RunScreen.REWARDS,
            "progress": state.progress.model_copy(update={"position": "map:0:0"}),
            "actions": (native.Action(ref="reward", label="Take reward", enabled=True),),
        }
    )
    http.observation = http.observation.model_copy(update={"state": state, "map": raw_map})

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="blocked", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=Notes(""),
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(lambda: llm, tools, budget)
                route = native.RouteKey.model_validate(http.observation.location.model_dump())
                result = await execution.run_unit(agent, ActTask(route))
                assert isinstance(result, Executed)
        assert len(http.actions) == 1
        step = http.actions[0].step
        assert isinstance(step, native.Interact) and step.action == "reward"
        wire = json.dumps(http.inputs, ensure_ascii=False)
        assert "pending_choice" in wire

    asyncio.run(scenario())


def test_run_act_cannot_bypass_map_plan(tmp_path: object) -> None:
    import asyncio
    from pathlib import Path

    import httpx2 as httpx
    from test_combat import reply
    from test_responses import configuration
    from test_run import RunHTTP

    from sts2_harness.client import ModClient
    from sts2_harness.decisions import ActTask
    from sts2_harness.execution import Executed, Execution
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.game import LiveGame
    from sts2_harness.llm.client import Llm
    from sts2_harness.notes import Notes
    from sts2_harness.results import ActionStopped
    from sts2_harness.run import RunAgent
    from sts2_harness.tools import ToolCatalog

    assert isinstance(tmp_path, Path)
    http = RunHTTP(
        [reply(("bypass", "run_act", '{"steps":[{"kind":"interact","action":"travel-0"}]}'))]
    )
    state = http.observation.state
    assert isinstance(state, native.RunState)
    state = state.model_copy(
        update={
            "actions": (
                native.Action(ref="travel-0", label="Rest", enabled=True, destination="map:1:0"),
                native.Action(ref="travel-1", label="Monster", enabled=True, destination="map:1:1"),
            )
        }
    )
    http.observation = http.observation.model_copy(update={"state": state})

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="bypass", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=Notes(""),
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(lambda: llm, tools, budget)
                route = native.RouteKey.model_validate(http.observation.location.model_dump())
                result = await execution.run_unit(agent, ActTask(route))
                assert isinstance(result, Executed)
                assert isinstance(result.outcome.outcome, ActionStopped)
        assert http.actions == []

    asyncio.run(scenario())
