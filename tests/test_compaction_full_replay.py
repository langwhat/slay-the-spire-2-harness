import asyncio
import json
import random
import string
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from test_chat_completions import chat_reply
from test_combat import reply
from test_lifecycle import Campaign, battle_state, run_state
from test_messages import events, sse
from test_replay import record_campaign
from test_responses import configuration

from sts2_harness.capacity import CapacityPolicy, CompactionDecision, measure
from sts2_harness.client import models as native
from sts2_harness.client.models import DecisionOwner
from sts2_harness.config import Protocol
from sts2_harness.journal import Journal, Manifest
from sts2_harness_eval import Reached, reach


def response(protocol: Protocol, *calls: tuple[str, str, str], text: str | None = None) -> bytes:
    if protocol is Protocol.RESPONSES:
        return reply(*calls, text=text)
    if protocol is Protocol.CHAT_COMPLETIONS:
        return chat_reply(*calls, text=text)
    stream = events()[:1]
    for index, (call_id, name, arguments) in enumerate(calls):
        stream.extend(
            [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": json.loads(arguments),
                    },
                },
                {"type": "content_block_stop", "index": index},
            ]
        )
    if text is not None:
        stream.extend(
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": text},
                },
                {"type": "content_block_stop", "index": 0},
            ]
        )
    stream.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if calls else "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
    )
    return sse(stream)


def campaign_with_history(protocol: Protocol) -> Campaign:
    campaign = Campaign()
    # Keep the Run decisions in one native node to exercise capacity fallback.
    campaign.states[3] = run_state(1, "next-room")
    campaign.states[4] = battle_state(1, "battle2")
    for index in (0, 3):
        state = json.loads(campaign.states[index].to_json())
        state["state"]["progress"]["position"] = "0:0"
        state["state"]["actions"][0].update(enabled=True, destination="1:0")
        state["state"]["actions"][0].pop("reason", None)
        state["map"] = {
            "bosses": [],
            "nodes": [
                {"position": "0:0", "room_type": "event", "children": ["1:0", "1:1"]},
                {"position": "1:0", "room_type": "monster", "children": []},
                {"position": "1:1", "room_type": "monster", "children": []},
            ],
            "legal_destinations": ["1:0", "1:1"],
        }
        campaign.states[index] = native.Observation.model_validate_json(json.dumps(state))
    reward = json.loads(campaign.states[2].to_json())
    reward["state"]["screen"] = "event"
    reward["state"]["actions"][0]["ref"] = "advance"
    campaign.states[2] = native.Observation.model_validate_json(json.dumps(reward))
    for index in (0, 2, 3):
        observation = campaign.states[index]
        campaign.states[index] = observation.model_copy(
            update={"location": observation.location.model_copy(update={"node_id": "shared-event"})}
        )
    random_text = "".join(random.Random(2026).choices(string.ascii_letters, k=6000))
    campaign.responses = iter(
        [
            response(
                protocol,
                ("long-note", "run_notes", json.dumps({"mode": "replace", "text": random_text})),
            ),
            response(
                protocol, ("restore-note", "run_notes", '{"mode":"replace","text":"Opening plan."}')
            ),
            response(
                protocol,
                (
                    "plan",
                    "run_plan",
                    '{"reason":"Enter combat.","edges":[{"from":"0:0","to":"1:0"}]}',
                ),
            ),
            response(protocol, ("travel", "run_next", '{"reason":"Enter combat."}')),
            response(protocol, ("battle1", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            response(protocol, text="A useful battle review."),
            response(
                protocol,
                (
                    "notes",
                    "run_notes",
                    json.dumps(
                        {
                            "mode": "edit",
                            "old_text": "Opening plan.",
                            "new_text": "Opening plan. Updated once.",
                        }
                    ),
                ),
            ),
            response(
                protocol,
                ("advance", "run_act", '{"steps":[{"kind":"interact","action":"advance"}]}'),
            ),
            response(
                protocol,
                (
                    "plan2",
                    "run_plan",
                    '{"reason":"Enter combat.","edges":[{"from":"0:0","to":"1:0"}]}',
                ),
            ),
            response(protocol, ("travel2", "run_next", '{"reason":"Enter combat."}')),
            response(protocol, ("battle2", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
        ]
    )
    return campaign


@pytest.mark.parametrize("protocol", list(Protocol))
def test_run_compaction_replays_without_repeating_game_or_notes(
    tmp_path: Path, protocol: Protocol
) -> None:
    async def scenario() -> None:
        calibration = campaign_with_history(protocol)
        with patch(
            "test_replay.configuration",
            return_value=configuration(tmp_path, LLM_PROTOCOL=protocol.value),
        ):
            await record_campaign(tmp_path / "calibration.sqlite", calibration)
        with closing(Journal.open(tmp_path / "calibration.sqlite")) as journal:
            calibration_records = journal.requests()
        # Select a window from measured complete protocol requests: the third Run group
        # crosses the threshold, while all earlier Run requests remain under it.
        target = next(
            index
            for index, record in enumerate(calibration_records)
            if record.role is DecisionOwner.RUN and record.confirmed_operation_cursor == 3
        )
        estimate = measure(calibration.inputs[target], protocol).estimated_input_tokens
        previous = max(
            measure(calibration.inputs[index], protocol).estimated_input_tokens
            for index, record in enumerate(calibration_records[:target])
            if record.role is DecisionOwner.RUN
        )
        window = estimate * 10 // 9
        assert previous * 10 < window * 9 <= estimate * 10

        campaign = campaign_with_history(protocol)
        path = tmp_path / "recording.sqlite"
        config = replace(
            configuration(tmp_path, LLM_PROTOCOL=protocol.value), context_window=window
        )

        def manifest_with_capacity(**kwargs: object) -> Manifest:
            return Manifest.model_validate(
                {**kwargs, "capacity_policy": CapacityPolicy(context_window=window)}
            )

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
            if record.preparation is not None
            and record.preparation.decision is CompactionDecision.COMPACTED
        )
        preparation = compressed.preparation
        assert preparation is not None
        assert preparation.estimate_before * 10 >= window * 9
        assert preparation.estimate_after is not None and preparation.estimate_after < window
        assert preparation.active_group_id is not None
        assert preparation.retained_complete_group_id is not None
        # The retained edit is non-idempotent: replaying it twice would duplicate this phrase
        # in the reconstructed model input and fail reach's digest comparison.
        assert preparation.input_after is not None
        retained = "".join(item.model_dump_json() for item in preparation.input_after)
        assert "Updated once." in retained
        assert preparation.baseline is not None
        baseline = preparation.baseline.model_dump_json()
        assert "Opening plan." in baseline and "Updated once." not in baseline
        random_text = "".join(random.Random(2026).choices(string.ascii_letters, k=6000))
        assert random_text not in retained
        actions_before = tuple(campaign.actions)
        requests_before = tuple(campaign.inputs)
        source = path.read_bytes()
        for anchor in anchors:
            if anchor.model_sequence < compressed.sequence:
                continue
            async with reach(path, anchor) as result:
                assert isinstance(result, Reached), result
        assert tuple(campaign.actions) == actions_before
        assert tuple(campaign.inputs) == requests_before
        assert path.read_bytes() == source
        with closing(Journal.open(path)) as journal:
            assert journal.operations() == operations

    asyncio.run(scenario())
