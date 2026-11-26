import asyncio
import json

from pydantic import MISSING
from test_mod_client import COMBAT
from test_orb_observation import read_observation

from sts2_harness.client.models import (
    CardDefinition,
    CardDefinitionPreview,
    CardEntry,
    CombatState,
    CostSymbol,
    DecisionOwner,
    NativeCategory,
    ResourceCosts,
    UpgradeNotUpgradable,
    UpgradeStatus,
)
from sts2_harness.context import CombatActInput, Context
from sts2_harness.knowledge import KnowledgeEntry


def test_is_sly_this_turn_omitted_when_false_and_emitted_when_true() -> None:
    async def read() -> None:
        context = Context(DecisionOwner.COMBAT)
        payload = json.loads(COMBAT)
        payload["objects"]["c1"]["is_sly_this_turn"] = False
        payload["objects"]["c2"]["is_sly_this_turn"] = True

        observed = (await read_observation(json.dumps(payload))).observation
        assert isinstance(observed.state, CombatState)
        c1 = observed.objects["c1"]
        c2 = observed.objects["c2"]
        assert isinstance(c1, CardEntry)
        assert isinstance(c2, CardEntry)
        assert c1.is_sly_this_turn is False
        assert c2.is_sly_this_turn is True

        context.append_task(CombatActInput(), observed)
        hand = json.loads(context.model_input()[-1].text)["observation"]["hand"]
        by_ref = {card["ref"]: card for card in hand}
        assert "is_sly_this_turn" not in by_ref["c1"]
        assert by_ref["c2"]["is_sly_this_turn"] is True

        # Clearing single-turn Sly back to false omits it again on the next frame.
        payload["objects"]["c2"]["is_sly_this_turn"] = False
        payload["stamp"]["observation_sequence"] += 1
        next_obs = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), next_obs)
        next_hand = json.loads(context.model_input()[-1].text)["observation"]["hand"]
        next_by_ref = {card["ref"]: card for card in next_hand}
        assert "is_sly_this_turn" not in next_by_ref["c2"]

    asyncio.run(read())


def test_is_sly_this_turn_optional_in_wire_model_and_omitted_when_false_in_knowledge() -> None:
    async def read() -> None:
        observed = (await read_observation(COMBAT)).observation
        c1 = observed.objects["c1"]
        assert isinstance(c1, CardEntry)
        assert c1.is_sly_this_turn is MISSING

    asyncio.run(read())

    preview_false = CardDefinitionPreview(
        definition_id="CARD.HAND_TRICK",
        name="Hand Trick",
        upgrades=0,
        card_type="skill",
        card_pool="POOL.SILENT",
        rarity="uncommon",
        is_upgradable=True,
        is_removable=True,
        keywords=(),
        should_retain_this_turn=False,
        exhaust_on_next_play=False,
        has_turn_end_in_hand_effect=False,
        is_sly_this_turn=False,
        enchantment=None,
        affliction=None,
        cost=ResourceCosts(energy=1, stars=CostSymbol.NONE),
        text="Gain 7 Block. Give a card in your hand Sly this turn.",
    )
    entry_false = KnowledgeEntry(
        definition=CardDefinition(
            category=NativeCategory.CARD,
            definition_id="CARD.HAND_TRICK",
            name="Hand Trick",
            native=preview_false,
            upgrade=UpgradeNotUpgradable(status=UpgradeStatus.NOT_UPGRADABLE),
        )
    )
    encoded_false = json.loads(entry_false.model_dump_json())
    assert "is_sly_this_turn" not in encoded_false

    preview_true = preview_false.model_copy(update={"is_sly_this_turn": True})
    entry_true = KnowledgeEntry(
        definition=CardDefinition(
            category=NativeCategory.CARD,
            definition_id="CARD.HAND_TRICK",
            name="Hand Trick",
            native=preview_true,
            upgrade=UpgradeNotUpgradable(status=UpgradeStatus.NOT_UPGRADABLE),
        )
    )
    encoded_true = json.loads(entry_true.model_dump_json())
    assert encoded_true["is_sly_this_turn"] is True


def test_cost_without_global_effects_preserves_local_star_cost() -> None:
    async def read() -> None:
        context = Context(DecisionOwner.COMBAT)
        payload = json.loads(COMBAT)
        payload["objects"]["c2"]["cost"] = {"energy": 1, "stars": 0}
        payload["objects"]["c2"]["cost_without_global_effects"] = {"energy": 1, "stars": 2}

        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        hand = json.loads(context.model_input()[-1].text)["observation"]["hand"]
        by_ref = {card["ref"]: card for card in hand}
        assert by_ref["c2"]["cost"] == {"energy": 1, "stars": 0}
        assert by_ref["c2"]["cost_without_global_effects"] == {"energy": 1, "stars": 2}

    asyncio.run(read())
