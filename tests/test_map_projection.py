import json

from pydantic import MISSING
from test_mod_client import COMBAT

from sts2_harness.client.models import CombatState, DecisionOwner, Observation, Progress, PublicMap
from sts2_harness.context import CombatActInput, Context
from sts2_harness.context.map_projection import planned_route, project_map
from sts2_harness.route_plan import Edge, RoutePlan


def test_map_uses_one_odds_value_when_all_reachable_unknown_rooms_match() -> None:
    raw_map = PublicMap.model_validate_json(
        '{"bosses":[{"name":"首领","definition_id":"ENCOUNTER.BOSS"}],"nodes":['
        '{"position":"map:0:0","room_type":"ancient","children":["map:1:0"]},'
        '{"position":"map:1:0","room_type":"unknown","children":["map:2:0"],'
        '"odds":{"monster":0.6,"event":0.4}},'
        '{"position":"map:2:0","room_type":"unknown","children":[],'
        '"odds":{"monster":0.6,"event":0.4}}],"legal_destinations":["map:1:0"]}'
    )
    progress = Progress(acts=3, act=1, floor=0, position="map:0:0", room_type="ancient")

    view = json.loads(project_map(raw_map, progress, None).to_json())

    assert view["boss"] == [{"name": "首领", "definition_id": "ENCOUNTER.BOSS"}]
    assert view["next"] == ["s1"]
    assert view["stretches"] == [{"id": "s1", "floors": "0-2", "rooms": "A??", "next": []}]
    assert view["unknown_odds"] == {"monster": 0.6, "event": 0.4}


def test_unknown_and_known_empty_destinations_stay_distinct() -> None:
    progress = Progress(acts=3, act=1, floor=None, position=None, room_type=None)
    unknown = PublicMap.model_validate_json('{"bosses":[],"nodes":[],"legal_destinations":null}')
    empty = PublicMap.model_validate_json('{"bosses":[],"nodes":[],"legal_destinations":[]}')

    assert json.loads(project_map(unknown, progress, None).to_json()) == {
        "boss": [],
        "at": None,
        "next": None,
        "stretches": [],
        "unknown_odds": None,
        "plan": None,
    }
    assert json.loads(project_map(empty, progress, None).to_json())["next"] == []


def test_partial_and_complete_routes_keep_known_room_order() -> None:
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
                        "position": f"map:{i}:0",
                        "room_type": room,
                        "children": [f"map:{i + 1}:0"] if i < 8 else [],
                    }
                    for i, room in enumerate(rooms)
                ],
                "legal_destinations": ["map:2:0"],
            }
        )
    )
    progress = Progress(acts=3, act=1, floor=1, position="map:1:0", room_type="monster")
    full = RoutePlan(
        "map:1:0", "rest", tuple(Edge(f"map:{i}:0", f"map:{i + 1}:0") for i in range(1, 8))
    )
    partial = RoutePlan("map:1:0", "rest", full.edges[:3])

    assert planned_route(raw_map, progress, full) == ("E?R$TMB",)
    assert planned_route(raw_map, progress, partial) == ("E?R",)
    assert json.loads(project_map(raw_map, progress, full).to_json())["plan"] == ["s1"]
    assert json.loads(project_map(raw_map, progress, partial).to_json())["plan"] is None


def test_combat_progress_receives_remaining_stretch_rooms_and_native_boss_count() -> None:
    observation = Observation.model_validate_json(COMBAT)
    assert isinstance(observation.state, CombatState)
    progress = observation.state.progress.model_copy(
        update={"position": "map:0:0", "bosses_left": 2}
    )
    public_map = PublicMap.model_validate_json(
        '{"bosses":[],"nodes":['
        '{"position":"map:0:0","room_type":"monster","children":["map:1:0"]},'
        '{"position":"map:1:0","room_type":"restsite","children":["map:2:0"]},'
        '{"position":"map:2:0","room_type":"boss","children":[]}],'
        '"legal_destinations":["map:1:0"]}'
    )
    observation = observation.model_copy(
        update={
            "map": public_map,
            "state": observation.state.model_copy(update={"progress": progress}),
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.set_route_plan(
        RoutePlan("map:0:0", "rest", (Edge("map:0:0", "map:1:0"), Edge("map:1:0", "map:2:0")))
    )
    context.append_task(CombatActInput(), observation)
    rendered = json.loads(context.model_input()[-1].text)["observation"]["progress"]
    assert rendered["route"] == ["RB"] and rendered["bosses_left"] == 2
    assert planned_route(public_map, progress, None) is MISSING
