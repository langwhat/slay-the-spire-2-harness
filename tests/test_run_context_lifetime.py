import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from test_combat import reply
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
from sts2_harness.note_values import NotesState
from sts2_harness.notes import Notes
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


@pytest.mark.parametrize("minimal", [False, True])
def test_run_keeps_act_context_and_appends_updates_across_nodes(
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
    notes = Notes(NotesState(other="Old decision memo."))

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
                LiveGame(client, namespace="act-context", game_instance_id="game", run_id="run"),
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
                        notes.write(NotesState(other="Current decision memo."), {"other"})
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
        third = http.inputs[2]["input"]
        assert isinstance(first, list) and isinstance(second, list) and isinstance(third, list)
        assert second[: len(first)] == first
        assert third[: len(second)] == second
        assert "open-choice" in json.dumps(second)
        appended = json.dumps(third[len(second) :])
        assert "Guidance for next." in appended
        assert ("Current decision memo." in appended) is (not minimal)
        observations: list[dict[str, JsonValue]] = []
        for item in third:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.startswith("{"):
                continue
            task = TypeAdapter(dict[str, JsonValue]).validate_json(content)
            observation = task.get("observation")
            if isinstance(observation, dict):
                observations.append(observation)
        assert sum(1 for obs in observations if obs.get("baseline") is True) == 1
        assert any("map" in observation for observation in observations)
        assert len(http.actions) == 3

    asyncio.run(scenario())


def test_act_local_run_context_compacts_cleanly_across_nodes() -> None:
    from test_context import RUN

    from sts2_harness.context import Context
    from sts2_harness.context.models import RunActInput
    from sts2_harness.note_values import NotesView

    first = native.Observation.model_validate_json(RUN)
    second = first.model_copy(
        update={
            "location": first.location.model_copy(update={"node_id": "node-2"}),
            "state": first.state.model_copy(
                update={"progress": first.state.progress.model_copy(update={"floor": 2})}
            ),
        }
    )
    third = second.model_copy(
        update={
            "location": second.location.model_copy(update={"node_id": "node-3"}),
            "state": second.state.model_copy(
                update={"progress": second.state.progress.model_copy(update={"floor": 3})}
            ),
        }
    )
    ctx = Context(native.DecisionOwner.RUN)
    for obs, plan_text in (
        (first, "Node 1 plan"),
        (second, "Node 2 plan"),
        (third, "Node 3 plan"),
    ):
        notes = NotesView(plan=plan_text)
        ctx.begin_group(obs, notes)
        ctx.append_task(RunActInput(notes), obs)
        ctx.finish_group()

    candidate = ctx.compaction_candidate()
    ctx.validate_compaction(candidate)
    ctx.install_compaction(candidate)

    messages = ctx.model_input()
    baseline = json.loads(messages[0].text)["context_baseline"]["observation"]
    assert baseline["baseline"] is True
    assert baseline["location"]["node_id"] == "node-2"
    retained = json.loads(messages[-1].text)["observation"]
    assert retained["location"]["node_id"] == "node-3"
    assert retained["notes"]["plan"] == "Node 3 plan"
