import json

import pytest
from pydantic import ValidationError

from sts2_harness.client.models import Progress, PublicMap
from sts2_harness.context.map_projection import DynamicMap, project_map


def test_projects_complete_native_topology_and_dynamic_sections() -> None:
    raw_map = PublicMap.model_validate_json(
        """{"bosses":[{"name":"首领","definition_id":"ENCOUNTER.BOSS"}],
        "nodes":[
          {"position":"map:0:0","room_type":"ancient","children":["map:1:0"]},
          {"position":"map:1:0","room_type":"unknown","children":[],
           "odds":{"monster":0.6,"event":0.4}}
        ],"legal_destinations":["map:1:0","map:1:2"]}"""
    )
    progress = Progress(act=1, floor=0, position="map:0:0", room_type="ancient")

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
    progress = Progress(act=1, floor=None, position=None, room_type=None)
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


def test_requires_published_destinations_field() -> None:
    with pytest.raises(ValidationError):
        PublicMap.model_validate_json('{"bosses":[],"nodes":[]}')


def test_complete_dynamic_replacement_contains_accepted_remaining_plan_and_fresh_odds() -> None:
    from sts2_harness.route_plan import Edge, RoutePlan

    first = PublicMap.model_validate_json(
        """{"bosses":[{"name":"Boss"}],"nodes":[
          {"position":"map:1:0","room_type":"unknown","children":["map:2:0"],
           "odds":{"monster":0,"shop":0.75}},
          {"position":"map:2:0","room_type":"monster","children":[]}
        ],"legal_destinations":["map:2:0"]}"""
    )
    second = PublicMap.model_validate_json(
        """{"bosses":[{"name":"Boss"}],"nodes":[
          {"position":"map:1:0","room_type":"unknown","children":["map:2:0"]},
          {"position":"map:2:0","room_type":"monster","children":[]}
        ],"legal_destinations":[]}"""
    )
    progress = Progress(act=1, floor=1, position="map:1:0", room_type="unknown")
    plan = RoutePlan(
        position="map:1:0",
        reason="Retain the branch",
        edges=(Edge("map:1:0", "map:2:0"), Edge("map:2:0", "map:3:2")),
    )

    topology, dynamic, odds = project_map(first, progress, plan)
    same_topology, refreshed, refreshed_odds = project_map(second, progress, plan)

    assert topology == same_topology
    moved_topology, _, _ = project_map(
        first, progress.model_copy(update={"floor": 2, "position": "map:2:0"}), None
    )
    assert moved_topology == topology
    assert {ref: json.loads(value.to_json()) for ref, value in odds.items()} == {
        "map:1:0": {"monster": 0, "shop": 0.75}
    }
    assert DynamicMap.model_validate_json(dynamic.to_json()) == dynamic
    assert json.loads(refreshed.to_json()) == {
        "position": "map:1:0",
        "legal_destinations": [],
        "special_movement": {"free_travel_active": None, "remaining_uses": None},
        "plan": {
            "edges": [
                {"from": "map:1:0", "to": "map:2:0"},
                {"from": "map:2:0", "to": "map:3:2"},
            ],
        },
    }
    assert refreshed_odds == {}
    assert DynamicMap.model_validate_json(refreshed.to_json()) == refreshed
