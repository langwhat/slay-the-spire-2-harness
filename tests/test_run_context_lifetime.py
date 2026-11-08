import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_mod_client import RUN
from test_responses import configuration
from test_run import RunHTTP

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActTask
from sts2_harness.execution import Executed, Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.client import Llm
from sts2_harness.material_text import MaterialText
from sts2_harness.notes import Notes
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


@pytest.mark.parametrize("minimal", [False, True])
def test_run_keeps_local_exchanges_and_rebuilds_current_node_input(
    tmp_path: Path, minimal: bool
) -> None:
    http = RunHTTP(
        [
            reply(("open-choice", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            reply(("select-choice", "run_act", '{"steps":[{"kind":"select","refs":[]}]}')),
            reply(("new-room", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
        ]
    )
    http.observation = http.observation.model_copy(
        update={"map": native.PublicMap(bosses=(), nodes=(), legal_destinations=None)}
    )
    notes = Notes("Old decision memo.")

    async def materials(observation: native.Observation) -> tuple[DocumentView, ...]:
        node = observation.location.node_id
        return (
            DocumentView(
                id="current-room",
                name=MaterialText.create("name", "Room guide"),
                text=MaterialText.create("guide", f"Guidance for {node}."),
                resources=(),
            ),
        )

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="node-context", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=notes,
                minimal=minimal,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(lambda: llm, tools, budget, materials=materials)
                for index in range(3):
                    if index == 2:
                        notes.replace("Current decision memo.")
                        http.observation = http.observation.model_copy(
                            update={
                                "location": http.observation.location.model_copy(
                                    update={"node_id": "next"}
                                )
                            }
                        )
                    route = native.RouteKey.model_validate(http.observation.location.model_dump())
                    assert isinstance(await execution.run_unit(agent, ActTask(route)), Executed)
        first = http.inputs[0]["input"]
        second = http.inputs[1]["input"]
        assert isinstance(first, list) and isinstance(second, list)
        assert second[: len(first)] == first
        assert "open-choice" in json.dumps(second)
        latest = json.dumps(http.inputs[2]["input"])
        assert "open-choice" not in latest and "select-choice" not in latest
        assert "Guidance for node." not in latest
        assert "Guidance for next." in latest
        assert "Old decision memo." not in latest
        assert ("Current decision memo." in latest) is (not minimal)
        assert "map_topology" in latest and "dynamic_map" in latest
        assert len(http.actions) == 3

    asyncio.run(scenario())


def test_session_retains_latest_battle_after_reward_actions_and_replays_new_node(
    tmp_path: Path,
) -> None:
    from contextlib import closing

    from test_lifecycle import Campaign, battle_state, run_state, terminal
    from test_replay import record_campaign

    from sts2_harness.journal import Journal
    from sts2_harness_eval import Reached, reach

    campaign = Campaign()
    campaign.states = [
        battle_state(1, "battle-one"),
        run_state(1, "battle-one"),
        run_state(1, "battle-one"),
        run_state(1, "shop"),
        battle_state(1, "battle-two"),
        run_state(1, "battle-two"),
        terminal(run_state(1, "finished")),
    ]
    for index in (1, 2, 3, 5):
        state = campaign.states[index]
        campaign.states[index] = state.model_copy(
            update={"state": state.state.model_copy(update={"screen": native.RunScreen.EVENT})}
        )
    action = '{"steps":[{"kind":"interact","action":"travel"}]}'
    campaign.responses = iter(
        [
            reply(("combat-one", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="First battle payment bottleneck."),
            reply(("take-gold", "run_act", action)),
            reply(("select-card", "run_act", action)),
            reply(("leave-shop", "run_act", action)),
            reply(("combat-two", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Second battle access bottleneck."),
            reply(("finish", "run_act", action)),
        ]
    )

    async def scenario() -> None:
        path = tmp_path / "node-recording.sqlite"
        anchors = await record_campaign(path, campaign)
        same_room = json.dumps(campaign.inputs[3]["input"])
        assert "take-gold" in same_room
        assert same_room.count("First battle payment bottleneck.") == 1
        next_room = json.dumps(campaign.inputs[4]["input"])
        assert "take-gold" not in next_room and "select-card" not in next_room
        assert next_room.count("First battle payment bottleneck.") == 1
        assert "previous_battle" in next_room
        latest = json.dumps(campaign.inputs[7]["input"])
        assert "First battle payment bottleneck." not in latest
        assert latest.count("Second battle access bottleneck.") == 1
        with closing(Journal.open(path)) as journal:
            assert journal.manifest.run_context_policy == "node_local_v1"
            assert len(journal.requests()) == 8
        actions_before = tuple(campaign.actions)
        for anchor in anchors:
            async with reach(path, anchor) as result:
                assert isinstance(result, Reached), result
        assert tuple(campaign.actions) == actions_before

    asyncio.run(scenario())


@pytest.mark.parametrize("pending", [False, True])
def test_latest_battle_survives_capacity_compaction_once(pending: bool) -> None:
    from test_run import handoff

    from sts2_harness.context import Context
    from sts2_harness.context.models import RunActInput
    from sts2_harness.llm.messages import TextMessage

    observation = native.Observation.model_validate_json(RUN)
    context = Context(native.DecisionOwner.RUN)
    evidence = handoff("Keep the payment bottleneck.")
    context.begin_group(observation, "")
    context.append_task(
        RunActInput(handoff=evidence) if pending else RunActInput(previous_battle=evidence),
        observation,
    )
    context.finish_group()
    for _ in range(2):
        context.begin_group(observation, "")
        context.append_task(RunActInput(), observation)
        context.append_correction("Continue the current selection.")
        context.finish_group()
    candidate = context.compaction_candidate()
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    context.begin_group(observation, "")
    context.append_task(RunActInput(previous_battle=evidence), observation)
    text = "".join(item.text for item in context.model_input() if isinstance(item, TextMessage))
    assert text.count("Keep the payment bottleneck.") == 1
    assert "previous_battle" in text
    context.finish_group()
    context.install_compaction(context.compaction_candidate())
    text = "".join(item.text for item in context.model_input() if isinstance(item, TextMessage))
    assert text.count("Keep the payment bottleneck.") == 1


@pytest.mark.parametrize("blocked_by", ["missing_map", "uncertain_action"])
def test_node_rebuild_waits_for_complete_map_and_settled_feedback(
    tmp_path: Path, blocked_by: str
) -> None:
    from pydantic import MISSING

    from sts2_harness.decisions import ActionDecision
    from sts2_harness.results import (
        ActionCompleted,
        ActionToolResult,
        ActionUnknown,
        Reason,
        ReasonCode,
        Suggestion,
    )

    http = RunHTTP(
        [
            reply((name, "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}'))
            for name in ("before-move", "recover-move", "after-recovery")
        ]
    )
    full_map = native.PublicMap(bosses=(), nodes=(), legal_destinations=None)
    http.observation = http.observation.model_copy(update={"map": full_map})

    async def scenario() -> None:
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:

            async def observe() -> native.Observation:
                return http.observation

            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=Notes(""),
                observe=observe,
                inspect=client.inspect,
                catalog=client.catalog,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(lambda: llm, tools, ToolRecoveryBudget())
                for index in range(3):
                    if index == 1:
                        http.observation = http.observation.model_copy(
                            update={
                                "location": http.observation.location.model_copy(
                                    update={"node_id": "next"}
                                ),
                                "map": MISSING if blocked_by == "missing_map" else full_map,
                            }
                        )
                    if index == 2:
                        http.observation = http.observation.model_copy(update={"map": full_map})
                    route = native.RouteKey.model_validate(http.observation.location.model_dump())
                    assert isinstance(
                        await agent.decide(ActTask(route), http.observation), ActionDecision
                    )
                    outcome = (
                        ActionUnknown(
                            confirmed_steps=0,
                            uncertain_step=1,
                            suggested_actions=(Suggestion(message="Inspect before acting again."),),
                            reason=Reason(
                                code=ReasonCode.CONTROL_UNAVAILABLE,
                                message="Synthetic uncertain write.",
                            ),
                        )
                        if index == 0 and blocked_by == "uncertain_action"
                        else ActionCompleted(completed_steps=1)
                    )
                    await agent.accept(ActionToolResult(outcome, http.observation))
        second = json.dumps(http.inputs[1]["input"])
        assert "before-move" in second
        if blocked_by == "uncertain_action":
            assert "Synthetic uncertain write." in second
        third = json.dumps(http.inputs[2]["input"])
        assert "before-move" not in third and "recover-move" not in third

    asyncio.run(scenario())
