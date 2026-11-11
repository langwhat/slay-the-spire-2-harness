import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
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
