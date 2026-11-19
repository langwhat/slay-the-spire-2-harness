import json
from collections.abc import Mapping, Sequence

from pydantic import MISSING
from test_lifecycle import terminal
from test_mod_client import COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import RunActInput
from sts2_harness.fights import Fight, FightHistory, FightKind


def battle(floor: int, hp: int, turn: int = 1) -> native.Observation:
    raw = json.loads(COMBAT)
    raw["location"]["node_id"] = f"battle-{floor}"
    raw["state"]["progress"].update(floor=floor, room_type="elite")
    raw["state"]["player"].update(hp=hp, max_hp=80)
    raw["state"]["turn"] = turn
    return native.Observation.model_validate_json(json.dumps(raw))


def after(hp: int, floor: int = 1) -> native.Observation:
    raw = json.loads(RUN)
    raw["state"]["player"].update(hp=hp, max_hp=80)
    raw["state"]["progress"]["floor"] = floor
    raw["state"]["battle_turns"] = 4
    return native.Observation.model_validate_json(json.dumps(raw))


def test_native_fight_boundaries_healing_deduplication_and_run_lifetime() -> None:
    history = FightHistory()
    history.observe(after(50))
    assert history.recent == ()
    for floor in range(1, 9):
        history.observe(battle(floor, 50))
        history.observe(battle(floor, 40, 3))
        history.observe(battle(floor, 40, 3))
        settling = after(40, floor)
        history.observe(
            settling.model_copy(
                update={"state": settling.state.model_copy(update={"battle_turns": None})}
            )
        )
        assert all(fight.floor != floor for fight in history.recent)
        history.observe(after(46, floor))
        history.observe(after(46, floor))
        assert history.recent[-1] == Fight(
            floor=floor,
            kind=FightKind.ELITE,
            hp_in=50,
            hp_out=46,
            damage=(0, 0),
            damage_one=(0, 0),
            incoming=(None, 0),
            hp_lost=(10, 0),
            potions=((), ()),
        )
    assert [fight.floor for fight in history.recent] == [3, 4, 5, 6, 7, 8]
    history.observe(battle(9, 46))
    assert len(history.recent) == 6
    history.observe(terminal(battle(9, 12, 2)))
    history.observe(after(12))
    assert history.recent[-1].floor == 8
    history.observe(battle(10, 50))
    history.observe(battle(11, 30))
    history.observe(after(20, 11))
    assert history.recent[-1].floor == 11
    new_run = after(80)
    new_run = new_run.model_copy(
        update={"location": new_run.location.model_copy(update={"run_id": "new-run"})}
    )
    history.observe(new_run)
    assert history.recent == ()


def test_fights_survive_context_replacement_and_stay_in_dynamic_run_observations() -> None:
    history = FightHistory()
    for floor in range(30, 36):
        history.observe(battle(floor, 72, 5))
        history.observe(after(61, floor))
    for minimal in (False, True):
        for _ in range(2):
            context = Context(native.DecisionOwner.RUN, minimal=minimal)
            context.set_fights(history.recent)
            context.append_task(RunActInput(), after(61))
            delivered = json.loads(context.model_input()[-1].text)["observation"]
            assert delivered["fights"] == [json.loads(fight.to_json()) for fight in history.recent]
            encoded = json.dumps({"fights": delivered["fights"]}, separators=(",", ":"))
            assert len(encoded) <= 900
        empty = Context(native.DecisionOwner.RUN, minimal=minimal)
        empty.append_task(RunActInput(), after(80))
        assert "fights" not in empty.model_input()[-1].text


def test_fight_encounter_comes_from_opening_observation() -> None:
    history = FightHistory()
    opening = battle(4, 50).model_copy(
        update={
            "encounter": native.NamedDefinition(name="守卫者", definition_id="ENCOUNTER.GUARDIAN")
        }
    )
    history.observe(opening)
    history.observe(battle(4, 40))
    history.observe(after(37, 4))
    assert json.loads(history.recent[0].to_json()) == {
        "floor": 4,
        "kind": "elite",
        "encounter": {"definition_id": "ENCOUNTER.GUARDIAN", "name": "守卫者"},
        "hp_in": 50,
        "hp_out": 37,
        "damage": [0],
        "damage_one": [0],
        "incoming": [0],
        "hp_lost": [13],
        "potions": [[]],
    }
    context = Context(native.DecisionOwner.RUN)
    context.set_fights(history.recent)
    context.append_task(RunActInput(), after(37, 4))
    delivered = context.model_input()[-1].text
    assert list(json.loads(delivered)["observation"]["fights"][0]) == [
        "floor",
        "kind",
        "encounter",
        "hp_in",
        "hp_out",
        "damage",
        "damage_one",
        "incoming",
        "hp_lost",
        "potions",
    ]
    history.observe(
        battle(5, 37).model_copy(update={"encounter": native.NamedDefinition(name="未知")})
    )
    history.observe(after(31, 5))
    assert history.recent[1].encounter is None


def test_fight_encounter_uses_consistent_later_observation_and_keeps_opening_hp() -> None:
    history = FightHistory()
    encounter = native.NamedDefinition(name="守卫者", definition_id="ENCOUNTER.GUARDIAN")
    history.observe(battle(4, 50))
    history.observe(battle(4, 40, 2).model_copy(update={"encounter": encounter}))
    history.observe(after(37, 4))
    assert history.recent[0].encounter is not None
    assert history.recent[0].encounter.definition_id == "ENCOUNTER.GUARDIAN"
    assert history.recent[0].encounter.name == "守卫者"
    assert history.recent[0].hp_in == 50


def test_fight_encounter_conflicts_are_not_assigned_or_carried_to_another_fight() -> None:
    history = FightHistory()
    first = native.NamedDefinition(name="守卫者", definition_id="ENCOUNTER.GUARDIAN")
    other = native.NamedDefinition(name="另一遭遇", definition_id="ENCOUNTER.OTHER")
    history.observe(battle(4, 50).model_copy(update={"encounter": first}))
    history.observe(battle(4, 40, 2).model_copy(update={"encounter": other}))
    history.observe(after(37, 4))
    assert history.recent[0].encounter is None
    history.observe(battle(5, 37))
    history.observe(after(31, 5))
    assert history.recent[1].encounter is None

    history.observe(battle(6, 31).model_copy(update={"encounter": first}))
    translated = first.model_copy(update={"name": "Guardian"})
    history.observe(battle(6, 29, 2).model_copy(update={"encounter": translated}))
    history.observe(after(27, 6))
    assert history.recent[2].encounter is None

    history.observe(battle(7, 27).model_copy(update={"encounter": first}))
    name_only = native.NamedDefinition(name="另一遭遇")
    history.observe(battle(7, 25, 2).model_copy(update={"encounter": name_only}))
    history.observe(after(23, 7))
    assert history.recent[3].encounter is None

    history.observe(battle(8, 23).model_copy(update={"encounter": first}))
    same_name = native.NamedDefinition(name="守卫者")
    history.observe(battle(8, 21, 2).model_copy(update={"encounter": same_name}))
    history.observe(after(19, 8))
    assert history.recent[4].encounter is not None
    assert history.recent[4].encounter.definition_id == "ENCOUNTER.GUARDIAN"


def test_fight_encounter_does_not_cross_an_interrupted_combat_segment() -> None:
    history = FightHistory()
    history.observe(battle(4, 50))
    busy = after(45, 4)
    history.observe(
        busy.model_copy(
            update={
                "input_readiness": native.InputReadiness.BUSY,
                "state": busy.state.model_copy(update={"battle_turns": None}),
            }
        )
    )
    later = native.NamedDefinition(name="另一遭遇", definition_id="ENCOUNTER.OTHER")
    history.observe(battle(4, 39, 2).model_copy(update={"encounter": later}))
    history.observe(after(35, 4))
    assert history.recent[0].hp_in == 50
    assert history.recent[0].encounter is None

    continuous = FightHistory()
    continuous.observe(battle(4, 50).model_copy(update={"encounter": later}))
    continuous.observe(busy)
    continuous.observe(after(35, 4))
    assert continuous.recent[0].encounter is not None
    assert continuous.recent[0].encounter.definition_id == "ENCOUNTER.OTHER"


def turn_state(
    turn: int,
    hp: int,
    enemies: Sequence[tuple[str, int, Sequence[Mapping[str, object]]]],
    potions: list[str],
    deck: list[tuple[str, str, int]],
) -> native.Observation:
    raw = json.loads(COMBAT)
    raw["location"]["node_id"] = "battle-9"
    raw["state"]["progress"].update(floor=9, room_type="boss")
    raw["state"]["player"].update(hp=hp, max_hp=80)
    raw["state"]["turn"] = turn
    template = raw["state"]["enemies"][0]
    raw["state"]["enemies"] = [
        {**template, "ref": ref, "hp": enemy_hp, "max_hp": 100, "intents": intents}
        for ref, enemy_hp, intents in enemies
    ]
    card = raw["objects"]["c1"]
    raw["objects"] = {
        ref: {**card, "definition_id": definition_id, "upgrades": upgrades}
        for ref, definition_id, upgrades in deck
    }
    potion = {
        "kind": "potion",
        "name": "药水",
        "usable": True,
        "needs_target": False,
        "target_type": None,
        "usage": None,
    }
    for ref in potions:
        raw["objects"][ref] = {**potion, "definition_id": f"POTION.{ref.upper()}"}
    raw["state"]["potions"] = potions
    raw["state"]["deck"] = [ref for ref, _, _ in deck]
    raw["state"]["piles"]["draw"] = []
    raw["state"]["piles"]["hand"] = []
    return native.Observation.model_validate_json(json.dumps(raw))


def test_fight_rounds_measure_damage_defense_potions_and_deck_changes() -> None:
    history = FightHistory()
    starter = [("c1", "CARD.STRIKE", 0), ("c2", "CARD.BASH", 0)]
    history.observe(turn_state(1, 60, [("a", 40, [])], [], starter))
    history.observe(after(60, 9))
    assert history.recent[-1].deck_added is MISSING

    attack = [{"type": "attack", "damage_per_hit": 6, "hits": 2}]
    bite = [{"type": "attack", "damage_per_hit": 9}]
    deck = [("c1", "CARD.STRIKE", 0), ("c2", "CARD.BASH", 1), ("c3", "CARD.SHRUG", 0)]
    pair = [("big", 50, attack), ("small", 10, bite)]
    history.observe(turn_state(1, 50, pair, ["fire", "block"], deck))
    history.observe(turn_state(1, 50, [("big", 40, attack), ("small", 10, bite)], ["block"], deck))
    second = [("big", 30, [{"type": "buff"}]), ("summoned", 20, bite)]
    history.observe(turn_state(2, 46, second, ["block"], deck))
    history.observe(turn_state(3, 46, [("big", 5, attack), ("summoned", 20, bite)], [], deck))
    history.observe(after(40, 9))

    fight = history.recent[-1]
    assert fight.deck_added == ("CARD.BASH+1", "CARD.SHRUG")
    assert fight.deck_removed == ("CARD.BASH",)
    assert fight.damage == (30, 25, 25)
    assert fight.damage_one == (20, 25, 20)
    assert fight.incoming == (12, 9, 0)
    assert fight.hp_lost == (4, 0, 6)
    assert fight.potions == (("POTION.FIRE",), ("POTION.BLOCK",), ())
