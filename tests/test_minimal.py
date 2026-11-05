import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import BattleHTTP, reply
from test_lifecycle import Campaign, battle_state
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.decisions import CombatHandoff
from sts2_harness.game import RunEndReason
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness_eval import ModelAnchor, Reached, reach


@pytest.mark.parametrize("consecutive", [False, True])
def test_minimal_session_plays_with_current_state_and_replays_its_requests(
    tmp_path: Path, consecutive: bool
) -> None:
    campaign = Campaign()
    second_turn = campaign.states[1]
    campaign.states.insert(
        2,
        second_turn.model_copy(
            update={"state": second_turn.state.model_copy(update={"turn": 2})}
        ),
    )
    if consecutive:
        campaign.states.insert(3, battle_state(1, "extra-battle"))
    campaign.responses = iter(
        [
            reply(
                (
                    "plan",
                    "run_plan",
                    '{"reason":"Enter combat.","edges":[{"from":"0:0","to":"1:0"}]}',
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
                    '{"reason":"Enter combat.","edges":[{"from":"0:0","to":"1:0"}]}',
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
                initial_notes="Hidden initial strategy.",
            ),
            path,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            documents=documents,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
            assert session.notes.read() == "Hidden initial strategy."

        assert len(campaign.actions) == (7 if consecutive else 6)
        assert len(campaign.inputs) == (9 if consecutive else 8)
        for request in campaign.inputs:
            encoded = json.dumps(request)
            assert "Hidden initial strategy." not in encoded
            assert "strategy_notes" not in encoded and "opening_notes" not in encoded
            assert "Synthetic automatic guidance." in encoded
            assert 'task=\\"review\\"' not in encoded
            tools = request["tools"]
            assert isinstance(tools, list)
            names = {
                tool["name"]
                for tool in tools
                if isinstance(tool, dict) and isinstance(tool["name"], str)
            }
            assert names in (
                {"run_inspect", "run_act", "run_plan", "run_next"},
                {"combat_inspect", "combat_act"},
            )

        first = campaign.inputs[0]["input"]
        second = campaign.inputs[1]["input"]
        assert isinstance(first, list) and isinstance(second, list)
        assert second[: len(first)] == first
        next_combat_decision = json.dumps(campaign.inputs[3]["input"])
        assert '"call_id": "battle1"' not in next_combat_decision
        after_battle = json.dumps(campaign.inputs[5 if consecutive else 4]["input"])
        assert '"call_id": "plan"' not in after_battle
        assert '"call_id": "travel"' not in after_battle
        assert "battle_report" in after_battle and "route_plan" in after_battle

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


@pytest.mark.parametrize("unknown", [False, True])
def test_minimal_combat_keeps_uncertain_feedback_until_success(
    tmp_path: Path, unknown: bool
) -> None:
    async def scenario() -> None:
        first = json.loads(
            reply(
                (
                    "first",
                    "combat_act",
                    '{"steps":[{"kind":"end_turn"},{"kind":"end_turn"}]}',
                )
            )
            .decode()
            .removeprefix("data: ")
        )
        first["response"]["output"].insert(
            0,
            {
                "type": "reasoning",
                "id": "r1",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "Synthetic current reasoning."}],
            },
        )
        battle = BattleHTTP(
            [
                ("data: " + json.dumps(first) + "\n\n").encode(),
                reply(("next", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("last", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ],
            steps_to_end=3,
        )
        battle.failed_first = not unknown
        battle.unknown_first = unknown
        running = asyncio.create_task(battle.run(tmp_path, minimal=True))
        if unknown:
            await battle.waiting.wait()
            battle.start()
        result = await running
        assert isinstance(result, CombatHandoff) and result.review is None
        assert len(battle.inputs) == len(battle.actions) == 3
        original = battle.inputs[0]["input"]
        recovery = battle.inputs[1]["input"]
        assert isinstance(original, list) and isinstance(recovery, list)
        assert recovery[: len(original)] == original
        text = json.dumps(recovery)
        assert "Synthetic current reasoning." in text
        assert ("Unconfirmed." if unknown else "Dispatch failed.") in text
        assert '"call_id": "first"' in text
        after_success = json.dumps(battle.inputs[2]["input"])
        assert "Synthetic current reasoning." not in after_success
        assert '"call_id": "first"' not in after_success
        assert '"call_id": "next"' not in after_success

    asyncio.run(scenario())
