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


def test_combat_needs_target_selection_cost_and_power_hovertip_dedup() -> None:
    async def read() -> None:
        context = Context(DecisionOwner.COMBAT)
        payload = json.loads(COMBAT)
        payload["descriptions"]["c1"] = "Forge 3. Double Sovereign Blade damage against enemy."
        payload["objects"]["c1"]["needs_target"] = True
        payload["objects"]["c1"]["previews"] = [
            {"target": "e1", "text": payload["descriptions"]["c1"]}
        ]
        c_disc = {
            **payload["objects"]["c1"],
            "definition_id": "CARD.DEVASTATE",
            "name": "Devastate",
            "cost": {"energy": 1, "stars": 4},
            "playable": None,
            "needs_target": None,
        }
        c_disc.pop("previews", None)
        payload["objects"]["c_disc"] = c_disc
        payload["descriptions"]["c_disc"] = "Deal 30 damage."
        payload["state"]["piles"]["discard"] = ["c_disc"]
        payload["state"]["selection"] = {
            "prompt": "Choose a card",
            "source": None,
            "min": 1,
            "max": 1,
            "candidates": ["c_disc"],
            "selected": [],
        }
        payload["mechanisms"] = [
            {
                "kind": "other",
                "definition_id": "POWER.PLATING_POWER",
                "name": "Plating",
                "text": "Type: Buff\nAt end of turn: gain stacks Block.",
                "owner": "player",
            },
            {
                "kind": "power",
                "definition_id": "POWER.PLATING_POWER",
                "name": "Plating",
                "text": "At end of turn, gain 4 Block.",
                "owner": "player",
            },
            {
                "kind": "keyword",
                "definition_id": None,
                "name": "Plating",
                "text": "Unrelated non-power keyword sharing a localized title.",
                "owner": "c1",
            },
            {
                "kind": "power",
                "definition_id": "POWER.TORIC_TOUGHNESS_POWER",
                "name": "Toric Toughness",
                "text": "At the start of your next turn, gain 5 Block.",
                "owner": "player",
            },
        ]

        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        first = json.loads(context.model_input()[-1].text)["observation"]

        # Hand card with identical target previews still exposes needs_target=true.
        c1_row = next(row for row in first["hand"] if row["ref"] == "c1")
        assert c1_row["needs_target"] is True
        assert "previews" not in c1_row
        # Combat selection candidate outside hand always exposes its current cost.
        assert first["choice"]["selection"]["candidates"][0]["cost"] == {"energy": 1, "stars": 4}
        # Exact-ID power HoverTip is suppressed when POWER.* rule is complete, while
        # same-title keywords and uncovered powers are preserved.
        mech_keys = [(m["kind"], m.get("definition_id"), m["name"]) for m in first["mechanisms"]]
        assert ("other", "POWER.PLATING_POWER", "Plating") in mech_keys
        assert ("power", "POWER.PLATING_POWER", "Plating") not in mech_keys
        assert ("keyword", None, "Plating") in mech_keys
        assert ("power", "POWER.TORIC_TOUGHNESS_POWER", "Toric Toughness") in mech_keys

        # Next turn: live stack tick on Plating power HoverTip is still suppressed!
        payload["stamp"]["observation_sequence"] += 1
        payload["state"]["turn"] = 2
        payload["mechanisms"][1]["text"] = "At end of turn, gain 3 Block."
        observed2 = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed2)
        second = json.loads(context.model_input()[-1].text)["observation"]
        assert "mechanisms" not in second

    asyncio.run(read())


def test_run_shop_entry_refreshes_deck_stats_even_when_unchanged() -> None:
    from pydantic import JsonValue
    from test_mod_client import RUN

    from sts2_harness.context import RunActInput
    from sts2_harness.llm.messages import ToolCall
    from sts2_harness.results import QueryToolResult, ReadCompleted

    async def read() -> None:
        context = Context(DecisionOwner.RUN)
        stats: dict[str, JsonValue] = {
            "cards": 30,
            "circulating": 22,
            "first_pass_turns": 6.0,
            "cycle_turns": 4.4,
        }
        context.set_deck_stats(stats, {})

        map_payload = json.loads(RUN)
        map_obs = (await read_observation(json.dumps(map_payload))).observation
        context.append_task(RunActInput(), map_obs)
        first = json.loads(context.model_input()[-1].text)["observation"]
        assert first["deck_stats"] == stats

        # Tool result still on map with identical deck_stats omits it.
        map_payload["stamp"]["observation_sequence"] += 1
        map_obs2 = (await read_observation(json.dumps(map_payload))).observation
        context.append_tool_result(
            ToolCall("call-1", "run_inspect", "{}"),
            QueryToolResult(ReadCompleted(), map_obs2),
        )
        second = json.loads(context.model_input()[-1].text)["observation"]
        assert "deck_stats" not in second

        # Transitioning into Shop force-refreshes deck_stats once on entry.
        shop_payload = json.loads(RUN)
        shop_payload["stamp"]["observation_sequence"] += 2
        shop_payload["state"]["screen"] = "shop"
        shop_obs = (await read_observation(json.dumps(shop_payload))).observation
        context.append_tool_result(
            ToolCall("call-2", "run_inspect", "{}"),
            QueryToolResult(ReadCompleted(), shop_obs),
        )
        on_shop_entry = json.loads(context.model_input()[-1].text)["observation"]
        assert on_shop_entry["deck_stats"] == stats

        # Subsequent tool result still in Shop omits unchanged deck_stats.
        shop_payload["stamp"]["observation_sequence"] += 3
        shop_obs2 = (await read_observation(json.dumps(shop_payload))).observation
        context.append_tool_result(
            ToolCall("call-3", "run_inspect", "{}"),
            QueryToolResult(ReadCompleted(), shop_obs2),
        )
        in_shop_again = json.loads(context.model_input()[-1].text)["observation"]
        assert "deck_stats" not in in_shop_again

    asyncio.run(read())
