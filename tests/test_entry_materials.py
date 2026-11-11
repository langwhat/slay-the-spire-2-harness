import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_mod_client import CARD, COMBAT, RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent
from sts2_harness.decisions import ActionDecision, ActTask
from sts2_harness.entry_materials import entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.knowledge import DocumentView, SkillSummary, SubjectId
from sts2_harness.llm.client import Llm
from sts2_harness.material_text import MaterialUnavailable
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.tools import ToolCatalog


class ObjectDocuments(FileDocuments):
    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        return ()


def test_combat_guidance_uses_current_public_objects_without_skill_navigation(
    tmp_path: Path,
) -> None:
    value = json.loads(COMBAT)
    value["encounter"] = {"name": "Encounter", "definition_id": "ENCOUNTER.TEST"}
    state = value["state"]
    for pile in ("hand", "draw", "discard", "exhaust", "other"):
        state["piles"][pile] = [pile]
        value["objects"][pile] = dict(json.loads(CARD), definition_id=f"CARD.{pile.upper()}")
    value["objects"]["candidate"] = dict(json.loads(CARD), definition_id="CARD.CANDIDATE")
    value["objects"]["unreferenced"] = dict(json.loads(CARD), definition_id="CARD.HIDDEN")
    value["objects"]["option"] = {"kind": "option", "name": "Skip"}
    value["objects"]["relic"] = {
        "kind": "relic",
        "name": "Relic",
        "definition_id": "RELIC.TEST",
        "is_used_up": True,
    }
    value["objects"]["potion"] = {
        "kind": "potion",
        "name": "Potion",
        "definition_id": "POTION.TEST",
        "usable": True,
        "needs_target": False,
        "target_type": None,
        "usage": None,
    }
    state["relics"] = ["relic"]
    state["potions"] = ["potion"]
    state["selection"]["candidates"] = ["candidate", "hand", "option"]
    state["enemies"][0]["powers"] = [{"name": "Enemy power", "definition_id": "POWER.ENEMY"}]
    state["inactive_enemies"] = [dict(state["enemies"][0], definition_id="ENEMY.INACTIVE")]
    state["allies"] = [
        {
            "ref": "ally",
            "name": "Ally",
            "hp": 1,
            "max_hp": 1,
            "block": 0,
            "alive": True,
            "powers": [{"name": "Ally power", "definition_id": "POWER.ALLY"}],
        }
    ]
    state["orbs"]["slots"].append({"name": "Unknown orb", "passive": None, "evoke": None})
    observation = native.Observation.model_validate_json(json.dumps(value))
    expected = (
        "CARD.HAND",
        "CARD.DRAW",
        "CARD.DISCARD",
        "CARD.EXHAUST",
        "CARD.OTHER",
        "CARD.CANDIDATE",
        "RELIC.TEST",
        "POTION.TEST",
        "ENCOUNTER.TEST",
        "ENEMY.ONE",
        "ENEMY.INACTIVE",
        "POWER.STRENGTH",
        "POWER.ENEMY",
        "POWER.ALLY",
        "ORB.LIGHTNING",
    )
    identities = (*expected, "CARD.HIDDEN", "CARD.ONE", "ENCOUNTER.BOSS")
    sources: list[DocumentSource] = []
    for identity in identities:
        path = tmp_path / identity
        path.write_text(f"Rules for {identity}.", encoding="utf-8")
        sources.append(
            DocumentSource(
                identity,
                identity,
                "Rules",
                path,
                subjects=(
                    SubjectId(category=identity.split(".")[0].lower(), definition_id=identity),
                ),
            )
        )

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Combat guidance must use the current publication.")

    async def scenario() -> None:
        async with ModClient("http://mod", transport=httpx.MockTransport(unexpected)) as client:
            execution = Execution(
                LiveGame(client, namespace="test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            documents = ObjectDocuments(tuple(sources))
            found = await entry_materials(
                execution, documents, native.DecisionOwner.COMBAT, observation, ()
            )
            assert tuple(document.id for document in found) == expected
            run = native.Observation.model_validate_json(RUN).model_copy(
                update={"map": observation.map}
            )
            assert tuple(
                document.id
                for document in await entry_materials(
                    execution, documents, native.DecisionOwner.RUN, run, ()
                )
            ) == ("ENCOUNTER.BOSS",)
            incomplete = observation.model_copy(
                update={
                    "objects": {
                        key: entry for key, entry in observation.objects.items() if key != "hand"
                    }
                }
            )
            with pytest.raises(MaterialUnavailable, match="Current object 'hand' is unavailable"):
                await entry_materials(
                    execution, documents, native.DecisionOwner.COMBAT, incomplete, ()
                )

    asyncio.run(scenario())


def test_combat_appends_new_or_changed_knowledge_before_the_next_request(tmp_path: Path) -> None:
    opening = native.Observation.model_validate_json(COMBAT)
    later_value = json.loads(COMBAT)
    later_value["objects"]["new"] = dict(json.loads(CARD), definition_id="CARD.NEW")
    later_value["state"]["selection"]["candidates"].append("new")
    later = native.Observation.model_validate_json(json.dumps(later_value))
    shared = tmp_path / "shared.txt"
    shared.write_text("Applies to: CARD.ONE, ENEMY.ONE\nInitial complete rules.")
    new = tmp_path / "new.txt"
    new.write_text("Applies to: CARD.NEW\nComplete new rules.")
    documents = ObjectDocuments(
        (
            DocumentSource(
                "shared",
                "Shared",
                "Rules",
                shared,
                subjects=(
                    SubjectId(category=native.NativeCategory.CARD, definition_id="CARD.ONE"),
                    SubjectId(category=native.NativeCategory.ENEMY, definition_id="ENEMY.ONE"),
                ),
            ),
            DocumentSource(
                "new",
                "New",
                "Rules",
                new,
                subjects=(
                    SubjectId(category=native.NativeCategory.CARD, definition_id="CARD.NEW"),
                ),
            ),
        )
    )
    inputs: list[bytes] = []
    responses = iter(
        (
            reply(("first", "combat_inspect", '{"area":"state"}')),
            reply(("second", "combat_inspect", '{"area":"state"}')),
            reply(("act", "combat_act", '{"steps":[{"kind":"select","refs":[]}]}')),
            reply(("fresh", "combat_act", '{"steps":[{"kind":"select","refs":[]}]}')),
        )
    )

    def model(request: httpx.Request) -> httpx.Response:
        inputs.append(request.content)
        shared.write_text("Applies to: CARD.ONE, ENEMY.ONE\nRevised complete rules.")
        return httpx.Response(
            200,
            content=next(responses),
            headers={
                "Content-Type": "text/event-stream",
            },
        )

    def mod(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                content=native.Status(
                    presentation=None,
                    location=later.location,
                    input_readiness=later.input_readiness,
                    control_intent=native.ControlIntent.RUNNING,
                    receipt=None,
                ).to_json(),
            )
        assert request.url.path == "/state"
        return httpx.Response(
            200,
            content=native.StateCompleted(
                status=native.ResultStatus.COMPLETED,
                observation=later,
            ).to_json(),
        )

    async def scenario() -> None:
        async with ModClient("http://mod", transport=httpx.MockTransport(mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="test", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                native.DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                skills=documents,
                supplements=documents,
            )

            async def materials(observation: native.Observation) -> tuple[DocumentView, ...]:
                return await entry_materials(
                    execution, documents, native.DecisionOwner.COMBAT, observation, ()
                )

            task = ActTask(native.RouteKey.model_validate(opening.location.model_dump()))
            async with Llm(configuration(tmp_path), transport=httpx.MockTransport(model)) as llm:
                role = CombatAgent(
                    llm, tools, ToolRecoveryBudget(), opening_notes="", materials=materials
                )
                assert isinstance(await role.decide(task, opening), ActionDecision)
                fresh = CombatAgent(
                    llm, tools, ToolRecoveryBudget(), opening_notes="", materials=materials
                )
                assert isinstance(await fresh.decide(task, later), ActionDecision)

    asyncio.run(scenario())
    wire = [json.loads(request)["input"] for request in inputs]
    initial = json.loads(wire[0][1]["content"])
    assert wire[0][1]["role"] == "system"
    assert initial == {
        "guidance": [
            {
                "id": "shared",
                "name": "Shared",
                "resources": [],
                "text": "Applies to: CARD.ONE, ENEMY.ONE\nInitial complete rules.",
            }
        ]
    }
    additions = [
        json.loads(message["content"])
        for message in wire[1]
        if message.get("role") == "user" and "guidance" in json.loads(message["content"])
    ]
    assert additions == [
        {
            "guidance": [
                {"id": "shared", "name": "Shared", "text": shared.read_text(), "resources": []},
                {"id": "new", "name": "New", "text": new.read_text(), "resources": []},
            ]
        }
    ]
    assert wire[2][: len(wire[1])] == wire[1]
    assert [message.get("role") for message in wire[2][len(wire[1]) :]] == [None, None, "user"]
    assert wire[3][1]["role"] == "system"
    assert json.loads(wire[3][1]["content"]) == additions[0]
