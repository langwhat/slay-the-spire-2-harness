import json
from collections.abc import Iterator

import pytest
from pydantic import JsonValue
from test_mod_client import COMBAT, RUN

from sts2_harness import context as context_module
from sts2_harness.client.models import (
    CardEntry,
    CombatState,
    DecisionOwner,
    MechanismKind,
    MechanismView,
    Observation,
    ResourceCosts,
    RunState,
    TargetPreview,
)
from sts2_harness.context import CombatActInput, Context, QueryToolResult, ReadCompleted
from sts2_harness.context.models import RunActInput
from sts2_harness.llm.messages import AssistantMessage, ResponsesReply, ToolCall
from sts2_harness.note_values import NotesView


def test_changed_hand_is_complete_and_original_history_stays_unchanged() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    original = context.model_input()[0].text
    initial = json.loads(original)["observation"]
    assert [row["ref"] for row in initial["hand"]] == ["c2", "c1"]
    assert initial["enemies"][0]["intents"][1]["hits"] == 2
    assert initial["hand"][1]["cost"]["energy"] == "X"
    assert "stars" not in initial["hand"][1]["cost"]
    assert initial["hand"][0]["text"] == "造成3点伤害。"
    assert not {"gold", "potion_slot_count", "empty_potion_slot_count"} & initial["player"].keys()
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
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), changed)
    )
    update = json.loads(context.model_input()[-1].text)["observation"]
    assert [row["ref"] for row in update["hand"]] == ["c2", "c1"]
    assert update["hand"][1]["cost"]["energy"] == 0
    assert "defs" not in update
    assert "enemies" not in update
    assert context.model_input()[0].text == original


def test_pile_membership_aggregates_without_order_or_instance_refs() -> None:
    observation = Observation.model_validate_json(COMBAT)
    state = observation.state
    assert isinstance(state, CombatState)
    card = observation.objects["c1"]
    observation = observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "piles": state.piles.model_copy(
                        update={
                            "hand": (),
                            "draw": ("c1", "c2"),
                        }
                    )
                }
            ),
            "descriptions": {"c1": "造成3点伤害。", "c2": "造成3点伤害。"},
            "objects": {"c1": card, "c2": card},
            "modifier_descriptions": {},
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    state = json.loads(context.model_input()[0].text)["observation"]
    assert len(state["draw"]) == 1
    assert state["draw"][0]["id"] == "CARD.ONE"
    assert state["draw"][0]["n"] == 2
    assert "ref" not in state["draw"][0]


def test_combat_projects_move_id_changes_with_identical_public_intents() -> None:
    payload = json.loads(COMBAT)
    enemy = payload["state"]["enemies"][0]
    enemy["next_move_id"] = "JAB_MOVE"
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), Observation.model_validate_json(json.dumps(payload)))
    first = json.loads(context.model_input()[-1].text)["observation"]["enemies"][0]

    enemy["next_move_id"] = "JAB_MOVE_2"
    context.append_task(CombatActInput(), Observation.model_validate_json(json.dumps(payload)))
    second = json.loads(context.model_input()[-1].text)["observation"]["enemies"][0]

    assert first["next_move_id"] == "JAB_MOVE"
    assert second["next_move_id"] == "JAB_MOVE_2"
    assert first["intents"] == second["intents"]


def test_compaction_restores_defs_sections_and_opening_notes() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.begin_group(observation, NotesView(other="ENGINE test"))
    context.append_task(CombatActInput(NotesView(other="ENGINE test")), observation)
    context.finish_group()
    candidate = context.compaction_candidate()
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    context.begin_group(observation, NotesView(other="ENGINE test"))
    context.append_reply(
        AssistantMessage(
            ResponsesReply.from_json(
                '[{"type":"function_call","id":"fc","call_id":"read",'
                '"name":"combat_inspect","arguments":"{}"}]'
            )
        )
    )
    context.append_tool_result(
        ToolCall("read", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), observation)
    )
    context.finish_group()
    context.install_compaction(context.compaction_candidate())
    baseline = json.loads(context.model_input()[0].text)["context_baseline"]
    assert baseline["observation"]["baseline"] is True
    assert baseline["observation"]["defs"][0]["id"] == "CARD.ONE"
    assert baseline["opening_notes"]["other"] == "ENGINE test"
    assert baseline["observation"]["hand"][0]["ref"] == "c2"


def test_role_boundary_hides_combat_menu_and_aggregates_native_deck_text() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), observation)
    state = json.loads(context.model_input()[0].text)["observation"]
    assert "hand" not in state
    assert "actions" not in state["choice"]
    assert state["deck"][0]["id"] == "CARD.ONE"
    assert "text" not in state["deck"][0]
    assert not any(row["id"].startswith("CARD.") for row in state.get("defs", []))
    assert "ref" not in state["deck"][0]
    context.append_task(RunActInput(), Observation.model_validate_json(RUN))
    update = json.loads(context.model_input()[-1].text)["observation"]
    assert update["deck"] == []
    assert update["choice"]["actions"][0]["ref"] == "travel"


def test_automatic_upgrade_previews_remain_beside_candidates_and_update() -> None:
    observation = Observation.model_validate_json(COMBAT)
    combat_state = observation.state
    assert isinstance(combat_state, CombatState)
    run = Observation.model_validate_json(RUN)
    assert isinstance(run.state, RunState)
    for role, state, task in (
        (DecisionOwner.COMBAT, combat_state, CombatActInput()),
        (
            DecisionOwner.RUN,
            run.state.model_copy(update={"selection": combat_state.selection}),
            RunActInput(),
        ),
    ):
        current = observation.model_copy(
            update={
                "state": state,
                "location": observation.location.model_copy(update={"owner": role}),
            }
        )
        context = Context(role)
        context.append_task(task, current)
        initial = json.loads(context.model_input()[-1].text)["observation"]
        candidates = (
            initial["hand"]
            if role is DecisionOwner.COMBAT
            else initial["choice"]["selection"]["candidates"]
        )
        assert candidates[1]["upgrade"] == {"status": "available", "text": "造成3点伤害。"}
        assert candidates[0]["upgrade"] == {"status": "not_upgradable"}

        changed = current.model_copy(update={"upgrades": {}})
        context.append_task(task, changed)
        update = json.loads(context.model_input()[-1].text)["observation"]
        updated = (
            update["hand"]
            if role is DecisionOwner.COMBAT
            else update["choice"]["selection"]["candidates"]
        )
        assert all("upgrade" not in row for row in updated)


def test_history_use_names_its_base_definition_scope_across_upgrade_rows() -> None:
    observation = Observation.model_validate_json(COMBAT)
    original = observation.objects["c1"]
    assert isinstance(original, CardEntry)
    state = observation.state
    assert isinstance(state, CombatState)
    observation = observation.model_copy(
        update={
            "state": state.model_copy(update={"deck": ("c1", "c2")}),
            "objects": {
                **observation.objects,
                "c2": original.model_copy(update={"upgrades": 1}),
            },
        }
    )
    context = Context(DecisionOwner.RUN)
    context.set_deck_stats({"observed_plays": 3}, {"CARD.ONE": 3})
    context.append_task(RunActInput(), observation)
    deck = json.loads(context.model_input()[-1].text)["observation"]["deck"]
    assert {row["id"] for row in deck} == {"CARD.ONE", "CARD.ONE+1"}
    assert all(row["use"] == {"observed_plays": 3} for row in deck)


def test_owned_deck_upgrades_group_copies_and_target_previews_keep_only_changes() -> None:
    observation = Observation.model_validate_json(COMBAT)
    state = observation.state
    card = observation.objects["c1"]
    assert isinstance(state, CombatState) and isinstance(card, CardEntry)
    observation = observation.model_copy(
        update={
            "state": state.model_copy(update={"deck": ("c1", "copy")}),
            "objects": {
                **observation.objects,
                "copy": card,
                "c1": card.model_copy(
                    update={
                        "previews": (
                            TargetPreview(target="same", text="Deal 3 damage."),
                            TargetPreview(target="different", text="Deal 9 damage."),
                        ),
                    }
                ),
            },
            "descriptions": {
                **observation.descriptions,
                "c1": "Deal 3 damage.",
                "copy": "Deal 3 damage.",
            },
            "modifier_descriptions": {
                **observation.modifier_descriptions,
                "copy": observation.modifier_descriptions["c1"],
            },
            "upgrades": {**observation.upgrades, "copy": observation.upgrades["c1"]},
        }
    )
    run = Context(DecisionOwner.RUN)
    run.append_task(RunActInput(), observation)
    deck = json.loads(run.model_input()[-1].text)["observation"]["deck"]
    assert len(deck) == 1 and deck[0]["n"] == 2
    assert deck[0]["upgrade"] == {"status": "available", "text": "造成3点伤害。"}
    combat = Context(DecisionOwner.COMBAT)
    combat.append_task(CombatActInput(), observation)
    hand = json.loads(combat.model_input()[-1].text)["observation"]["hand"]
    assert hand[1]["previews"] == [{"target": {"ref": "different"}, "text": "Deal 9 damage."}]
    changed = observation.model_copy(
        update={
            "objects": {
                **observation.objects,
                "c1": card.model_copy(
                    update={
                        "previews": (
                            TargetPreview(target="same", text=observation.descriptions["c1"] or ""),
                        ),
                    }
                ),
            }
        }
    )
    combat.append_task(CombatActInput(), changed)
    hand = json.loads(combat.model_input()[-1].text)["observation"]["hand"]
    assert "previews" not in hand[1]


def test_mechanisms_are_once_delivered_without_owners_and_restore_after_compaction() -> None:
    observation = Observation.model_validate_json(COMBAT)
    mechanism = MechanismView(
        kind=MechanismKind.POWER,
        definition_id="POWER.ONE",
        name="Power",
        owner="c1",
        text="Native rule",
    )
    observation = observation.model_copy(
        update={
            "mechanisms": (
                mechanism,
                mechanism.model_copy(update={"owner": "c2"}),
            )
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    first = json.loads(context.model_input()[-1].text)["observation"]
    assert first["mechanisms"] == [
        {
            "kind": "power",
            "definition_id": "POWER.ONE",
            "name": "Power",
            "text": "Native rule",
        }
    ]
    assert "deck" not in first
    context.begin_group(observation, NotesView(other=""))
    state = observation.state
    assert isinstance(state, CombatState)
    changed = observation.model_copy(
        update={
            "state": state.model_copy(update={"turn": 2}),
            "mechanisms": (mechanism.model_copy(update={"owner": "c2"}),),
        }
    )
    context.append_task(CombatActInput(), changed)
    assert "mechanisms" not in json.loads(context.model_input()[-1].text)["observation"]
    context.finish_group()
    context.install_compaction(context.compaction_candidate())
    baseline = json.loads(context.model_input()[0].text)["context_baseline"]["observation"]
    assert baseline["mechanisms"] == first["mechanisms"]
    changed = changed.model_copy(
        update={"mechanisms": (mechanism.model_copy(update={"text": "Changed native rule"}),)}
    )
    context.append_task(CombatActInput(), changed)
    update = json.loads(context.model_input()[-1].text)["observation"]
    assert update["mechanisms"][0]["text"] == "Changed native rule"


def test_combat_delivers_native_enemy_rule_text_verbatim() -> None:
    observation = Observation.model_validate_json(COMBAT)
    mechanism = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="MONSTER.LAGAVULIN_MATRIARCH",
        name="樂加維林族母",
        text=(
            "First move: SLEEP_MOVE\n"
            "沉睡 (POWER.ASLEEP_POWER) · After damage resolves: attempt to stun the holder\n"
            "沉睡 (POWER.ASLEEP_POWER) · After side turn ends: attempt to subtract one stack"
        ),
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(
        CombatActInput(), observation.model_copy(update={"mechanisms": (mechanism,)})
    )
    delivered = json.loads(context.model_input()[-1].text)["observation"]["mechanisms"]
    assert delivered == [json.loads(mechanism.to_json())]


def test_current_unknown_values_override_known_card_text_and_cost() -> None:
    observation = Observation.model_validate_json(COMBAT)
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    context = Context(DecisionOwner.COMBAT)
    known = observation.model_copy(
        update={"descriptions": {"c1": "Known current effect", "c2": "Known current effect"}}
    )
    context.append_task(CombatActInput(), known)
    unknown = known.model_copy(
        update={
            "objects": {
                **known.objects,
                "c1": card.model_copy(update={"cost": ResourceCosts(energy=None)}),
            },
            "descriptions": {"c1": None},
        }
    )
    context.append_task(CombatActInput(), unknown)
    row = json.loads(context.model_input()[-1].text)["observation"]["hand"][1]
    assert row["text"] is None
    assert row["cost"]["energy"] is None


def test_nonhand_piles_preserve_current_cost_and_effect_differences() -> None:
    observation = Observation.model_validate_json(COMBAT)
    assert isinstance(observation.state, CombatState)
    original = observation.objects["c1"]
    assert isinstance(original, CardEntry)
    observation = observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "piles": observation.state.piles.model_copy(
                        update={
                            "hand": (),
                            "draw": (),
                            "discard": ("c1", "c2", "c3"),
                        }
                    ),
                }
            ),
            "objects": {
                "c1": original.model_copy(update={"cost": ResourceCosts(energy=1)}),
                "c2": original.model_copy(update={"cost": ResourceCosts(energy=0)}),
                "c3": original.model_copy(update={"cost": ResourceCosts(energy=0)}),
            },
            "descriptions": {
                "c1": "Deal 8 damage.",
                "c2": "Deal 16 damage.",
                "c3": "Deal 16 damage.",
            },
            "modifier_descriptions": {},
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    projected = json.loads(context.model_input()[-1].text)["observation"]
    assert len(projected["discard"]) == 2
    changed = next(row for row in projected["discard"] if row["n"] == 2)
    assert changed["cost"]["energy"] == 0
    assert changed["text"] == "Deal 16 damage."
    assert all("ref" not in row for row in projected["discard"])


def test_compaction_restores_rule_that_reappeared_after_an_absent_baseline() -> None:
    observation = Observation.model_validate_json(COMBAT)
    mechanism = MechanismView(
        kind=MechanismKind.POWER,
        definition_id="POWER.ONE",
        name="Power",
        owner="c1",
        text="Native recurring rule",
    )
    visible = observation.model_copy(update={"mechanisms": (mechanism,)})
    absent = observation.model_copy(update={"mechanisms": ()})
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), visible)
    context.append_task(CombatActInput(), absent)
    context.begin_group(absent, NotesView())
    context.append_task(CombatActInput(), visible)
    assert "mechanisms" not in json.loads(context.model_input()[-1].text)["observation"]
    candidate = context.compaction_candidate()
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    baseline = json.loads(context.model_input()[0].text)["context_baseline"]["observation"]
    assert baseline["mechanisms"][0]["text"] == "Native recurring rule"


class _ReversedSet(set[str]):
    def __iter__(self) -> Iterator[str]:
        ordered: list[str] = sorted(set[str].copy(self), reverse=True)
        return iter(ordered)


def test_compaction_baseline_definition_order_does_not_follow_set_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = Observation.model_validate_json(COMBAT)
    card = observation.objects["c2"]
    assert isinstance(card, CardEntry)
    observation = observation.model_copy(
        update={
            "objects": {
                **observation.objects,
                "c2": card.model_copy(update={"definition_id": "CARD.TWO"}),
            }
        }
    )
    original = context_module.definition_references

    def reversed_references(value: JsonValue) -> set[str]:
        return _ReversedSet(original(value))

    monkeypatch.setattr(context_module, "definition_references", reversed_references)
    context = Context(DecisionOwner.COMBAT)
    context.begin_group(observation, NotesView())
    context.append_task(CombatActInput(), observation)
    context.finish_group()
    group = context._last_group  # pyright: ignore[reportPrivateUsage]
    assert group is not None
    # Only the retained message supplies these definitions to the compaction baseline.
    group.observation["defs"] = []
    _, baseline = context.compaction_input()
    definitions = json.loads(baseline.text)["context_baseline"]["observation"]["defs"]
    assert [row["id"] for row in definitions] == ["CARD.ONE", "CARD.TWO", "ENEMY.ONE"]


def test_combat_state_format_explains_unpowered_vs_unblockable_damage() -> None:
    from sts2_harness.combat import _system  # pyright: ignore[reportPrivateUsage]
    from sts2_harness.prompts import PackagedPrompts

    text = _system(PackagedPrompts(), viewer_input=False, minimal=True).body
    assert (
        "Unpowered damage ignores attack modifiers such as POWER.STRENGTH_POWER, "
        "POWER.WEAK_POWER, and POWER.VULNERABLE_POWER, "
        "and is still blockable unless marked unblockable."
    ) in text


def test_precombat_boss_and_elite_mechanisms_route_by_role_and_remaining_bosses() -> None:
    from sts2_harness.client.models import NamedDefinition, PublicMap
    from sts2_harness.deck import card_rewards, deck_context, deck_observation
    from sts2_harness.prompts import PackagedPrompts
    from sts2_harness.run import run_observation

    base = Observation.model_validate_json(RUN)
    assert isinstance(base.state, RunState)
    public_map = PublicMap.model_validate_json(
        '{"bosses":[{"name":"女王","definition_id":"ENCOUNTER.QUEEN_BOSS"},'
        '{"name":"实验体","definition_id":"ENCOUNTER.TEST_SUBJECT_BOSS"}],'
        '"elite_pool":[{"name":"骑士","definition_id":"ENCOUNTER.KNIGHTS_ELITE"}],'
        '"nodes":[{"position":"map:0:0","room_type":"monster","children":["map:1:0"]},'
        '{"position":"map:1:0","room_type":"boss","children":[]}],'
        '"legal_destinations":["map:1:0"]}'
    )
    boss_first = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="ENCOUNTER.QUEEN_BOSS",
        name="女王",
        text="Monsters: 1× 女王 (MONSTER.QUEEN, 419 HP)",
        owner="act",
    )
    boss_second = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="ENCOUNTER.TEST_SUBJECT_BOSS",
        name="实验体",
        text="Monsters: 1× 实验体 (MONSTER.TEST_SUBJECT, 211 HP)",
        owner="act",
    )
    elite_rule = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="ENCOUNTER.KNIGHTS_ELITE",
        name="骑士",
        text="Monsters: 1× 连枷骑士 (MONSTER.FLAIL_KNIGHT, 108 HP)",
        owner="act",
    )
    active = base.model_copy(
        update={
            "map": public_map,
            "state": base.state.model_copy(
                update={
                    "progress": base.state.progress.model_copy(
                        update={"position": "map:0:0", "room_type": "monster", "bosses_left": 2}
                    )
                }
            ),
            "mechanisms": (*base.mechanisms, boss_first, boss_second, elite_rule),
        }
    )

    # Run receives both upcoming Boss rules and the act's candidate Elite rules once per act.
    run_ctx = Context(DecisionOwner.RUN)
    run_ctx.append_task(RunActInput(), run_observation(active))
    run_payload = json.loads(run_ctx.model_input()[-1].text)["observation"]
    run_mech_ids = [row["definition_id"] for row in run_payload["mechanisms"]]
    assert "ENCOUNTER.QUEEN_BOSS" in run_mech_ids
    assert "ENCOUNTER.TEST_SUBJECT_BOSS" in run_mech_ids
    assert "ENCOUNTER.KNIGHTS_ELITE" in run_mech_ids
    assert run_payload["map"]["elite_pool"] == [
        {"name": "骑士", "definition_id": "ENCOUNTER.KNIGHTS_ELITE"}
    ]

    # Deck receives remaining Bosses in progress.boss and Boss rules, excluding Elite pool rules.
    offers, _ = card_rewards(active)
    deck_view = deck_observation(active, offers)
    deck_ctx = deck_context(PackagedPrompts(), (), deck_view, minimal=False)
    deck_ctx.append_task(RunActInput(), deck_view)
    deck_payload = json.loads(deck_ctx.model_input()[-1].text)["observation"]
    assert deck_payload["progress"]["boss"] == [
        {"name": "女王", "definition_id": "ENCOUNTER.QUEEN_BOSS"},
        {"name": "实验体", "definition_id": "ENCOUNTER.TEST_SUBJECT_BOSS"},
    ]
    deck_mech_ids = [row["definition_id"] for row in deck_payload.get("mechanisms", [])]
    assert "ENCOUNTER.QUEEN_BOSS" in deck_mech_ids
    assert "ENCOUNTER.TEST_SUBJECT_BOSS" in deck_mech_ids
    assert "ENCOUNTER.KNIGHTS_ELITE" not in deck_mech_ids

    # When only the second Boss remains, Deck keeps only the remaining Boss.
    one_left = active.model_copy(
        update={
            "state": active.state.model_copy(
                update={"progress": active.state.progress.model_copy(update={"bosses_left": 1})}
            )
        }
    )
    one_left_view = deck_observation(one_left, offers)
    one_left_ctx = deck_context(PackagedPrompts(), (), one_left_view, minimal=False)
    one_left_ctx.append_task(RunActInput(), one_left_view)
    one_left_payload = json.loads(one_left_ctx.model_input()[-1].text)["observation"]
    assert one_left_payload["progress"]["boss"] == [
        {"name": "实验体", "definition_id": "ENCOUNTER.TEST_SUBJECT_BOSS"}
    ]
    assert [
        row["definition_id"]
        for row in one_left_payload.get("mechanisms", [])
        if str(row.get("definition_id", "")).startswith("ENCOUNTER.")
    ] == ["ENCOUNTER.TEST_SUBJECT_BOSS"]

    # Post-Boss rewards omit defeated Bosses from Deck, and Combat ignores act-scoped Run rules.
    post_boss = active.model_copy(
        update={
            "map": public_map.model_copy(
                update={
                    "bosses": (NamedDefinition(name="女王", definition_id="ENCOUNTER.QUEEN_BOSS"),),
                    "legal_destinations": (),
                }
            ),
            "state": active.state.model_copy(
                update={
                    "progress": active.state.progress.model_copy(
                        update={"position": "map:1:0", "room_type": "boss", "bosses_left": 0}
                    )
                }
            ),
        }
    )
    post_view = deck_observation(post_boss, offers)
    post_ctx = deck_context(PackagedPrompts(), (), post_view, minimal=False)
    post_ctx.append_task(RunActInput(), post_view)
    post_payload = json.loads(post_ctx.model_input()[-1].text)["observation"]
    assert "boss" not in post_payload["progress"]
    assert not any(
        str(row.get("definition_id", "")).startswith("ENCOUNTER.")
        for row in post_payload.get("mechanisms", [])
    )

    combat_ctx = Context(DecisionOwner.COMBAT)
    combat_ctx.append_task(CombatActInput(), active)
    combat_payload = json.loads(combat_ctx.model_input()[-1].text)["observation"]
    assert not any(
        str(row.get("definition_id", "")).startswith("ENCOUNTER.")
        for row in combat_payload.get("mechanisms", [])
    )


def test_act_event_item_and_power_native_mechanisms_route_cleanly_across_roles() -> None:
    from sts2_harness.client.models import (
        Action,
        EventEntry,
        EventState,
        ObjectKind,
        Offer,
        OfferEntry,
        OfferKind,
        PublicMap,
        RelicEntry,
        RunScreen,
    )
    from sts2_harness.deck import card_rewards, deck_context, deck_observation
    from sts2_harness.prompts import PackagedPrompts
    from sts2_harness.run import run_observation

    base = Observation.model_validate_json(RUN)
    assert isinstance(base.state, RunState)
    combat_base = Observation.model_validate_json(COMBAT)
    deck_card = combat_base.objects["c1"]
    reward_card = combat_base.objects["c2"]
    assert isinstance(deck_card, CardEntry) and isinstance(reward_card, CardEntry)

    public_map = PublicMap.model_validate_json(
        '{"bosses":[{"name":"女王","definition_id":"ENCOUNTER.QUEEN_BOSS"}],'
        '"elite_pool":[],"nodes":[{"position":"map:0:0","room_type":"event","children":["map:1:0"]},'
        '{"position":"map:1:0","room_type":"boss","children":[]}],'
        '"legal_destinations":[]}'
    )
    act_rule = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="ACT.OVERGROWTH",
        name="繁茂之地",
        text=(
            "Entering the next act's opening Ancient room after defeating this act's boss(es) "
            "heals floor(0.8 × missing HP)."
        ),
        owner="act",
    )
    event_rule_page1 = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="EVENT.FIELD_OF_MAN_SIZED_HOLES",
        name="人形孔洞之野",
        text=(
            "Option [钻入]: select 1 removable card(s) from deck to remove, "
            "then unconditionally add 1 常态 (CARD.NORMALITY) to deck"
        ),
        owner="ev1",
    )
    relic_rule = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="RELIC.JUZU_BRACELET",
        name="数珠手链",
        text="Unknown (?) map nodes exclude room types: Monster, Elite.",
        owner="r1",
    )
    deck_card_rule = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="CARD.ONE",
        name="打击",
        text=(
            "Resolves attack damage first, "
            "then selects from discard pile to move on top of draw pile."
        ),
        owner="c_deck",
    )
    reward_card_rule = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="CARD.BURNING_PACT",
        name="燃烧契约",
        text=(
            "Selects card(s) in hand to exhaust first "
            "(auto-selects when only 1 other card is in hand); "
            "draws cards only if a card was selected and exhausted."
        ),
        owner="c_offer",
    )
    reward_power_rule = MechanismView(
        kind=MechanismKind.OTHER,
        definition_id="POWER.DARK_EMBRACE_POWER",
        name="黑暗之拥",
        text=(
            "Exhaust draw timing: when an owned card is exhausted without Ethereal, "
            "holder immediately draws this power's current stacks card(s)"
        ),
        owner="c_offer",
    )

    event_obs = base.model_copy(
        update={
            "map": public_map,
            "state": base.state.model_copy(
                update={
                    "screen": RunScreen.EVENT,
                    "deck": ("c_deck",),
                    "relics": ("r1",),
                    "event": EventState(
                        ref="ev1",
                        name="人形孔洞之野",
                        ancient=False,
                        definition_id="EVENT.FIELD_OF_MAN_SIZED_HOLES",
                    ),
                }
            ),
            "objects": {
                **base.objects,
                "ev1": EventEntry(
                    kind=ObjectKind.EVENT,
                    name="人形孔洞之野",
                    ancient=False,
                    definition_id="EVENT.FIELD_OF_MAN_SIZED_HOLES",
                ),
                "r1": RelicEntry(
                    kind=ObjectKind.RELIC,
                    definition_id="RELIC.JUZU_BRACELET",
                    name="数珠手链",
                    status="normal",
                    is_used_up=False,
                    is_melted=False,
                    stack_count=1,
                ),
                "c_deck": deck_card,
            },
            "descriptions": {
                **base.descriptions,
                "ev1": "事件描述",
                "r1": "未知房间不再遇到怪物。",
                "c_deck": "造成6点伤害。",
            },
            "modifier_descriptions": {"c_deck": combat_base.modifier_descriptions["c1"]},
            "upgrades": {"c_deck": combat_base.upgrades["c1"]},
            "mechanisms": (act_rule, event_rule_page1, relic_rule, deck_card_rule),
        }
    )

    # Run receives act-transition, event-option, owned relic, and owned card rules once,
    # and re-delivers event rules when follow-up event options change rule text.
    run_ctx = Context(DecisionOwner.RUN)
    run_ctx.append_task(RunActInput(), run_observation(event_obs))
    run_mechs = json.loads(run_ctx.model_input()[-1].text)["observation"]["mechanisms"]
    assert {row["definition_id"] for row in run_mechs} == {
        "ACT.OVERGROWTH",
        "EVENT.FIELD_OF_MAN_SIZED_HOLES",
        "RELIC.JUZU_BRACELET",
        "CARD.ONE",
    }
    event_rule_page2 = event_rule_page1.model_copy(
        update={"text": "Option [深入]: lose 5 Max HP, then obtain 1 relic directly"}
    )
    event_page2_obs = event_obs.model_copy(
        update={"mechanisms": (act_rule, event_rule_page2, relic_rule, deck_card_rule)}
    )
    run_ctx.append_task(RunActInput(), run_observation(event_page2_obs))
    page2_mechs = json.loads(run_ctx.model_input()[-1].text)["observation"]["mechanisms"]
    assert [row["definition_id"] for row in page2_mechs] == ["EVENT.FIELD_OF_MAN_SIZED_HOLES"]
    assert page2_mechs[0]["text"] == event_rule_page2.text

    # On a card-reward screen, Deck receives owned + offered card/relic/power rules
    # and excludes act/event rules, while Run hides unowned card-reward rules until taken.
    reward_obs = event_obs.model_copy(
        update={
            "state": event_obs.state.model_copy(
                update={
                    "screen": RunScreen.REWARDS,
                    "event": None,
                    "offers": (
                        Offer(
                            ref="offer_card",
                            kind=OfferKind.CARD,
                            contents=("c_offer",),
                            actions=("act_offer",),
                            requires_selection=True,
                        ),
                    ),
                    "actions": (
                        Action(
                            ref="act_offer",
                            label="卡牌奖励",
                            enabled=True,
                            subject="offer_card",
                        ),
                    ),
                }
            ),
            "objects": {
                **event_obs.objects,
                "offer_card": OfferEntry(kind=ObjectKind.OFFER, name="卡牌奖励"),
                "c_offer": reward_card.model_copy(update={"definition_id": "CARD.BURNING_PACT"}),
            },
            "descriptions": {
                **event_obs.descriptions,
                "offer_card": "卡牌奖励",
                "c_offer": "消耗1张牌，抽2张牌。",
            },
            "modifier_descriptions": {
                **event_obs.modifier_descriptions,
                "c_offer": combat_base.modifier_descriptions["c1"],
            },
            "upgrades": {
                **event_obs.upgrades,
                "c_offer": combat_base.upgrades["c2"],
            },
            "mechanisms": (
                act_rule,
                event_rule_page1,
                relic_rule,
                deck_card_rule,
                reward_card_rule,
                reward_power_rule,
            ),
        }
    )
    offers, _ = card_rewards(reward_obs)
    deck_view = deck_observation(reward_obs, offers)
    deck_ctx = deck_context(PackagedPrompts(), (), deck_view, minimal=False)
    deck_ctx.append_task(RunActInput(), deck_view)
    deck_mechs = json.loads(deck_ctx.model_input()[-1].text)["observation"]["mechanisms"]
    assert {row["definition_id"] for row in deck_mechs} == {
        "RELIC.JUZU_BRACELET",
        "CARD.ONE",
        "CARD.BURNING_PACT",
        "POWER.DARK_EMBRACE_POWER",
    }

    run_reward_ctx = Context(DecisionOwner.RUN)
    run_reward_ctx.append_task(RunActInput(), run_observation(reward_obs))
    run_reward_obs = json.loads(run_reward_ctx.model_input()[-1].text)["observation"]
    run_reward_mechs = run_reward_obs["mechanisms"]
    assert "CARD.BURNING_PACT" not in {row["definition_id"] for row in run_reward_mechs}
    assert "POWER.DARK_EMBRACE_POWER" not in {row["definition_id"] for row in run_reward_mechs}
    assert "ACT.OVERGROWTH" in {row["definition_id"] for row in run_reward_mechs}

    # Combat excludes act-scoped and event-scoped mechanisms on foreign Run observations
    # while delivering owned card/relic rules, and delivers player power rules in Combat.
    combat_on_event = Context(DecisionOwner.COMBAT)
    combat_on_event.append_task(CombatActInput(), event_obs)
    foreign_mechs = json.loads(combat_on_event.model_input()[-1].text)["observation"]["mechanisms"]
    assert {row["definition_id"] for row in foreign_mechs} == {
        "RELIC.JUZU_BRACELET",
        "CARD.ONE",
    }

    combat_obs = combat_base.model_copy(
        update={
            "mechanisms": (
                act_rule,
                MechanismView(
                    kind=MechanismKind.OTHER,
                    definition_id="CARD.ONE",
                    name="打击",
                    text=deck_card_rule.text,
                    owner="c1",
                ),
                MechanismView(
                    kind=MechanismKind.OTHER,
                    definition_id="CARD.ONE",
                    name="打击+",
                    text=deck_card_rule.text,
                    owner="c2",
                ),
                MechanismView(
                    kind=MechanismKind.OTHER,
                    definition_id="POWER.DARK_EMBRACE_POWER",
                    name="黑暗之拥",
                    text=reward_power_rule.text,
                    owner="player",
                ),
            )
        }
    )
    combat_ctx2 = Context(DecisionOwner.COMBAT)
    combat_ctx2.append_task(CombatActInput(), combat_obs)
    combat_mechs = json.loads(combat_ctx2.model_input()[-1].text)["observation"]["mechanisms"]
    # Base and upgraded copies sharing definition_id and text deduplicate to one entry.
    assert [row["definition_id"] for row in combat_mechs] == [
        "CARD.ONE",
        "POWER.DARK_EMBRACE_POWER",
    ]

    # Run-role view of an active Combat observation excludes player/creature power rules
    # while keeping deck/relic/potion-owned rules.
    run_during_combat = Context(DecisionOwner.RUN)
    run_during_combat.append_task(RunActInput(), combat_obs)
    run_combat_mechs = json.loads(run_during_combat.model_input()[-1].text)["observation"].get(
        "mechanisms", []
    )
    assert "POWER.DARK_EMBRACE_POWER" not in {row["definition_id"] for row in run_combat_mechs}
    assert {row["definition_id"] for row in run_combat_mechs} == {"CARD.ONE"}
