import json

from pydantic import MISSING
from test_mod_client import COMBAT

from sts2_harness.client.models import CardEntry, CombatState, DecisionOwner, Observation
from sts2_harness.context.run_projection import RunDelivery
from sts2_harness.context.view import visible_observation


def test_hand_changes_do_not_resend_card_details_or_other_piles() -> None:
    original = Observation.model_validate_json(COMBAT)
    delivery = RunDelivery(role=DecisionOwner.COMBAT)
    first = delivery.project(visible_observation(original, DecisionOwner.COMBAT), original)
    assert first.current is not MISSING
    first_json = json.loads(first.to_json())
    assert [entry["ref"] for entry in first_json["current"]["hand"]] == ["c2", "c1"]
    card = original.objects["c1"]
    assert isinstance(card, CardEntry)
    changed = original.model_copy(
        update={"objects": {**original.objects, "c1": card.model_copy(update={"playable": False})}}
    )
    second = delivery.project(visible_observation(changed, DecisionOwner.COMBAT), changed)
    current = json.loads(second.to_json())["current"]
    assert set(current) == {"hand"}
    assert current["hand"][1]["playable"] is False
    assert "previews" in current["hand"][1]
    assert "hand_previews" not in json.loads(second.to_json())["decision"]
    state = changed.state
    assert isinstance(state, CombatState)
    moved = changed.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "piles": state.piles.model_copy(update={"hand": ("c2",), "discard": ("c1",)})
                }
            )
        }
    )
    third = json.loads(
        delivery.project(visible_observation(moved, DecisionOwner.COMBAT), moved).to_json()
    )
    assert set(third["current"]) == {"hand", "discard", "cards"}
    assert third["current"]["cards"][0]["playable"] is False
    assert [entry["ref"] for entry in third["current"]["discard"]] == ["c1"]
    assert "cost" not in third["current"]["discard"][0]


def test_hand_preserves_preview_authority_and_explicit_empty() -> None:
    original = Observation.model_validate_json(COMBAT)
    card = original.objects["c1"]
    assert isinstance(card, CardEntry)
    original = original.model_copy(
        update={"objects": {**original.objects, "c1": card.model_copy(update={"previews": ()})}}
    )
    delivery = RunDelivery(role=DecisionOwner.COMBAT)
    first = json.loads(
        delivery.project(visible_observation(original, DecisionOwner.COMBAT), original).to_json()
    )
    assert first["current"]["hand"][1]["previews"] == []
    foreign = original.model_copy(
        update={"location": original.location.model_copy(update={"owner": DecisionOwner.RUN})}
    )
    changed = json.loads(
        delivery.project(visible_observation(original, DecisionOwner.COMBAT), foreign).to_json()
    )
    assert all("previews" not in entry for entry in changed["current"]["hand"])
    assert "cards" not in changed["current"]


def test_hand_preserves_missing_null_false_and_clears_on_phase_exit() -> None:
    from test_mod_client import RUN

    original = Observation.model_validate_json(COMBAT)
    card = original.objects["c1"]
    assert isinstance(card, CardEntry)
    delivery = RunDelivery(role=DecisionOwner.COMBAT)
    for playable in (MISSING, None, False):
        changed = original.model_copy(
            update={
                "objects": {
                    **original.objects,
                    "c1": card.model_copy(update={"playable": playable, "previews": MISSING}),
                }
            }
        )
        projected = delivery.project(visible_observation(changed, DecisionOwner.COMBAT), changed)
        assert projected.current is not MISSING and projected.current.hand is not MISSING
        assert projected.current.hand is not None
        assert projected.current.hand[1].playable is playable
        assert projected.current.hand[1].previews is MISSING
        if projected.current.cards is not MISSING:
            assert all(item.playable is MISSING for item in projected.current.cards)
    run = Observation.model_validate_json(RUN)
    run = run.model_copy(update={"location": original.location})
    cleared = delivery.project(visible_observation(run, DecisionOwner.COMBAT), run)
    assert cleared.current is not MISSING
    assert cleared.current.hand is None
    assert cleared.current.draw is None and cleared.current.discard is None
    assert cleared.current.exhaust is None and cleared.current.other is None
    fresh = delivery.project(visible_observation(original, DecisionOwner.COMBAT), original)
    assert fresh.current is not MISSING
    assert fresh.current.hand is not MISSING and fresh.current.hand is not None
    assert fresh.current.hand[1].playable is None


def test_hand_afflictions_share_rule_text_without_merging_live_instances() -> None:
    raw = json.loads(COMBAT)
    for ref, amount in (("c1", 0), ("c2", None)):
        raw["objects"][ref]["affliction"] = {
            "definition_id": "AFFLICTION.BOUND",
            "name": "Bound",
            "amount": amount,
            "status": "active",
        }
        raw["modifier_descriptions"][ref] = {
            "enchantment": None,
            "affliction": "Play at most one Bound card per turn.",
        }
    raw["objects"]["c2"]["name"] = "Another card"
    original = Observation.model_validate_json(json.dumps(raw))
    delivery = RunDelivery(role=DecisionOwner.COMBAT)
    payload = json.loads(
        delivery.project(visible_observation(original, DecisionOwner.COMBAT), original).to_json()
    )
    hand = {entry["ref"]: entry for entry in payload["current"]["hand"]}
    first, second = hand["c1"]["affliction"], hand["c2"]["affliction"]
    assert first == {
        "definition_id": "AFFLICTION.BOUND",
        "name": "Bound",
        "amount": 0,
        "status": "active",
        "profile": first["profile"],
    }
    assert second["amount"] is None
    assert first["profile"] == second["profile"]
    assert payload["catalog_additions"][first["profile"]]["content"] == {
        "kind": "affliction",
        "definition_id": "AFFLICTION.BOUND",
        "name": "Bound",
        "text": "Play at most one Bound card per turn.",
    }
    assert all("affliction" not in card for card in payload["current"]["cards"])
    assert all("affliction" not in text for text in payload["current"]["modifier_descriptions"])
    for binding in payload["scene"]["bindings"]:
        profile = payload["catalog_additions"][binding["profile"]]
        if profile["kind"] == "card":
            assert "affliction" not in profile["content"]


def test_hand_affliction_unknown_text_clearing_and_pile_transitions() -> None:
    raw = json.loads(COMBAT)
    modifier = {"definition_id": "AFFLICTION.BOUND", "name": "Bound", "amount": 2}
    raw["objects"]["c1"]["affliction"] = modifier
    raw["modifier_descriptions"]["c1"]["affliction"] = None
    raw["state"]["piles"]["hand"] = ["c2"]
    raw["state"]["piles"]["discard"] = ["c1"]
    delivery = RunDelivery(role=DecisionOwner.COMBAT)

    def project() -> str:
        observation = Observation.model_validate_json(json.dumps(raw))
        return delivery.project(
            visible_observation(observation, DecisionOwner.COMBAT), observation
        ).to_json()

    opening = json.loads(project())
    assert opening["current"]["cards"][0]["affliction"] == modifier
    raw["state"]["piles"]["hand"] = ["c2", "c1"]
    raw["state"]["piles"]["discard"] = []
    entered = json.loads(project())
    current = entered["current"]
    afflicted = current["hand"][1]["affliction"]
    assert entered["catalog_additions"][afflicted["profile"]]["content"]["text"] is None
    assert "affliction" not in current["cards"][0]
    assert current["modifier_descriptions"][0]["enchantment"] == "附魔说明"
    assert "affliction" not in current["modifier_descriptions"][0]
    del raw["modifier_descriptions"]["c1"]
    missing = json.loads(project())
    assert "profile" not in missing["current"]["hand"][1]["affliction"]
    raw["objects"]["c1"]["affliction"] = None
    cleared = json.loads(project())
    assert "affliction" not in cleared["current"]["hand"][1]
    raw["objects"]["c1"]["affliction"] = {**modifier, "amount": 0, "status": "inactive"}
    raw["state"]["piles"]["hand"] = ["c2"]
    raw["state"]["piles"]["discard"] = ["c1"]
    left = json.loads(project())
    assert left["current"]["cards"][0]["affliction"] == {
        **modifier,
        "amount": 0,
        "status": "inactive",
    }
