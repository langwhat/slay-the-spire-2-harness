import json

import pytest
from pydantic import JsonValue
from test_lifecycle import battle_state, run_state

from sts2_harness.client import models as native
from sts2_harness_eval.checkpoints import (
    CardIdentity,
    CreatureIdentity,
    IdentityFacts,
    IdentityRead,
    MapIdentity,
    PotionSlotKey,
    RelicIdentity,
)
from sts2_harness_eval.verification import (
    LoadedCorrespondence,
    Publication,
    Relation,
    SourceCorrespondence,
    VerificationFailure,
    _refs,  # pyright: ignore[reportPrivateUsage]
    _translate,  # pyright: ignore[reportPrivateUsage]
    _value,  # pyright: ignore[reportPrivateUsage]
    check_control,
    counter_bounds,
    is_relic_counter,
    native_refs,
)


def with_actions(
    observation: native.Observation, *enabled: bool, label: str = "go"
) -> native.Observation:
    actions = tuple(
        native.Action(ref=f"interaction:1:{index}", label=label, enabled=value)
        for index, value in enumerate(enabled, 1)
    )
    return observation.model_copy(
        update={"state": observation.state.model_copy(update={"actions": actions})}
    )


def test_before_an_operation_only_its_own_control_flag_is_compared() -> None:
    recorded = with_actions(run_state(1, "node"), True, True)
    skip_disabled = with_actions(run_state(1, "node"), True, False)
    take = native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:1")
    SourceCorrespondence().compare(recorded, skip_disabled, 1, next_step=take)
    with pytest.raises(VerificationFailure) as unrelated:
        SourceCorrespondence().compare(recorded, skip_disabled, 1)
    assert unrelated.value.field_path == "state.actions[1].enabled"
    skip = native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:2")
    with pytest.raises(VerificationFailure) as used:
        SourceCorrespondence().compare(recorded, skip_disabled, 1, next_step=skip)
    assert used.value.field_path == "state.actions[1].enabled"
    relabeled = with_actions(run_state(1, "node"), True, True, label="other")
    with pytest.raises(VerificationFailure) as label:
        SourceCorrespondence().compare(recorded, relabeled, 1, next_step=take)
    assert label.value.field_path == "state.actions[0].label"


def test_treasure_proceed_label_can_change_after_the_chest_is_resolved() -> None:
    base = run_state(1, "node")
    assert isinstance(base.state, native.RunState)
    chest = native.Action(
        ref="interaction:1:1",
        label="Open chest",
        enabled=False,
        operation=native.ActionOperation.OPEN_CHEST,
    )
    proceed = native.Action(
        ref="interaction:1:2",
        label="Proceed",
        enabled=True,
        operation=native.ActionOperation.PROCEED,
    )

    def treasure(*actions: native.Action) -> native.Observation:
        return base.model_copy(
            update={
                "state": base.state.model_copy(
                    update={"screen": native.RunScreen.TREASURE, "actions": actions}
                )
            }
        )

    step = native.Interact(kind=native.StepKind.INTERACT, action=proceed.ref)
    skip = proceed.model_copy(update={"label": "Skip"})
    SourceCorrespondence().compare(
        treasure(chest, proceed), treasure(chest, skip), 1, next_step=step
    )

    with pytest.raises(VerificationFailure):
        SourceCorrespondence().compare(
            treasure(chest, proceed),
            treasure(chest, skip.model_copy(update={"enabled": False})),
            1,
            next_step=step,
        )
    with pytest.raises(VerificationFailure):
        SourceCorrespondence().compare(
            treasure(chest, proceed),
            treasure(chest.model_copy(update={"enabled": True}), skip),
            1,
            next_step=step,
        )


def test_potions_are_identified_by_their_list_position() -> None:
    base = run_state(1, "node")
    potions = ("potion:1:7", "potion:1:3")
    observation = base.model_copy(
        update={"state": base.state.model_copy(update={"potions": potions})}
    )
    read = IdentityRead(
        status=native.ResultStatus.COMPLETED,
        observation=observation,
        identities=IdentityFacts(cards=(), creatures=(), map_travel=(), relics=()),
    )
    refs = native_refs(read, Relation.SOURCE_REPLAY)
    assert len(refs) >= 2
    assert {ref: key for ref, key in refs.items() if ref in potions} == {
        ref: PotionSlotKey(slot=slot) for slot, ref in enumerate(potions)
    }


def test_only_unseeded_event_numbers_are_masked() -> None:
    base = with_actions(run_state(1, "node"), True)

    def described(event: str, relic: str, definition: str = "EVENT.TRIAL") -> native.Observation:
        event_entry = native.EventEntry(
            kind=native.ObjectKind.EVENT, name="Trial", ancient=False, definition_id=definition
        )
        return base.model_copy(
            update={
                "objects": {**base.objects, "event:1:1": event_entry},
                "descriptions": {"event:1:1": event, "relic:1:2": relic},
            }
        )

    recorded = described("Entrant 747 enters.", "Gain 3 block.")
    SourceCorrespondence().compare(recorded, described("Entrant 940 enters.", "Gain 3 block."), 1)
    with pytest.raises(VerificationFailure) as other:
        SourceCorrespondence().compare(
            recorded, described("Entrant 940 enters.", "Gain 4 block."), 1
        )
    assert other.value.field_path == "descriptions.relic:1:2"
    with pytest.raises(VerificationFailure) as words:
        SourceCorrespondence().compare(
            recorded, described("Entrant 940 leaves.", "Gain 3 block."), 1
        )
    assert words.value.field_path == "descriptions.event:1:1"
    seeded = described("Lose 7 gold.", "Gain 3 block.", "EVENT.OTHER")
    with pytest.raises(VerificationFailure):
        SourceCorrespondence().compare(
            seeded, described("Lose 9 gold.", "Gain 3 block.", "EVENT.OTHER"), 1
        )


def test_ranked_refs_pair_by_order_and_refuse_count_or_value_differences() -> None:
    recorded = with_actions(run_state(1, "node"), True, True)

    def shifted(offset: int, *enabled: bool) -> native.Observation:
        base = run_state(1, "node")
        actions = tuple(
            native.Action(ref=f"interaction:1:{index + offset}", label="go", enabled=value)
            for index, value in enumerate(enabled, 1)
        )
        return base.model_copy(update={"state": base.state.model_copy(update={"actions": actions})})

    with pytest.raises(VerificationFailure):
        SourceCorrespondence().compare(recorded, shifted(2, True, True), 1)
    refs = SourceCorrespondence(rank_refs=True).compare(recorded, shifted(2, True, True), 1)
    assert refs == {"interaction:1:1": "interaction:1:3", "interaction:1:2": "interaction:1:4"}
    with pytest.raises(VerificationFailure):
        SourceCorrespondence(rank_refs=True).compare(recorded, shifted(2, True), 1)
    with pytest.raises(VerificationFailure) as value:
        SourceCorrespondence(rank_refs=True).compare(recorded, shifted(2, True, False), 1)
    assert value.value.field_path == "state.actions[1].enabled"


def test_arrow_slot_target_refs_are_translated_with_other_refs() -> None:
    value: JsonValue = {
        "state": {"arrow_slots": [{"status": "away", "impaled_target_ref": "enemy:1:2"}]}
    }
    assert _refs(value) == {"enemy:1:2"}


def test_arrow_slot_target_may_be_the_player() -> None:
    value: JsonValue = {
        "state": {"arrow_slots": [{"status": "away", "impaled_target_ref": "player"}]}
    }
    assert _refs(value) == set()


def armed_battle(epoch: int, first: int) -> IdentityRead:
    """Return a combat read whose refs start at ``first``, with an arrow impaled in the enemy."""
    base = battle_state(1, "node")
    refs = {
        name: f"{kind}:{epoch}:{first + index}"
        for index, (name, kind) in enumerate(
            (("c1", "card"), ("c2", "card"), ("c3", "card"), ("e1", "enemy"))
        )
    }
    value = _translate(_value(base), refs)
    observation = native.Observation.model_validate_json(json.dumps(value))
    slot = native.ArrowSlot(status=native.ArrowSlotStatus.AWAY, impaled_target_ref=refs["e1"])
    observation = observation.model_copy(
        update={"state": observation.state.model_copy(update={"arrow_slots": (slot,)})}
    )
    return IdentityRead(
        status=native.ResultStatus.COMPLETED,
        observation=observation,
        identities=IdentityFacts(
            cards=tuple(
                CardIdentity(ref=refs[name], combat_card_id=index, deck_card=None)
                for index, name in enumerate(("c1", "c2", "c3"))
            ),
            creatures=(CreatureIdentity(ref=refs["e1"], combat_id=7),),
            map_travel=(),
            relics=(),
        ),
    )


def test_loaded_reads_translate_the_arrow_slot_target_by_creature_identity() -> None:
    recorded, loaded = armed_battle(4, 10), armed_battle(9, 30)
    reference = Publication(
        source_sequence=3, source_stamp=recorded.observation.stamp, read=recorded
    )
    assert LoadedCorrespondence().compare(reference, loaded) is None
    elsewhere = loaded.observation.state.model_copy(
        update={"arrow_slots": (native.ArrowSlot(status=native.ArrowSlotStatus.AWAY),)}
    )
    with pytest.raises(VerificationFailure) as missing:
        LoadedCorrespondence().compare(
            reference,
            loaded.model_copy(
                update={"observation": loaded.observation.model_copy(update={"state": elsewhere})}
            ),
        )
    assert missing.value.field_path == "state.arrow_slots[0].keys"


def test_loaded_result_ignores_only_unrelated_controls_before_an_operation() -> None:
    base = run_state(1, "node")
    recorded = IdentityRead(
        status=native.ResultStatus.COMPLETED,
        observation=with_actions(base, True, True),
        identities=IdentityFacts(
            cards=(),
            creatures=(),
            map_travel=(
                MapIdentity(ref="interaction:1:1", act=1, row=0, col=0),
                MapIdentity(ref="interaction:1:2", act=1, row=0, col=1),
            ),
            relics=(),
        ),
    )
    reference = Publication(source_sequence=3, source_stamp=base.stamp, read=recorded)
    loaded = recorded.model_copy(update={"observation": with_actions(base, True, False)})
    correspondence = LoadedCorrespondence()
    take = native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:1")
    assert correspondence.compare(reference, loaded, next_step=take) is None

    skip = native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:2")
    for next_step in (None, skip):
        with pytest.raises(VerificationFailure, match=r"state\.actions\[1\]\.enabled"):
            correspondence.compare(reference, loaded, next_step=next_step)


def test_unmatched_loaded_result_does_not_bind_a_transient_room() -> None:
    recorded, loaded = armed_battle(4, 10), armed_battle(9, 30)
    reference = Publication(
        source_sequence=3, source_stamp=recorded.observation.stamp, read=recorded
    )
    transient = loaded.observation.model_copy(
        update={
            "location": loaded.observation.location.model_copy(update={"node_id": "previous"}),
            "game_language": "en",
        }
    )
    correspondence = LoadedCorrespondence()
    with pytest.raises(VerificationFailure):
        correspondence.compare(reference, loaded.model_copy(update={"observation": transient}))
    assert correspondence.compare(reference, loaded) is None


def actions(*refs: int) -> native.Observation:
    return with_actions_at(run_state(1, "node"), refs)


def with_actions_at(
    observation: native.Observation, ordinals: tuple[int, ...]
) -> native.Observation:
    listed = tuple(
        native.Action(ref=f"interaction:1:{ordinal}", label=str(index), enabled=True)
        for index, ordinal in enumerate(ordinals)
    )
    return observation.model_copy(
        update={"state": observation.state.model_copy(update={"actions": listed})}
    )


def test_ranked_refs_keep_one_bijection_across_reads() -> None:
    correspondence = SourceCorrespondence(rank_refs=True)
    assert correspondence.compare(actions(1, 2), actions(5, 6), 1) == {
        "interaction:1:1": "interaction:1:5",
        "interaction:1:2": "interaction:1:6",
    }
    # A ref keeps its first partner even when a newer ref would sort before it.
    with pytest.raises(VerificationFailure):
        correspondence.compare(actions(2, 3), actions(4, 6), 2)
    assert correspondence.compare(actions(2, 3), actions(6, 9), 2) == {
        "interaction:1:2": "interaction:1:6",
        "interaction:1:3": "interaction:1:9",
    }
    # A live ref already paired cannot stand for another recorded ref.
    with pytest.raises(VerificationFailure) as reused:
        correspondence.compare(actions(4), actions(5), 3)
    assert reused.value.field_path == "refs.interaction:1:5"


def test_a_refused_read_leaves_ranked_pairs_unchanged() -> None:
    correspondence = SourceCorrespondence(rank_refs=True)
    relabeled = actions(1).model_copy(
        update={
            "state": actions(1).state.model_copy(
                update={"actions": (native.Action(ref="interaction:1:8", label="x", enabled=True),)}
            )
        }
    )
    with pytest.raises(VerificationFailure):
        correspondence.compare(actions(1), relabeled, 1)
    assert correspondence.compare(actions(1), actions(9), 1) == {
        "interaction:1:1": "interaction:1:9"
    }


def test_the_next_operation_needs_its_control_present_and_enabled() -> None:
    go = native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:1")
    check_control(with_actions(run_state(1, "node"), True), go, Relation.SOURCE_REPLAY, 4)
    for observation in (
        with_actions(run_state(1, "node"), False),
        with_actions(run_state(1, "node")),
    ):
        with pytest.raises(VerificationFailure) as refused:
            check_control(observation, go, Relation.SOURCE_REPLAY, 4)
        assert (refused.value.field_path, refused.value.sequence) == ("control.interact", 4)
    battle = discarding((1, 2, 3))
    assert isinstance(battle.state, native.CombatState)
    play = native.PlayCard(kind=native.StepKind.PLAY_CARD, card="card:1:1")
    with pytest.raises(VerificationFailure):
        check_control(battle, play, Relation.REPLAY_LOAD, 5)
    in_hand = battle.model_copy(
        update={
            "state": battle.state.model_copy(
                update={"piles": battle.state.piles.model_copy(update={"hand": ("card:1:1",)})}
            )
        }
    )
    check_control(in_hand, play, Relation.REPLAY_LOAD, 5)


def discarding(ordinals: tuple[int, int, int]) -> native.Observation:
    """Return a combat read with two cards in the discard pile, listed by ref text like the Mod."""
    names = ("c1", "c2", "c3")
    refs = {name: f"card:1:{ordinal}" for name, ordinal in zip(names, ordinals, strict=True)}
    refs["e1"] = "enemy:1:1"
    observation = native.Observation.model_validate_json(
        json.dumps(_translate(_value(battle_state(1, "node")), refs))
    )
    state = observation.state
    assert isinstance(state, native.CombatState)
    piles = state.piles.model_copy(
        update={"hand": (), "discard": tuple(sorted((refs["c1"], refs["c2"])))}
    )
    return observation.model_copy(update={"state": state.model_copy(update={"piles": piles})})


def test_ranked_refs_compare_unordered_piles_in_translated_ref_order() -> None:
    recorded, live = discarding((9, 10, 11)), discarding((7, 8, 9))
    assert isinstance(recorded.state, native.CombatState)
    assert recorded.state.piles.discard == ("card:1:10", "card:1:9")
    assert SourceCorrespondence(rank_refs=True).compare(recorded, live, 1) == {
        "card:1:9": "card:1:7",
        "card:1:10": "card:1:8",
        "card:1:11": "card:1:9",
        "enemy:1:1": "enemy:1:1",
    }


def test_result_requires_the_recorded_location_and_usable_next_control() -> None:
    go = native.Interact(kind=native.StepKind.INTERACT, action="interaction:1:1")
    correspondence = SourceCorrespondence(rank_refs=True)
    earlier = with_actions(run_state(1, "earlier"), True)
    correspondence.compare(earlier, earlier, 1)
    recorded = with_actions(run_state(1, "node"), True)
    with pytest.raises(VerificationFailure) as disabled:
        correspondence.compare(recorded, with_actions(run_state(1, "node"), False), 2, next_step=go)
    assert disabled.value.field_path == "state.actions[0].enabled"
    with pytest.raises(VerificationFailure) as moved:
        correspondence.compare(recorded, earlier, 2, next_step=go)
    assert moved.value.field_path == "node_id"
    assert correspondence.compare(recorded, recorded, 2, next_step=go)


def with_relic(counter: int, gold: int = 99) -> native.Observation:
    base = with_actions(run_state(1, "node"), True)
    relic = native.RelicEntry(
        kind=native.ObjectKind.RELIC,
        definition_id="RELIC.HAPPY_FLOWER",
        name="Happy Flower",
        counter=counter,
    )
    state = base.state
    assert isinstance(state, native.RunState)
    player = state.player.model_copy(update={"gold": gold})
    return base.model_copy(
        update={
            "objects": {**base.objects, "relic:1:9": relic},
            "state": state.model_copy(update={"relics": ("relic:1:9",), "player": player}),
        }
    )


def test_a_counter_at_threshold_equals_its_reset_value_and_nothing_else_differs() -> None:
    bounds = {"RELIC.HAPPY_FLOWER": (3, 0)}
    for recorded, live in ((3, 0), (0, 3)):
        SourceCorrespondence().compare(
            with_relic(recorded), with_relic(live), 1, counter_bounds=bounds
        )
    for recorded, live, known in ((3, 1, bounds), (2, 0, bounds), (3, 0, {})):
        with pytest.raises(VerificationFailure) as refused:
            SourceCorrespondence().compare(
                with_relic(recorded), with_relic(live), 1, counter_bounds=known
            )
        assert refused.value.field_path == "objects.relic:1:9.counter"
    with pytest.raises(VerificationFailure) as granted:
        SourceCorrespondence().compare(
            with_relic(3), with_relic(0, gold=98), 1, counter_bounds=bounds
        )
    assert granted.value.field_path == "state.player.gold"


def test_counter_bounds_come_from_relic_identities() -> None:
    observation = with_relic(3)
    known = RelicIdentity(
        ref="relic:1:9", owner_net_id=1, slot=0, counter_threshold=3, counter_reset=0
    )
    read = IdentityRead(
        status=native.ResultStatus.COMPLETED,
        observation=observation,
        identities=IdentityFacts(cards=(), creatures=(), map_travel=(), relics=(known,)),
    )
    assert counter_bounds(read) == {"RELIC.HAPPY_FLOWER": (3, 0)}
    unknown = read.model_copy(
        update={
            "identities": read.identities.model_copy(
                update={"relics": (known.model_copy(update={"counter_threshold": None}),)}
            )
        }
    )
    assert counter_bounds(unknown) == {}
    assert is_relic_counter("objects.relic:1:9.counter")
    assert not is_relic_counter("objects.card:1:9.counter")
