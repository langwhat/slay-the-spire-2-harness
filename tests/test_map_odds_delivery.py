import json

import pytest
from pydantic import MISSING
from test_mod_client import RUN

from sts2_harness.client.models import DecisionOwner, MapOdds, Observation, PublicMap
from sts2_harness.context import Context
from sts2_harness.context.models import BaselineMessage, RunActInput, TaskMessage


def test_map_odds_change_independently_and_survive_node_resets_and_compaction() -> None:
    opening = Observation.model_validate_json(RUN).model_copy(
        update={
            "map": PublicMap.model_validate_json(
                '{"bosses":[],"nodes":[{"position":"map:2:0","room_type":"unknown",'
                '"children":[],"odds":{"monster":0,"event":0.4}},'
                '{"position":"missing-odds","room_type":"unknown","children":[]}],'
                '"legal_destinations":[]}'
            )
        }
    )
    context = Context(DecisionOwner.RUN)
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    first = json.loads(message.content.to_json())["observation"]["current"]
    assert first["unknown_odds"] == {"map:2:0": {"monster": 0, "event": 0.4}}
    assert "unknown_odds" not in first["dynamic_map"]
    context.finish_group()
    moved = opening.model_copy(
        update={
            "location": opening.location.model_copy(update={"node_id": "next"}),
            "state": opening.state.model_copy(
                update={
                    "progress": opening.state.progress.model_copy(update={"position": "map:2:0"})
                }
            ),
        }
    )
    context.begin_group(moved, "")
    context.append_task(RunActInput(), moved)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    update = json.loads(message.content.to_json())["observation"]["current"]
    assert "unknown_odds" not in update
    assert update["dynamic_map"]["position"] == "map:2:0"
    context.install_compaction(context.compaction_candidate())
    context.finish_group()
    context.begin_group(moved, "")
    candidate = context.compaction_candidate()
    baseline = next(m for m in candidate if isinstance(m, BaselineMessage))
    contents = baseline.content["context_baseline"]
    assert isinstance(contents, dict)
    observation = contents["observation"]
    assert isinstance(observation, dict)
    current = observation["current"]
    assert isinstance(current, dict)
    assert current["unknown_odds"] == first["unknown_odds"]
    context.install_compaction(candidate)
    assert moved.map is not MISSING
    changed_odds = moved.model_copy(
        update={
            "map": moved.map.model_copy(
                update={
                    "nodes": (
                        moved.map.nodes[0].model_copy(update={"odds": MapOdds(event=0.9)}),
                        *moved.map.nodes[1:],
                    )
                }
            )
        }
    )
    context.append_task(RunActInput(), changed_odds)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    replacement = json.loads(message.content.to_json())["observation"]["current"]
    assert replacement["unknown_odds"] == {"map:2:0": {"event": 0.9}}
    assert "dynamic_map" not in replacement
    context.append_task(RunActInput(), moved)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    assert (
        json.loads(message.content.to_json())["observation"]["current"]["unknown_odds"]
        == first["unknown_odds"]
    )
    changed = moved.model_copy(update={"map": moved.map.model_copy(update={"nodes": ()})})
    context.append_task(RunActInput(), changed)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    assert json.loads(message.content.to_json())["observation"]["current"]["unknown_odds"] == {}
    context.append_task(RunActInput(), changed.model_copy(update={"map": MISSING}))
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    assert json.loads(message.content.to_json())["observation"]["current"]["unknown_odds"] is None


@pytest.mark.parametrize("boundary", ["game", "run", "act"])
def test_identical_map_odds_are_redelivered_across_lifetime_boundaries(boundary: str) -> None:
    opening = Observation.model_validate_json(RUN).model_copy(
        update={
            "map": PublicMap.model_validate_json(
                '{"bosses":[],"nodes":[],"legal_destinations":null}'
            )
        }
    )
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), opening)
    if boundary == "act":
        changed = opening.model_copy(
            update={
                "state": opening.state.model_copy(
                    update={"progress": opening.state.progress.model_copy(update={"act": 2})}
                )
            }
        )
    else:
        changed = opening.model_copy(
            update={
                "location": opening.location.model_copy(
                    update={"game_instance_id" if boundary == "game" else "run_id": "new"}
                )
            }
        )
    context.append_task(RunActInput(), changed)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    assert json.loads(message.content.to_json())["observation"]["current"]["unknown_odds"] == {}
