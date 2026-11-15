import json

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
            floor=floor, kind=FightKind.ELITE, turns=4, hp_in=50, hp_out=46
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
            assert delivered["fights"] == [fight.model_dump() for fight in history.recent]
            encoded = json.dumps({"fights": delivered["fights"]}, separators=(",", ":"))
            assert len(encoded) <= 500
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
    assert history.recent[0].model_dump() == {
        "floor": 4,
        "kind": "elite",
        "encounter": {"definition_id": "ENCOUNTER.GUARDIAN", "name": "守卫者"},
        "turns": 4,
        "hp_in": 50,
        "hp_out": 37,
    }
    context = Context(native.DecisionOwner.RUN)
    context.set_fights(history.recent)
    context.append_task(RunActInput(), after(37, 4))
    delivered = context.model_input()[-1].text
    assert list(json.loads(delivered)["observation"]["fights"][0]) == [
        "floor",
        "kind",
        "encounter",
        "turns",
        "hp_in",
        "hp_out",
    ]
    history.observe(
        battle(5, 37).model_copy(update={"encounter": native.NamedDefinition(name="未知")})
    )
    history.observe(after(31, 5))
    assert history.recent[1].encounter is None
