import asyncio
import json

import pytest
from pydantic import MISSING, ValidationError
from test_mod_client import COMBAT
from test_orb_observation import read_observation

from sts2_harness.client import ClientFailureKind, ClientUnavailable
from sts2_harness.client.models import CombatState, DecisionOwner, MelodyTag, RelicStatus
from sts2_harness.context import CombatActInput, Context
from sts2_harness.context.run_models import RunObservation


def test_combat_delivers_melodies_and_clears_when_absent() -> None:
    async def read() -> None:
        context = Context(DecisionOwner.COMBAT)
        payload = json.loads(COMBAT)
        payload["state"]["melodies"] = []
        observed = (await read_observation(json.dumps(payload))).observation
        assert isinstance(observed.state, CombatState)
        assert observed.state.melodies == ()
        context.append_task(CombatActInput(), observed)
        assert json.loads(context.model_input()[-1].text)["observation"]["melodies"] == []

        payload["state"]["melodies"] = ["wah", "ki"]
        payload["stamp"]["observation_sequence"] += 1
        observed = (await read_observation(json.dumps(payload))).observation
        assert isinstance(observed.state, CombatState)
        assert observed.state.melodies == (MelodyTag.WAH, MelodyTag.KI)
        context.append_task(CombatActInput(), observed)
        assert json.loads(context.model_input()[-1].text)["observation"]["melodies"] == [
            "wah",
            "ki",
        ]

        del payload["state"]["melodies"]
        payload["stamp"]["observation_sequence"] += 1
        observed = (await read_observation(json.dumps(payload))).observation
        assert isinstance(observed.state, CombatState)
        assert observed.state.melodies is MISSING
        context.append_task(CombatActInput(), observed)
        assert json.loads(context.model_input()[-1].text)["observation"]["melodies"] is None

    asyncio.run(read())


def test_mod_rejects_unknown_or_duplicate_melody_tags() -> None:
    async def read(melodies: list[str]) -> None:
        payload = json.loads(COMBAT)
        payload["state"]["melodies"] = melodies
        with pytest.raises(ClientUnavailable) as failure:
            await read_observation(json.dumps(payload))
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    for bad in (["boom"], ["wah", "wah"]):
        asyncio.run(read(bad))
        with pytest.raises(ValidationError):
            RunObservation.model_validate_json(json.dumps({"melodies": bad}))


def test_combat_active_relics_tracks_pulsing_relics_and_refreshes_on_turn_change() -> None:
    async def read() -> None:
        context = Context(DecisionOwner.COMBAT)
        payload = json.loads(COMBAT)
        payload["state"]["turn"] = 1
        payload["state"]["relics"] = ["r1", "r2", "r3", "r4"]
        payload["objects"]["r1"] = {
            "kind": "relic",
            "definition_id": "RELIC.PAELS_EYE",
            "name": "Pael's Eye",
            "status": RelicStatus.ACTIVE,
            "is_used_up": False,
            "is_melted": False,
            "stack_count": 1,
        }
        payload["objects"]["r2"] = {
            "kind": "relic",
            "definition_id": "RELIC.PEN_NIB",
            "name": "Pen Nib",
            "counter": 9,
            "status": RelicStatus.ACTIVE,
            "is_used_up": False,
            "is_melted": False,
            "stack_count": 1,
        }
        payload["objects"]["r3"] = {
            "kind": "relic",
            "definition_id": "RELIC.WAX_RELIC",
            "name": "Melted Relic",
            "status": RelicStatus.ACTIVE,
            "is_used_up": False,
            "is_melted": True,
            "stack_count": 1,
        }
        payload["objects"]["r4"] = {
            "kind": "relic",
            "definition_id": "RELIC.SONG_BOW",
            "name": "Song Bow",
            "status": RelicStatus.NORMAL,
            "is_used_up": False,
            "is_melted": False,
            "stack_count": 1,
        }
        payload["descriptions"]["r1"] = "Extra turn if no cards played."
        payload["descriptions"]["r2"] = "Every 10th attack deals double damage."
        payload["descriptions"]["r3"] = "Melted."
        payload["descriptions"]["r4"] = "Start with 6 Aim."

        # Turn 1 baseline: active_relics includes non-melted active relics without redundant status.
        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        turn1 = json.loads(context.model_input()[-1].text)["observation"]
        assert turn1["active_relics"] == [
            {"id": "RELIC.PAELS_EYE", "name": "Pael's Eye"},
            {"id": "RELIC.PEN_NIB", "name": "Pen Nib", "counter": 9},
        ]

        # Same turn, no change to active_relics -> omitted from diff.
        payload["stamp"]["observation_sequence"] += 1
        payload["state"]["player"]["block"] = 5
        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        same_turn = json.loads(context.model_input()[-1].text)["observation"]
        assert "active_relics" not in same_turn

        # Turn 2 with identical active_relics -> force re-emitted on turn change.
        payload["stamp"]["observation_sequence"] += 1
        payload["state"]["turn"] = 2
        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        turn2 = json.loads(context.model_input()[-1].text)["observation"]
        assert turn2["active_relics"] == [
            {"id": "RELIC.PAELS_EYE", "name": "Pael's Eye"},
            {"id": "RELIC.PEN_NIB", "name": "Pen Nib", "counter": 9},
        ]

        # Mid-turn deactivation -> emits empty list [].
        payload["stamp"]["observation_sequence"] += 1
        payload["objects"]["r1"]["status"] = RelicStatus.NORMAL
        payload["objects"]["r2"]["status"] = RelicStatus.NORMAL
        payload["objects"]["r2"]["counter"] = 0
        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        deactivated = json.loads(context.model_input()[-1].text)["observation"]
        assert deactivated["active_relics"] == []

        # Turn 3 while still empty -> not redundantly re-emitted.
        payload["stamp"]["observation_sequence"] += 1
        payload["state"]["turn"] = 3
        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        turn3 = json.loads(context.model_input()[-1].text)["observation"]
        assert "active_relics" not in turn3

    asyncio.run(read())
