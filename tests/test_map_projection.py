import json

from test_mod_client import COMBAT

from sts2_harness.client.models import CombatState, DecisionOwner, Observation, Progress, PublicMap
from sts2_harness.context import CombatActInput, Context
from sts2_harness.context.map_projection import planned_route, project_map
from sts2_harness.route_plan import Edge, RoutePlan


def test_projects_complete_native_topology_and_dynamic_sections() -> None:
    raw_map = PublicMap.model_validate_json(
        """{"bosses":[{"name":"首领","definition_id":"ENCOUNTER.BOSS"}],
        "nodes":[
          {"position":"map:0:0","room_type":"ancient","children":["map:1:0"]},
          {"position":"map:1:0","room_type":"unknown","children":[],
           "odds":{"monster":0.6,"event":0.4}}
        ],"legal_destinations":["map:1:0","map:1:2"]}"""
    )
    progress = Progress(acts=3, act=1, floor=0, position="map:0:0", room_type="ancient")

    topology, dynamic, odds = project_map(raw_map, progress, None)

    assert json.loads(topology.to_json()) == {
        "bosses": [{"name": "首领", "definition_id": "ENCOUNTER.BOSS"}],
        "nodes": [
            {"id": "map:1:0", "room_type": "unknown", "children": []},
            {"id": "map:0:0", "room_type": "ancient", "children": ["map:1:0"]},
        ],
    }
    assert {ref: json.loads(value.to_json()) for ref, value in odds.items()} == {
        "map:1:0": {"monster": 0.6, "event": 0.4}
    }
    assert json.loads(dynamic.to_json()) == {
        "position": "map:0:0",
        "legal_destinations": ["map:1:0", "map:1:2"],
        "special_movement": {"free_travel_active": None, "remaining_uses": None},
        "plan": None,
    }


def test_unknown_and_known_empty_destinations_stay_distinct() -> None:
    progress = Progress(acts=3, act=1, floor=None, position=None, room_type=None)
    unknown = PublicMap.model_validate_json('{"bosses":[],"nodes":[],"legal_destinations":null}')
    empty = PublicMap.model_validate_json('{"bosses":[],"nodes":[],"legal_destinations":[]}')

    topology, unknown_dynamic, odds = project_map(unknown, progress, None)
    _, empty_dynamic, _ = project_map(empty, progress, None)

    assert json.loads(topology.to_json()) == {"bosses": [], "nodes": []}
    assert odds == {}
    assert json.loads(unknown_dynamic.to_json()) == {
        "position": None,
        "legal_destinations": None,
        "special_movement": {"free_travel_active": None, "remaining_uses": None},
        "plan": None,
    }
    assert json.loads(empty_dynamic.to_json())["legal_destinations"] == []


def test_ahead_counts_only_remaining_chain_before_boss() -> None:
    rooms = (
        "ancient",
        "monster",
        "elite",
        "unknown",
        "restsite",
        "shop",
        "treasure",
        "monster",
        "boss",
    )
    raw_map = PublicMap.model_validate_json(
        json.dumps(
            {
                "bosses": [],
                "nodes": [
                    {
                        "position": str(i),
                        "room_type": room,
                        "children": [str(i + 1)] if i < 8 else [],
                    }
                    for i, room in enumerate(rooms)
                ],
                "legal_destinations": ["1"],
            }
        )
    )
    plan = RoutePlan("1", "rest", tuple(Edge(str(i), str(i + 1)) for i in range(1, 8)))
    progress = Progress(acts=3, act=1, floor=1, position="1", room_type="monster")

    _, dynamic, _ = project_map(raw_map, progress, plan)

    assert json.loads(dynamic.to_json())["ahead"] == {
        "fights": 1,
        "elites": 1,
        "unknown": 1,
        "rests": 1,
        "shops": 1,
    }
    assert planned_route(raw_map, progress, plan) == rooms[2:]


def test_ahead_is_omitted_for_partial_or_branched_plan() -> None:
    raw_map = PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"0","room_type":"monster","children":["1"]},'
        '{"position":"1","room_type":"restsite","children":["2","3"]},'
        '{"position":"2","room_type":"boss","children":[]},'
        '{"position":"3","room_type":"boss","children":[]}],"legal_destinations":["1"]}'
    )
    progress = Progress(acts=3, act=3, floor=40, position="0", room_type="monster")
    for edges in ((Edge("0", "1"),), (Edge("0", "1"), Edge("1", "2"), Edge("1", "3"))):
        _, dynamic, _ = project_map(raw_map, progress, RoutePlan("0", "rest", edges))
        assert "ahead" not in json.loads(dynamic.to_json())


def test_combat_progress_receives_only_accepted_remaining_route_and_native_boss_count() -> None:
    observation = Observation.model_validate_json(COMBAT)
    assert isinstance(observation.state, CombatState)
    progress = observation.state.progress.model_copy(update={"position": "0", "bosses_left": 2})
    public_map = PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"0","room_type":"monster","children":["1"]},'
        '{"position":"1","room_type":"restsite","children":["2"]},'
        '{"position":"2","room_type":"boss","children":[]}],"legal_destinations":["1"]}'
    )
    observation = observation.model_copy(
        update={
            "map": public_map,
            "state": observation.state.model_copy(update={"progress": progress}),
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.set_route_plan(RoutePlan("0", "rest", (Edge("0", "1"), Edge("1", "2"))))
    context.append_task(CombatActInput(), observation)
    progress = json.loads(context.model_input()[-1].text)["observation"]["progress"]
    assert progress["route"] == ["restsite", "boss"] and progress["bosses_left"] == 2
    context = Context(DecisionOwner.COMBAT)
    context.set_route_plan(RoutePlan("0", "rest", (Edge("0", "1"),)))
    context.append_task(CombatActInput(), observation)
    assert "route" not in json.loads(context.model_input()[-1].text)["observation"]["progress"]
