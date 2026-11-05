from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import MISSING
from test_lifecycle import Campaign
from test_mod_client import COMBAT
from test_replay import record_campaign
from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy, CompactionDecision, measure
from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.context import Context
from sts2_harness.context.models import ActContent, CombatActInput, ReviewContent
from sts2_harness.journal import Journal, Manifest
from sts2_harness.journal_models import LiteralText, ResultSnapshot, TaskSnapshot, TextSnapshot
from sts2_harness.llm.messages import AssistantMessage, ResponsesReply, ToolCall, ToolResultMessage
from sts2_harness.results import ActionCompleted, ActionToolResult
from sts2_harness_eval import Reached, reach


def action(context: Context, ident: str, observation: native.Observation) -> None:
    context.append_reply(
        AssistantMessage(
            ResponsesReply.from_json(
                json.dumps(
                    [
                        {
                            "type": "function_call",
                            "call_id": ident,
                            "name": "combat_act",
                            "arguments": "{}",
                        }
                    ]
                )
            )
        )
    )
    context.append_tool_result(
        ToolCall(ident, "combat_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), observation),
    )


def test_combat_baseline_precedes_latest_group_and_retains_opening_advice() -> None:
    opening = native.Observation.model_validate_json(COMBAT)
    changed = opening.model_copy(
        update={
            "state": opening.state.model_copy(
                update={"player": opening.state.player.model_copy(update={"hp": 7})}
            )
        }
    )
    context = Context(native.DecisionOwner.COMBAT)
    context.begin_group(opening, "Opening advice")
    context.append_task(CombatActInput("Opening advice"), opening)
    action(context, "a0", changed)
    context.finish_group()
    context.begin_group(changed, "Opening advice")
    context.append_task(CombatActInput(), changed)
    action(context, "a1", changed)
    context.finish_group()
    context.begin_group(changed, "Opening advice")
    context.append_task(CombatActInput(), changed)

    candidate = context.compaction_candidate()
    baseline = json.loads(candidate[0].text)["context_baseline"]
    assert baseline["opening_notes"] == "Opening advice"
    assert baseline["observation"]["current"]["player"]["hp"] == 7
    selection = baseline["observation"]["decision"]["selection"]
    assert selection["selected"] == []
    assert [candidate["ref"] for candidate in selection["candidates"]] == ["c2", "c1"]
    assert "strategy_notes" not in baseline and "route_plan" not in baseline
    assert [item.call_id for item in candidate if isinstance(item, ToolResultMessage)] == ["a1"]
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    assert context.model_input() == candidate


def reasoning_query(protocol: Protocol, ident: str) -> bytes:
    from test_compaction_full_replay import response
    from test_messages import events, sse

    if protocol is Protocol.MESSAGES:
        stream = events()[:7]
        stream.extend(
            [
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "tool_use",
                        "id": ident,
                        "name": "combat_inspect",
                        "input": {"area": "state"},
                    },
                },
                {"type": "content_block_stop", "index": 2},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
        )
        return sse(stream)
    payload = response(protocol, (ident, "combat_inspect", '{"area":"state"}'))
    chunks = payload.decode().split("\n\n")
    chunk = json.loads(chunks[0][len("data: ") :])
    if protocol is Protocol.RESPONSES:
        chunk["response"]["output"].insert(
            0,
            {
                "type": "reasoning",
                "id": f"reasoning-{ident}",
                "summary": [],
                "encrypted_content": "synthetic-continuation",
            },
        )
    else:
        chunk["choices"][0]["delta"]["reasoning_content"] = "synthetic-continuation"
    chunks[0] = "data: " + json.dumps(chunk)
    return "\n\n".join(chunks).encode()


def combat_campaign(protocol: Protocol) -> Campaign:
    import random
    import string

    from test_compaction_full_replay import response
    from test_lifecycle import Campaign

    campaign = Campaign()
    start, battle, reward, next_act, second_battle, ending = campaign.states
    battle_two = battle.model_copy(
        update={
            "state": battle.state.model_copy(
                update={"player": battle.state.player.model_copy(update={"hp": 7})}
            )
        }
    )
    battle_three = battle.model_copy(
        update={
            "state": battle.state.model_copy(
                update={"player": battle.state.player.model_copy(update={"hp": 5})}
            )
        }
    )
    campaign.states = [
        start,
        battle,
        battle_two,
        battle_three,
        reward,
        next_act,
        second_battle,
        ending,
    ]
    old_text = "".join(random.Random(53).choices(string.ascii_letters, k=9000))
    campaign.responses = iter(
        [
            response(
                protocol,
                ("plan", "run_plan", '{"reason":"Battle.","edges":[{"from":"0:0","to":"1:0"}]}'),
            ),
            response(protocol, ("travel", "run_next", '{"reason":"Battle."}')),
            response(protocol, text=old_text),
            response(protocol, ("a0", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reasoning_query(protocol, "q1"),
            response(protocol, ("a1", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reasoning_query(protocol, "q2"),
            response(protocol, ("a2", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            response(protocol, text="Observed damage; later timing is unknown."),
            response(
                protocol, ("notes", "run_notes", '{"mode":"replace","text":"Reserve for act two."}')
            ),
            response(
                protocol,
                ("advance", "run_act", '{"steps":[{"kind":"interact","action":"advance"}]}'),
            ),
            response(
                protocol,
                ("plan2", "run_plan", '{"reason":"Battle.","edges":[{"from":"0:0","to":"1:0"}]}'),
            ),
            response(protocol, ("travel2", "run_next", '{"reason":"Battle."}')),
            response(protocol, ("terminal", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
        ]
    )
    return campaign


@pytest.mark.parametrize("protocol", list(Protocol))
@pytest.mark.parametrize("stage", ["before_action", "after_query", "review"])
def test_combat_compaction_recording_replays_without_repeating_actions(
    tmp_path: Path, protocol: Protocol, stage: str
) -> None:
    async def scenario() -> None:
        calibration = combat_campaign(protocol)
        with patch(
            "test_replay.configuration",
            return_value=configuration(tmp_path, LLM_PROTOCOL=protocol.value),
        ):
            await record_campaign(tmp_path / "calibration.sqlite", calibration)
        with closing(Journal.open(tmp_path / "calibration.sqlite")) as journal:
            calibration_records = journal.requests()
        combat_candidates = [
            index
            for index, record in enumerate(calibration_records)
            if record.role is native.DecisionOwner.COMBAT
            and record.confirmed_operation_cursor == (4 if stage == "review" else 3)
        ]
        target = combat_candidates[1 if stage == "after_query" else 0]
        estimate = measure(calibration.inputs[target], protocol).estimated_input_tokens
        previous = max(
            measure(calibration.inputs[index], protocol).estimated_input_tokens
            for index, record in enumerate(calibration_records[:target])
            if record.role is native.DecisionOwner.COMBAT
        )
        window = estimate * 10 // 9
        assert previous * 10 < window * 9 <= estimate * 10
        config = replace(
            configuration(tmp_path, LLM_PROTOCOL=protocol.value), context_window=window
        )

        def manifest_with_capacity(**kwargs: object) -> Manifest:
            return Manifest.model_validate(
                {**kwargs, "capacity_policy": CapacityPolicy(context_window=window)}
            )

        campaign = combat_campaign(protocol)
        path = tmp_path / "recording.sqlite"
        with (
            patch("test_replay.configuration", return_value=config),
            patch("test_replay.Manifest", side_effect=manifest_with_capacity),
        ):
            anchors = await record_campaign(path, campaign)
        with closing(Journal.open(path)) as journal:
            records = journal.requests()
            operations = journal.operations()
        compressed = next(
            record
            for record in records
            if record.role is native.DecisionOwner.COMBAT
            and record.preparation is not None
            and record.preparation.decision is CompactionDecision.COMPACTED
        )
        preparation = compressed.preparation
        assert preparation is not None and preparation.input_after is not None
        assert preparation.baseline is not None
        assert isinstance(preparation.baseline, TextSnapshot)
        assert isinstance(preparation.baseline.value, LiteralText)
        baseline = json.loads(preparation.baseline.value.body)["context_baseline"]
        assert baseline["opening_notes"] == "Opening plan."
        assert baseline["observation"]["current"]["player"]["hp"] == (
            7 if stage == "review" else 10
        )
        retained_players = [
            content.observation.current.player
            for item in preparation.input_after
            if isinstance(item, TaskSnapshot)
            and isinstance(content := item.content, ActContent)
            and content.observation.current is not MISSING
            and content.observation.current.player is not MISSING
        ]
        assert [player.hp for player in retained_players] == ([5] if stage == "review" else [7, 5])
        call_ids = [
            item.call_id for item in preparation.input_after if isinstance(item, ResultSnapshot)
        ]
        expected = {
            "before_action": ["q1", "a1"],
            "after_query": ["q1", "a1", "q2"],
            "review": ["q2", "a2"],
        }
        assert call_ids == expected[stage]
        wire = json.dumps(campaign.inputs[records.index(compressed)], ensure_ascii=False)
        if protocol is Protocol.MESSAGES:
            assert "Synthetic thought." in wire and "synthetic-signature" in wire
            assert "synthetic-redacted" in wire
        else:
            assert "synthetic-continuation" in wire
        review = next(
            record
            for record in records
            if record.role is native.DecisionOwner.COMBAT
            and any(
                isinstance(item, TaskSnapshot) and isinstance(item.content, ReviewContent)
                for item in record.input
            )
        )
        expected_review = ["q2", "a2"] if stage == "review" else ["q1", "a1", "q2", "a2"]
        assert [
            item.call_id for item in review.input if isinstance(item, ResultSnapshot)
        ] == expected_review
        actions_before = tuple(campaign.actions)
        inputs_before = tuple(campaign.inputs)
        for anchor in anchors:
            if anchor.model_sequence < compressed.sequence:
                continue
            async with reach(path, anchor) as result:
                assert isinstance(result, Reached), result
        assert tuple(campaign.actions) == actions_before
        assert tuple(campaign.inputs) == inputs_before
        with closing(Journal.open(path)) as journal:
            assert journal.operations() == operations
        assert len(campaign.actions) == 7
        assert records[-1].role is native.DecisionOwner.COMBAT
        assert records[-1].confirmed_operation_cursor == 6

    asyncio.run(scenario())


def test_repeated_combat_compaction_keeps_unknown_result_and_later_facts() -> None:
    from sts2_harness.results import ActionUnknown, Reason, ReasonCode, Suggestion

    opening = native.Observation.model_validate_json(COMBAT)
    next_state = opening.model_copy(
        update={
            "state": opening.state.model_copy(
                update={"player": opening.state.player.model_copy(update={"hp": 7})}
            ),
            "descriptions": {**opening.descriptions, "c1": "Current modified card text."},
        }
    )
    context = Context(native.DecisionOwner.COMBAT)
    context.begin_group(opening, "Keep the block engine.")
    context.append_task(CombatActInput("Keep the block engine."), opening)
    action(context, "old", next_state)
    context.finish_group()
    context.begin_group(next_state, "Keep the block engine.")
    context.append_task(CombatActInput(), next_state)
    call = ToolCall("uncertain", "combat_act", "{}")
    context.append_reply(
        AssistantMessage(
            ResponsesReply.from_json(
                '[{"type":"function_call","call_id":"uncertain",'
                '"name":"combat_act","arguments":"{}"}]'
            )
        )
    )
    context.append_tool_result(
        call,
        ActionToolResult(
            ActionUnknown(
                confirmed_steps=0,
                uncertain_step=1,
                reason=Reason(code=ReasonCode.EXECUTION_UNCONFIRMED, message="Not confirmed."),
                suggested_actions=(Suggestion(message="Inspect before choosing again."),),
            ),
            None,
        ),
    )
    context.finish_group()
    context.begin_group(next_state, "Keep the block engine.")
    context.append_task(CombatActInput(), next_state)
    first = context.compaction_candidate()
    assert '"observation":null' in first[-2].text
    context.validate_compaction(first)
    context.install_compaction(first)
    action(context, "confirmed", next_state)
    context.finish_group()
    context.begin_group(next_state, "Keep the block engine.")
    context.append_task(CombatActInput(), next_state)
    second = context.compaction_candidate()
    baseline = json.loads(second[0].text)["context_baseline"]
    assert baseline["opening_notes"] == "Keep the block engine."
    assert baseline["observation"]["current"]["player"]["hp"] == 7
    assert any(
        item["ref"] == "c1" and item["text"] == "Current modified card text."
        for item in baseline["observation"]["current"]["card_descriptions"]
    )
    assert [item.call_id for item in second if isinstance(item, ToolResultMessage)] == ["confirmed"]
    context.validate_compaction(second)
    context.install_compaction(second)


def test_combat_compaction_preserves_guidance_changes_in_order() -> None:
    from sts2_harness.knowledge import DocumentView, GuidanceContent
    from sts2_harness.llm.messages import TextMessage, TextRole
    from sts2_harness.material_text import MaterialText

    observation = native.Observation.model_validate_json(COMBAT)
    original = DocumentView(
        id="battle",
        name=MaterialText.create("name", "Battle"),
        text=MaterialText.create("text", "Initial guidance."),
        resources=(),
    )
    updated = original.model_copy(update={"text": MaterialText.create("text", "Updated guidance.")})
    context = Context(
        native.DecisionOwner.COMBAT,
        system=(TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=(original,))),),
    )
    context.begin_group(observation, "Opening")
    context.append_task(CombatActInput("Opening"), observation)
    action(context, "first", observation)
    context.finish_group()
    context.begin_group(observation, "Opening")
    context.append_materials((updated,))
    context.append_task(CombatActInput(), observation)
    action(context, "second", observation)
    context.finish_group()
    context.begin_group(observation, "Opening")
    context.append_task(CombatActInput(), observation)
    first = context.compaction_candidate()
    contents = [message.text for message in first]
    assert next(i for i, text in enumerate(contents) if "Initial guidance." in text) < next(
        i for i, text in enumerate(contents) if "Updated guidance." in text
    )
    context.install_compaction(first)
    action(context, "third", observation)
    context.finish_group()
    context.begin_group(observation, "Opening")
    context.append_task(CombatActInput(), observation)
    second = context.compaction_candidate()
    contents = [message.text for message in second]
    assert sum("Updated guidance." in text for text in contents) == 1
    assert next(i for i, text in enumerate(contents) if "Updated guidance." in text) < next(
        i for i, text in enumerate(contents) if "context_baseline" in text
    )
    context.validate_compaction(second)
    context.install_compaction(second)
