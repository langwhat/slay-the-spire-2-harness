import json

from test_mod_client import COMBAT

from sts2_harness.client.models import DecisionOwner, Observation
from sts2_harness.context import CombatActInput, Context, QueryToolResult, ReadCompleted
from sts2_harness.llm.messages import ToolCall


def test_combat_delivers_complete_description_independently_of_cost() -> None:
    original = Observation.model_validate_json(COMBAT)
    card = original.objects["c1"]
    original = original.model_copy(update={"descriptions": {"c1": "Deal 6 damage. Draw 1 card."}})
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), original)
    first = json.loads(context.model_input()[-1].text)["observation"]
    assert first["current"]["card_descriptions"][0]["text"] == "Deal 6 damage. Draw 1 card."
    assert {item["ref"] for item in first["current"]["piles"]["hand"]} == {"c1", "c2"}
    changed = original.model_copy(
        update={"objects": {"c1": card.model_copy(update={"playable": False})}}
    )
    context.append_tool_result(
        ToolCall("q", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), changed)
    )
    second = json.loads(context.model_input()[-1].text)["observation"]
    assert "card_descriptions" not in second.get("current", {})
    assert second["current"]["cards"][0]["playable"] is False
    assert "catalog_additions" not in second


def test_combat_preserves_live_facts_previews_and_complete_descriptions() -> None:
    raw = json.loads(COMBAT)
    raw["descriptions"]["c1"] = "Deal 6 damage. Draw 1 card."
    raw["objects"]["c1"]["local_keywords"] = []
    raw["objects"]["c1"]["has_turn_end_in_hand_effect"] = None
    raw["objects"]["c2"]["local_keywords"] = ["LOCAL"]
    observation = Observation.model_validate_json(json.dumps(raw))
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    payload = json.loads(context.model_input()[-1].text)["observation"]
    current, decision = payload["current"], payload["decision"]
    assert current["player"]["resources"] == {"energy": 0, "stars": None}
    assert current["actors"]["enemies"][0]["intents"][1]["damage_per_hit"] is None
    assert current["orbs"]["slots"][0]["passive"] == 0
    assert current["deck"][0]["ref"] == "c1"
    assert decision["selection"]["selected"] == []
    previews = {item["ref"]: item["previews"] for item in decision["hand_previews"]}
    assert previews["c1"][0]["target"] == {
        "ref": "e1",
        "name": "敌人",
        "definition_id": "ENEMY.ONE",
    }
    cards = {item["ref"]: item for item in current["cards"]}
    assert "previews" not in cards["c1"]
    assert "local_keywords" not in cards["c1"]
    assert cards["c2"]["local_keywords"] == ["LOCAL"]
    assert "should_retain_this_turn" not in cards["c1"]
    assert cards["c1"]["has_turn_end_in_hand_effect"] is None
    assert cards["c1"]["enchantment"]["amount"] == 0
    assert "affliction" not in cards["c1"]
    assert current["modifier_descriptions"][0]["enchantment"] == "附魔说明"
    assert "affliction" not in current["modifier_descriptions"][0]
    assert "should_retain_this_turn" not in current["upgrades"][0]["card"]
    assert current["generated"][0]["source"]["ref"] == "c1"
    assert current["generated"][0]["previews"][0]["source"]["definition_id"] == "CARD.ONE"
    assert current["card_descriptions"][0]["text"] == "Deal 6 damage. Draw 1 card."
    assert len(current["mechanisms"]) == 2
    assert current["mechanisms"][0]["owner"]["ref"] == "player"
    assert current["mechanisms"][1]["owner"]["definition_id"] == "ENEMY.ONE"


def test_description_replacement_null_and_node_scope() -> None:
    raw = json.loads(COMBAT)
    raw["descriptions"]["c1"] = "Deal 6 damage. Draw 1 card."
    observation = Observation.model_validate_json(json.dumps(raw))
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("q", "combat_inspect", "{}")
    context.append_task(CombatActInput(), observation)
    for text in ("Deal 8 damage. Draw 1 card.", None, "Deal 9 damage. Draw 1 card."):
        observation = observation.model_copy(
            update={"descriptions": {**observation.descriptions, "c1": text}}
        )
        context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
        payload = json.loads(context.model_input()[-1].text)["observation"]
        assert payload["current"]["card_descriptions"] == [
            {"ref": "c1", "definition_id": "CARD.ONE", "name": "牌", "text": text}
        ]
        assert "catalog_additions" not in payload
    observation = observation.model_copy(
        update={"location": observation.location.model_copy(update={"node_id": "next"})}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    payload = json.loads(context.model_input()[-1].text)["observation"]
    assert payload["scene"]["node_id"] == "next"
    assert len(payload["current"]["card_descriptions"]) == 2
    assert payload["current"]["deck"][0]["ref"] == "c1"


def test_actor_descriptions_remain_visible_only_to_combat() -> None:
    from sts2_harness.context import RunActInput

    raw = json.loads(COMBAT)
    enemy = dict(raw["state"]["enemies"][0])
    enemy.pop("ref")
    raw["objects"]["e1"] = {"kind": "enemy", **enemy}
    raw["descriptions"]["e1"] = "The enemy changes its intent."
    observation = Observation.model_validate_json(json.dumps(raw))
    combat = Context(DecisionOwner.COMBAT)
    combat.append_task(CombatActInput(), observation)
    payload = json.loads(combat.model_input()[-1].text)["observation"]
    assert payload["current"]["object_descriptions"] == [
        {
            "ref": "e1",
            "definition_id": "ENEMY.ONE",
            "name": "敌人",
            "text": "The enemy changes its intent.",
        }
    ]
    run = Context(DecisionOwner.RUN)
    run.append_task(RunActInput(), observation)
    payload = json.loads(run.model_input()[-1].text)["observation"]
    assert payload["current"]["object_descriptions"] == []
