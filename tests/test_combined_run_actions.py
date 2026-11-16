import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue
from test_combat import reply
from test_lifecycle import Campaign
from test_responses import configuration
from test_run import RunHTTP

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActTask
from sts2_harness.execution import Executed, Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.llm.client import Llm
from sts2_harness.note_values import NotesState
from sts2_harness.notes import Notes
from sts2_harness.results import QueryToolResult, Reason, ReasonCode, Rejected, Suggestion
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


@pytest.mark.parametrize("tool", ["run_act", "run_next"])
@pytest.mark.parametrize("refused", [False, True])
@pytest.mark.parametrize("minimal", [False, True])
def test_action_writes_supplied_notes_before_its_first_step(
    tmp_path: Path, tool: str, refused: bool, minimal: bool
) -> None:
    rejected = refused or minimal
    notes = Notes(NotesState(core=("CARD.BASH",), other="Keep the potion."))
    arguments: dict[str, JsonValue] = (
        {"steps": [{"kind": "interact", "action": "travel"}]}
        if tool == "run_act"
        else {"reason": "Enter the next room.", "stretches": ["s2"]}
    )
    arguments["notes"] = {"plan": "Find block before the Boss.", "other": ""}
    http = RunHTTP(
        [
            reply(("combined", tool, json.dumps(arguments))),
            reply(("fallback", "run_act", '{"steps":[{"kind":"interact","action":"finish"}]}')),
        ]
    )
    if tool == "run_next":
        http.observation = Campaign().current
    http.observation = http.observation.model_copy(
        update={
            "state": http.observation.state.model_copy(
                update={
                    "actions": (
                        *http.observation.state.actions,
                        native.Action(ref="finish", label="Finish", enabled=True),
                    )
                }
            )
        }
    )

    def model_response(request: httpx.Request) -> httpx.Response:
        if http.inputs:
            assert http.actions == []
        body = json.loads(request.content)
        for definition in body["tools"]:
            if definition["name"] in ("run_act", "run_next"):
                assert ("notes" in definition["parameters"]["properties"]) is not minimal
        return http.model(request)

    async def guard(observation: native.Observation | None) -> QueryToolResult | None:
        if refused:
            return QueryToolResult(
                Rejected(
                    reason=Reason(code=ReasonCode.BUSY, message="Notes are not writable."),
                    suggested_actions=(Suggestion(message="Continue when notes are writable."),),
                ),
                observation,
            )
        return None

    def mod(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/action":
            assert notes.read().plan == ("" if rejected else "Find block before the Boss.")
            assert notes.read().core == ("CARD.BASH",)
            assert notes.read().other == ("Keep the potion." if rejected else "")
        return http.mod(request)

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with (
            ModClient("http://mod", transport=httpx.MockTransport(mod)) as client,
            Llm(configuration(tmp_path), transport=httpx.MockTransport(model_response)) as model,
        ):
            execution = Execution(
                LiveGame(client, namespace="combined", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=notes,
                notes_write_guard=guard,
                minimal=minimal,
            )
            agent = RunAgent(lambda: model, tools, budget)
            route = native.RouteKey.model_validate(http.observation.location.model_dump())
            assert isinstance(await execution.run_unit(agent, ActTask(route)), Executed)
            if tool == "run_next":
                assert (agent.route_plan is None) is rejected
        assert len(http.inputs) == (2 if rejected else 1)
        assert len(http.actions) == 1
        (tmp_path / "actual-request-bodies.json").write_text(json.dumps(http.inputs, indent=2))

    asyncio.run(scenario())
