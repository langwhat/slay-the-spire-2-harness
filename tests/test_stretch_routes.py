import json

from test_mod_client import RUN

from sts2_harness.client import models as native
from sts2_harness.context import Context, RunActInput


def test_run_input_shows_reachable_stretches_with_stable_ids() -> None:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    public_map = native.PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"map:0:0","room_type":"ancient",'
        '"children":["map:1:0","map:1:1"]},'
        '{"position":"map:1:0","room_type":"monster","children":["map:2:0"]},'
        '{"position":"map:1:1","room_type":"monster","children":["map:2:1"]},'
        '{"position":"map:2:0","room_type":"elite","children":["map:3:0"]},'
        '{"position":"map:2:1","room_type":"unknown","children":["map:3:0"]},'
        '{"position":"map:3:0","room_type":"restsite","children":["map:4:0"]},'
        '{"position":"map:4:0","room_type":"boss","children":[]}],'
        '"legal_destinations":["map:2:0"]}'
    )
    progress = observation.state.progress.model_copy(update={"position": "map:1:0"})
    observation = observation.model_copy(
        update={
            "state": observation.state.model_copy(update={"progress": progress}),
            "map": public_map,
        }
    )
    context = Context(native.DecisionOwner.RUN)
    context.append_task(RunActInput(), observation)
    body = json.loads(context.model_input()[-1].text)
    assert body["observation"]["map"] == {
        "boss": [],
        "at": {"node": "map:1:0", "stretch": "s2", "room": 1},
        "next": ["s2"],
        "stretches": [
            {"id": "s2", "floors": "1-2", "rooms": "ME", "next": ["s4"]},
            {"id": "s4", "floors": "3-4", "rooms": "RB", "next": []},
        ],
        "unknown_odds": None,
        "plan": None,
    }


def test_map_preserves_current_availability_and_per_room_unknown_odds() -> None:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    public_map = native.PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"map:0:0","room_type":"ancient","children":["map:1:0"]},'
        '{"position":"map:1:0","room_type":"unknown","children":["map:2:0"],'
        '"odds":{"event":0.5,"monster":0.5}},'
        '{"position":"map:2:0","room_type":"unknown","children":["map:3:0"]},'
        '{"position":"map:3:0","room_type":"boss","children":[]}],'
        '"legal_destinations":null}'
    )
    progress = observation.state.progress.model_copy(update={"position": None})
    observation = observation.model_copy(
        update={
            "state": observation.state.model_copy(update={"progress": progress}),
            "map": public_map,
        }
    )
    context = Context(native.DecisionOwner.RUN)
    context.append_task(RunActInput(), observation)
    view = json.loads(context.model_input()[-1].text)["observation"]["map"]
    assert view["at"] is None and view["next"] is None
    assert view["unknown_odds"] == {"s1": {"2": {"monster": 0.5, "event": 0.5}, "3": None}}

    context = Context(native.DecisionOwner.RUN)
    empty = observation.model_copy(
        update={"map": public_map.model_copy(update={"legal_destinations": ()})}
    )
    context.append_task(RunActInput(), empty)
    view = json.loads(context.model_input()[-1].text)["observation"]["map"]
    assert view["next"] == []
