import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign, battle_state
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.game import RunEndReason
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.note_values import NotesState
from sts2_harness_eval import ModelAnchor, Reached, reach


@pytest.mark.parametrize("consecutive", [False, True])
def test_minimal_session_plays_with_current_state_and_replays_its_requests(
    tmp_path: Path, consecutive: bool
) -> None:
    campaign = Campaign()
    second_turn = campaign.states[1]
    campaign.states.insert(
        2,
        second_turn.model_copy(update={"state": second_turn.state.model_copy(update={"turn": 2})}),
    )
    if consecutive:
        campaign.states.insert(3, battle_state(1, "extra-battle"))
    campaign.responses = iter(
        [
            reply(
                (
                    "plan",
                    "run_plan",
                    '{"reason":"Enter combat.","stretches":["s2"]}',
                )
            ),
            reply(("travel", "run_next", '{"reason":"Enter combat."}')),
            reply(("battle1", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(("battle1again", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            *(
                [reply(("extra", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))]
                if consecutive
                else []
            ),
            reply(("advance", "run_act", '{"steps":[{"kind":"interact","action":"advance"}]}')),
            reply(
                (
                    "plan2",
                    "run_plan",
                    '{"reason":"Enter combat.","stretches":["s2"]}',
                )
            ),
            reply(("travel2", "run_next", '{"reason":"Enter combat."}')),
            reply(("battle2", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
        ]
    )
    guidance = tmp_path / "guidance.md"
    guidance.write_text("Synthetic automatic guidance.")
    documents = FileDocuments(
        (DocumentSource(id="automatic", name="Automatic", description="Synthetic", path=guidance),)
    )

    async def scenario() -> None:
        path = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                minimal=True,
                materials=SessionMaterials(
                    entry_documents_enabled=True,
                    act_guidance={1: ("automatic",), 2: ("automatic",)},
                    battle_guidance=("automatic",),
                ),
                recording_id="minimal",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(other="Hidden initial strategy."),
            ),
            path,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            documents=documents,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
            assert session.notes.read().other == "Hidden initial strategy."

        assert len(campaign.actions) == (7 if consecutive else 6)
        assert len(campaign.inputs) == (9 if consecutive else 8)
        for request in campaign.inputs:
            encoded = json.dumps(request)
            assert "Hidden initial strategy." not in encoded
            assert "Synthetic automatic guidance." in encoded
            tools = request["tools"]
            assert isinstance(tools, list)
            names = {
                tool["name"]
                for tool in tools
                if isinstance(tool, dict) and isinstance(tool["name"], str)
            }
            assert names in (
                {"run_act", "run_plan", "run_next"},
                {"combat_act"},
            )
            messages = request["input"]
            assert isinstance(messages, list)
            latest = messages[-1]
            assert isinstance(latest, dict) and latest.get("role") == "user"
            content = latest["content"]
            assert isinstance(content, str)
            task = json.loads(content)
            assert task["task"] == "act" and "observation" in task
            assert "strategy_notes" not in task and "opening_notes" not in task
            assert "instruction" not in task

        first = campaign.inputs[0]["input"]
        second = campaign.inputs[1]["input"]
        assert isinstance(first, list) and isinstance(second, list)
        assert second[: len(first)] == first
        result = second[-2]
        assert isinstance(result, dict) and result["type"] == "function_call_output"
        output = result["output"]
        assert isinstance(output, str) and json.loads(output)["observation"] is None
        first_combat = campaign.inputs[2]["input"]
        next_combat = campaign.inputs[3]["input"]
        assert isinstance(first_combat, list) and isinstance(next_combat, list)
        assert next_combat[: len(first_combat)] == first_combat
        result = next_combat[-2]
        assert isinstance(result, dict) and result["type"] == "function_call_output"
        output = result["output"]
        assert isinstance(output, str) and json.loads(output)["observation"] is None
        assert '"call_id": "battle1"' in json.dumps(next_combat)
        after_battle = campaign.inputs[5 if consecutive else 4]["input"]
        assert isinstance(after_battle, list)
        assert after_battle[: len(second)] == second
        text = json.dumps(after_battle)
        assert '"call_id": "travel"' in text and '"call_id": "plan"' in text
        current_task = after_battle[-1]
        assert isinstance(current_task, dict) and isinstance(current_task["content"], str)
        current = json.loads(current_task["content"])
        assert current["battle_report"]["last_observed_turn"] == (None if consecutive else 2)
        next_act = campaign.inputs[6 if consecutive else 5]["input"]
        assert isinstance(next_act, list)
        next_act_text = json.dumps(next_act)
        assert '"call_id": "travel"' not in next_act_text
        assert '"call_id": "plan"' not in next_act_text

        archive = Journal.open(path)
        assert archive.manifest.minimal
        records = archive.requests()
        archive.close()
        for record in records:
            anchor = ModelAnchor("minimal", record.sequence)
            async with reach(path, anchor) as result:
                assert result == Reached(
                    anchor, record.confirmed_operation_cursor, record.semantic_digest
                )

    asyncio.run(scenario())
