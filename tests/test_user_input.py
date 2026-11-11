import asyncio
import json
from pathlib import Path

import httpx2 as httpx
from pydantic import JsonValue
from test_combat import reply
from test_mod_client import COMBAT
from test_responses import configuration

from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, InspectResult
from sts2_harness.combat import CombatAgent
from sts2_harness.decisions import (
    ActionDecision,
    ActTask,
)
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.llm.client import Llm
from sts2_harness.note_values import NotesView
from sts2_harness.notes import Notes
from sts2_harness.tools import ToolCatalog
from sts2_harness.user_input import UserInputQueue, create_input_app
from sts2_harness.user_input_values import UserInput

ACT = '{"steps":[{"kind":"end_turn"}]}'


def suggestions(request: dict[str, JsonValue]) -> list[list[str]]:
    found: list[list[str]] = []
    wire = request["input"]
    assert isinstance(wire, list)
    for item in wire:
        if isinstance(item, dict) and item.get("role") == "user":
            content = item["content"]
            if isinstance(content, str) and content.startswith("[viewer] "):
                found.append([line.removeprefix("[viewer] ") for line in content.splitlines()])
    return found


UNAVAILABLE = native.TechnicalError(
    status=native.ResultStatus.FAILED,
    error=native.InternalError(code=native.ErrorCode.INTERNAL_ERROR, message="Unavailable."),
)


def tools_for(
    owner: native.DecisionOwner, opening: native.Observation, notes: Notes | None = None
) -> ToolCatalog:
    async def observe() -> native.Observation:
        return opening

    async def inspect(_: native.InspectQuery) -> InspectResult:
        return UNAVAILABLE

    async def catalog(_: native.CatalogRequest) -> CatalogResult:
        return UNAVAILABLE

    return ToolCatalog(owner, notes=notes, observe=observe, inspect=inspect, catalog=catalog)


def agent_with_model(
    tmp_path: Path, handler: httpx.MockTransport
) -> tuple[Llm, ToolCatalog, ToolRecoveryBudget]:
    return (
        Llm(configuration(tmp_path, LLM_EXTRA_RETRIES="1"), transport=handler, sleep=_no_sleep),
        tools_for(native.DecisionOwner.COMBAT, native.Observation.model_validate_json(COMBAT)),
        ToolRecoveryBudget(),
    )


async def _no_sleep(_: float) -> None:
    return None


def test_input_enters_at_each_logical_request_and_retries_reuse_the_frozen_request(
    tmp_path: Path,
) -> None:
    opening = native.Observation.model_validate_json(COMBAT)
    queue = UserInputQueue()
    queue.submit((UserInput("1", "block first"),))
    requests: list[dict[str, JsonValue]] = []
    replies = iter(
        (
            reply(("inspect", "combat_inspect", '{"area":"state"}')),
            None,
            reply(("act", "combat_act", ACT)),
        )
    )

    def model(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            queue.submit((UserInput("2", "kill the left enemy"),))
        body = next(replies)
        if body is None:
            # Arrives during a failed attempt, so the retry must not contain it.
            queue.submit((UserInput("3", "too late for this request"),))
            return httpx.Response(500, json={"error": {"code": "server_error"}})
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    async def scenario() -> None:
        llm, tools, budget = agent_with_model(tmp_path, httpx.MockTransport(model))
        async with llm:
            role = CombatAgent(
                llm, tools, budget, opening_notes=NotesView(other=""), user_input=queue
            )
            decision = await role.decide(
                ActTask(native.RouteKey.model_validate(opening.location.model_dump())), opening
            )
        assert isinstance(decision, ActionDecision)

    asyncio.run(scenario())
    assert suggestions(requests[0]) == [["block first"]]
    assert suggestions(requests[1]) == [["block first"], ["kill the left enemy"]]
    assert suggestions(requests[2]) == suggestions(requests[1])
    assert queue.drain() == (UserInput("3", "too late for this request"),)
    assert queue.delivered == ("1", "2", "3")


def test_http_boundary_submits_and_reports_deliveries() -> None:
    queue = UserInputQueue()

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_input_app(queue))
        async with httpx.AsyncClient(transport=transport, base_url="http://input") as client:
            rejected = await client.post("/inputs", content='{"inputs":[]}')
            assert rejected.status_code == 415
            empty = await client.post("/inputs", json={"inputs": []})
            assert empty.status_code == 400
            blank = await client.post("/inputs", json={"inputs": [{"id": "1", "text": "  "}]})
            assert blank.status_code == 400
            accepted = await client.post(
                "/inputs", json={"inputs": [{"id": "1", "text": "weigh the elite"}]}
            )
            assert accepted.json() == {"instance": queue.instance, "accepted": 1}
            assert (await client.get("/inputs/deliveries")).json() == {
                "instance": queue.instance,
                "last": 0,
                "deliveries": [],
            }
            queue.drain()
            state = (await client.get("/inputs/deliveries", params={"after": 0})).json()
            assert state["deliveries"] == [{"sequence": 1, "id": "1"}]
            later = (await client.get("/inputs/deliveries", params={"after": 1})).json()
            assert later["deliveries"] == []

    asyncio.run(scenario())


_VIEWER_RULE = "viewer-rule-sentinel"
