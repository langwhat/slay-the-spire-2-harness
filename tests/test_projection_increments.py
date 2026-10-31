import json

import pytest
from test_combat_compaction import action
from test_mod_client import COMBAT, RUN

from sts2_harness.client.models import (
    CardEntry,
    DecisionOwner,
    Observation,
    PublicMap,
    ResourceCosts,
)
from sts2_harness.context import CombatActInput, Context, QueryToolResult, ReadCompleted
from sts2_harness.context.models import AutomaticProgress, RunActInput
from sts2_harness.llm.messages import ToolCall
from sts2_harness.results import ActionCompleted, ActionToolResult


def test_one_card_cost_change_does_not_repeat_unchanged_cards() -> None:
    observation = Observation.model_validate_json(COMBAT)
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    changed = observation.model_copy(
        update={
            "objects": {
                **observation.objects,
                "c1": card.model_copy(update={"cost": ResourceCosts(energy=0)}),
            }
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    context.begin_group(observation, "")
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), changed)
    )
    increment = json.loads(context.model_input()[-1].text)["observation"]
    assert "scene" not in increment
    cards = json.dumps(increment["current"]["cards"])
    assert '"c1"' in cards
    assert '"c2"' not in cards, "A cost change must not resend an unchanged card."


def test_run_group_resource_change_keeps_unchanged_entries_incremental() -> None:
    raw = json.loads(RUN)
    cards = json.loads(COMBAT)
    for key in (
        "objects",
        "descriptions",
        "modifier_descriptions",
        "generated",
        "mechanisms",
        "map",
    ):
        raw[key] = cards[key]
    raw["state"]["deck"] = ["c1", "c2"]
    opening = Observation.model_validate_json(json.dumps(raw))
    raw["state"]["player"]["gold"] = 1
    changed = Observation.model_validate_json(json.dumps(raw))
    context = Context(DecisionOwner.RUN)
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    context.finish_group()
    context.begin_group(changed, "")
    context.append_task(RunActInput(), changed)
    frame = json.loads(context.model_input()[-1].text)["observation"]
    assert "scene" not in frame
    assert set(frame["current"]) == {"player"}
    assert frame["current"]["player"]["gold"] == 1
    context.finish_group()
    baseline = json.loads(context.compaction_candidate()[0].text)["context_baseline"]
    assert baseline["observation"]["current"]["player"]["gold"] == opening.state.player.gold
    assert len(baseline["observation"]["current"]["cards"]) == 2
    context.install_compaction(context.compaction_candidate())
    context.append_task(RunActInput(strategy_notes="Keep current strategy."), changed)
    assert "current" not in json.loads(context.model_input()[-1].text)["observation"]


def test_run_group_mechanism_change_does_not_resend_untouched_state() -> None:
    raw = json.loads(RUN)
    combat = json.loads(COMBAT)
    for key in ("objects", "descriptions", "mechanisms"):
        raw[key] = combat[key]
    raw["state"]["deck"] = ["c1", "c2"]
    opening = Observation.model_validate_json(json.dumps(raw))
    raw["mechanisms"].append(raw["mechanisms"][0])
    changed = Observation.model_validate_json(json.dumps(raw))
    context = Context(DecisionOwner.RUN)
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    context.finish_group()
    context.begin_group(changed, "")
    context.append_task(RunActInput(), changed)
    wire = json.loads(context.model_input()[-1].text)["observation"]
    assert "scene" not in wire
    assert set(wire["current"]) == {"mechanisms"}
    assert len(wire["current"]["mechanisms"]) == 1
    context.install_compaction(context.compaction_candidate())
    context.install_compaction(context.compaction_candidate())


def test_run_action_menu_is_retained_until_changed_and_rebuilt_in_baselines() -> None:
    opening = Observation.model_validate_json(RUN)
    raw = json.loads(RUN)
    raw["state"]["actions"] = []
    cleared = Observation.model_validate_json(json.dumps(raw))
    context = Context(DecisionOwner.RUN)
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    menu = json.loads(context.model_input()[-1].text)["observation"]["decision"]["actions"]
    context.finish_group()
    context.begin_group(opening, "")
    context.append_task(RunActInput(strategy_notes="Keep current strategy."), opening)
    assert "actions" not in json.loads(context.model_input()[-1].text)["observation"]["decision"]
    context.finish_group()
    candidate = context.compaction_candidate()
    baseline = json.loads(candidate[0].text)["context_baseline"]["observation"]
    assert baseline["decision"]["actions"] == menu
    context.install_compaction(candidate)
    context.append_task(RunActInput(), cleared)
    assert json.loads(context.model_input()[-1].text)["observation"]["decision"]["actions"] == []
    context.append_task(RunActInput(), opening)
    assert json.loads(context.model_input()[-1].text)["observation"]["decision"]["actions"] == menu
    next_node = opening.model_copy(
        update={"location": opening.location.model_copy(update={"node_id": "next-node"})}
    )
    context.append_task(RunActInput(), next_node)
    assert json.loads(context.model_input()[-1].text)["observation"]["decision"]["actions"] == menu


def test_run_group_card_updates_and_removals_preserve_profile_closure() -> None:
    raw = json.loads(RUN)
    raw["objects"] = json.loads(COMBAT)["objects"]
    raw["state"]["deck"] = ["c1", "c2"]
    context = Context(DecisionOwner.RUN)
    opening = Observation.model_validate_json(json.dumps(raw))
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    context.finish_group()
    raw["objects"]["c1"]["cost"] = {"energy": 0}
    for ref in ("c1", "c3"):
        if ref == "c3":
            raw["objects"][ref] = {**raw["objects"]["c1"], "definition_id": "CARD.NEW"}
            raw["state"]["deck"].append(ref)
        observation = Observation.model_validate_json(json.dumps(raw))
        context.begin_group(observation, "")
        context.append_task(RunActInput(), observation)
        wire = json.loads(context.model_input()[-1].text)["observation"]
        assert "reset" not in wire.get("scene", {})
        assert [card["ref"] for card in wire["current"]["cards"]] == [ref]
        context.finish_group()
        context.install_compaction(context.compaction_candidate())
    del raw["objects"]["c3"]
    raw["state"]["deck"].remove("c3")
    observation = Observation.model_validate_json(json.dumps(raw))
    context.begin_group(observation, "")
    context.append_task(RunActInput(), observation)
    wire = json.loads(context.model_input()[-1].text)["observation"]
    assert "reset" not in wire["scene"]
    assert wire["scene"]["removed"] == ["c3"]
    assert "cards" not in wire["current"]
    assert wire["current"]["removed"]["cards"] == ["c3"]
    context.finish_group()
    context.install_compaction(context.compaction_candidate())


def test_new_card_relations_are_incremental_and_removal_is_explicit() -> None:
    opening = Observation.model_validate_json(COMBAT)
    changed = opening.model_copy(
        update={
            "objects": {**opening.objects, "c3": opening.objects["c1"]},
            "descriptions": {**opening.descriptions, "c3": "New card text."},
            "modifier_descriptions": {
                **opening.modifier_descriptions,
                "c3": opening.modifier_descriptions["c1"],
            },
            "generated": {**opening.generated, "c3": opening.generated["c1"]},
            "mechanisms": (
                *opening.mechanisms,
                opening.mechanisms[0].model_copy(update={"owner": "c3"}),
            ),
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), opening)
    call = ToolCall("inspect", "combat_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), changed))
    added = json.loads(context.model_input()[-1].text)["observation"]
    assert [entry["ref"] for entry in added["scene"]["bindings"]] == ["c3"]
    for section in ("cards", "card_descriptions", "modifier_descriptions"):
        assert [entry["ref"] for entry in added["current"][section]] == ["c3"]
    assert [entry["source"]["ref"] for entry in added["current"]["generated"]] == ["c3"]
    assert [entry["owner"]["ref"] for entry in added["current"]["mechanisms"]] == ["c3"]
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), opening))
    removed = json.loads(context.model_input()[-1].text)["observation"]
    assert removed["scene"]["bindings"] == []
    assert removed["scene"]["removed"] == ["c3"]
    for section in ("cards", "card_descriptions", "modifier_descriptions", "generated"):
        assert section not in removed["current"]
        assert removed["current"]["removed"][section] == ["c3"]
    assert removed["current"]["removed"]["mechanisms"] == added["current"]["mechanisms"]


def test_node_change_keeps_unchanged_act_map_through_compaction() -> None:
    observation = Observation.model_validate_json(RUN).model_copy(
        update={
            "map": PublicMap.model_validate_json(
                '{"bosses":[],"nodes":[{"position":"map:0:0",'
                '"room_type":"ancient","children":[]}],"legal_destinations":[]}'
            )
        }
    )
    next_node = observation.model_copy(
        update={"location": observation.location.model_copy(update={"node_id": "next-node"})}
    )
    context = Context(DecisionOwner.RUN)
    context.begin_group(observation, "")
    context.append_task(RunActInput(), observation)
    first = json.loads(context.model_input()[-1].text)["observation"]
    assert first["current"]["map_topology"]
    context.finish_group()
    context.begin_group(observation, "")
    context.append_task(RunActInput(), next_node)
    changed = json.loads(context.model_input()[-1].text)["observation"]
    assert "map_topology" not in changed.get("current", {})
    assert "dynamic_map" not in changed.get("current", {})
    context.finish_group()
    context.begin_group(next_node, "")
    context.install_compaction(context.compaction_candidate())


def test_run_group_retains_only_unchanged_same_act_map() -> None:
    raw = json.loads(RUN)
    raw["map"] = {"bosses": [], "nodes": [], "legal_destinations": []}
    context = Context(DecisionOwner.RUN)
    opening = Observation.model_validate_json(json.dumps(raw))
    context.append_task(RunActInput(), opening)
    raw["mechanisms"] = json.loads(COMBAT)["mechanisms"]
    changed = Observation.model_validate_json(json.dumps(raw))
    context.begin_group(changed, "")
    context.append_task(RunActInput(), changed)
    frame = json.loads(context.model_input()[-1].text)["observation"]
    assert "scene" not in frame
    assert "dynamic_map" not in frame["current"]
    candidate = context.compaction_candidate()
    baseline = json.loads(candidate[0].text)["context_baseline"]["observation"]
    assert baseline["current"]["dynamic_map"]["legal_destinations"] == []
    context.install_compaction(candidate)
    raw["map"]["legal_destinations"] = None
    context.append_task(RunActInput(), Observation.model_validate_json(json.dumps(raw)))
    frame = json.loads(context.model_input()[-1].text)["observation"]
    assert frame["current"]["dynamic_map"]["legal_destinations"] is None
    raw["state"]["progress"]["act"] = 2
    context.append_task(RunActInput(), Observation.model_validate_json(json.dumps(raw)))
    frame = json.loads(context.model_input()[-1].text)["observation"]
    assert "dynamic_map" in frame["current"]


def test_first_group_compaction_preserves_mechanism_multiplicity() -> None:
    opening = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.begin_group(opening, "")
    context.append_task(CombatActInput(), opening)
    first = json.loads(context.model_input()[-1].text)["observation"]
    assert first["scene"]["reset"] is True
    baseline = json.loads(context.compaction_candidate()[0].text)["context_baseline"]
    assert baseline["observation"]["scene"]["reset"] is True
    context.install_compaction(context.compaction_candidate())
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), opening)
    )
    assert "current" not in json.loads(context.model_input()[-1].text)["observation"]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_changed_group_entry_rebuilds_mechanisms_before_compaction(count: int) -> None:
    original = Observation.model_validate_json(COMBAT)
    opening = original.model_copy(update={"mechanisms": (original.mechanisms[0],) * 2})
    changed = original.model_copy(update={"mechanisms": (original.mechanisms[0],) * count})
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), opening)
    context.begin_group(changed, "")
    context.append_task(CombatActInput(), changed)
    frame = json.loads(context.model_input()[-1].text)["observation"]
    assert frame["scene"]["reset"] is True
    assert len(frame["current"]["mechanisms"]) == count
    context.install_compaction(context.compaction_candidate())
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), changed)
    )
    assert "current" not in json.loads(context.model_input()[-1].text)["observation"]


def test_automatic_group_keeps_delivered_snapshot_as_delta_origin() -> None:
    opening = Observation.model_validate_json(RUN)
    before = opening.model_copy(
        update={
            "state": opening.state.model_copy(
                update={"player": opening.state.player.model_copy(update={"hp": 7})}
            )
        }
    )
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), opening)
    context.begin_group(before, "")
    context.append_automatic(
        AutomaticProgress(label="Continue", reason="Only one destination."),
        ActionToolResult(ActionCompleted(completed_steps=1), opening),
    )
    retained = context.model_input()[-1]
    candidate = context.compaction_candidate()
    baseline = json.loads(candidate[0].text)["context_baseline"]["observation"]
    assert baseline["current"]["player"]["hp"] == 10
    assert candidate[-1] == retained
    assert "scene" not in json.loads(retained.text)["observation"]
    context.install_compaction(candidate)


def test_automatic_group_keeps_delivered_menu_when_unseen_menu_changes() -> None:
    opening = Observation.model_validate_json(RUN)
    before = opening.model_copy(update={"state": opening.state.model_copy(update={"actions": ()})})
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), opening)
    context.begin_group(before, "")
    context.append_automatic(
        AutomaticProgress(label="Continue", reason="Only one destination."),
        ActionToolResult(ActionCompleted(completed_steps=1), opening),
    )
    candidate = context.compaction_candidate()
    baseline = json.loads(candidate[0].text)["context_baseline"]["observation"]
    assert baseline["decision"]["actions"]
    context.install_compaction(candidate)
    result = json.loads(context.model_input()[-1].text)["observation"]
    assert "actions" not in result["decision"]


def test_compaction_preserves_entry_removals_defaults_and_mechanism_multiplicity() -> None:
    original = Observation.model_validate_json(COMBAT)
    card = original.objects["c1"]
    assert isinstance(card, CardEntry)
    opening = original.model_copy(
        update={
            "objects": {
                **original.objects,
                "c1": card.model_copy(update={"should_retain_this_turn": True}),
            },
            "mechanisms": (*original.mechanisms, original.mechanisms[0]),
        }
    )
    changed = original.model_copy(
        update={
            "descriptions": {"c2": original.descriptions["c2"]},
            "mechanisms": (original.mechanisms[0],),
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.begin_group(opening, "")
    context.append_task(CombatActInput(), opening)
    action(context, "a0", opening)
    context.finish_group()
    context.begin_group(opening, "")
    context.append_task(CombatActInput(), opening)
    action(context, "a1", changed)
    increment = json.loads(context.model_input()[-1].text)["observation"]["current"]
    assert [entry["ref"] for entry in increment["cards"]] == ["c1"]
    assert "should_retain_this_turn" not in increment["cards"][0]
    assert increment["removed"]["card_descriptions"] == ["c1"]
    assert len(increment["removed"]["mechanisms"]) == 2
    assert "mechanisms" not in increment
    context.finish_group()
    context.begin_group(changed, "")
    context.append_task(CombatActInput(), changed)
    context.install_compaction(context.compaction_candidate())
    action(context, "a2", opening)
    restored = json.loads(context.model_input()[-1].text)["observation"]["current"]
    assert restored["cards"][0]["should_retain_this_turn"] is True
    assert restored["card_descriptions"] == [
        {"ref": "c1", "name": card.name, "definition_id": card.definition_id, "text": None}
    ]
    assert len(restored["mechanisms"]) == 2
    context.finish_group()
    context.begin_group(opening, "")
    context.append_task(CombatActInput(), opening)
    candidate = context.compaction_candidate()
    baseline = json.loads(candidate[0].text)["context_baseline"]["observation"]["current"]
    assert [entry["ref"] for entry in baseline["cards"]] == ["c1", "c2"]
    assert [entry["ref"] for entry in baseline["card_descriptions"]] == ["c2"]
    assert len(baseline["mechanisms"]) == 1
    context.install_compaction(candidate)
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), opening)
    )
    assert "current" not in json.loads(context.model_input()[-1].text)["observation"]
