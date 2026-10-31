import json

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
from sts2_harness.context.models import RunActInput
from sts2_harness.llm.messages import ToolCall


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
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), changed)
    )
    increment = json.loads(context.model_input()[-1].text)["observation"]
    cards = json.dumps(increment["current"]["cards"])
    assert '"c1"' in cards
    assert '"c2"' not in cards, "A cost change must not resend an unchanged card."


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
    context.finish_group()
    context.begin_group(next_node, "")
    context.install_compaction(context.compaction_candidate())


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
