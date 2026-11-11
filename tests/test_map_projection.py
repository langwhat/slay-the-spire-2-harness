import json

from sts2_harness.client.models import Progress, PublicMap
from sts2_harness.context.map_projection import project_map


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
