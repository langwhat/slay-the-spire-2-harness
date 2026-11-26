from pydantic import MISSING
from test_mod_client import CARD, COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness.deck_stats import DeckStatistics


def observation(*costs: int | native.CostSymbol | None) -> native.Observation:
    result = native.Observation.model_validate_json(RUN)
    assert isinstance(result.state, native.RunState)
    card = native.CardEntry.model_validate_json(CARD)
    return result.model_copy(
        update={
            "state": result.state.model_copy(
                update={
                    "deck": tuple(f"c{i}" for i in range(len(costs))),
                    "player": result.state.player.model_copy(
                        update={"base_draw": 2, "base_energy": 3}
                    ),
                }
            ),
            "objects": {
                f"c{i}": card.model_copy(
                    update={
                        "definition_id": f"CARD.{i}",
                        "cost": native.ResourceCosts(energy=cost),
                    }
                )
                for i, cost in enumerate(costs)
            },
        }
    )


def history(*cards: str) -> native.HistoryData:
    return native.HistoryData(
        cards_played_this_combat=tuple(
            native.NamedDefinition(name=card, definition_id=card) for card in cards
        ),
        cards_played_this_turn=(),
        cards_exhausted_this_turn=(),
    )


def test_current_deck_counts_are_independent_of_battle_history() -> None:
    current = observation(0, 1, 3, native.CostSymbol.X)
    statistics = DeckStatistics()
    stats, uses = statistics.view(current)
    assert stats == {
        "cards": 4,
        "attacks": 4,
        "skills": 0,
        "powers": 0,
        "basic_cards": 0,
        "unplayable": 0,
        "circulating": 4,
        "circulating_avg_energy": 1.33,
        "base_draw": 2,
        "cycle_turns": 2.0,
        "base_energy": 3,
        "added": 0,
        "skipped": 0,
    }
    assert uses == {}
    battle = native.Observation.model_validate_json(COMBAT)
    statistics.capture(battle, history("CARD.0", "CARD.0", "CARD.1"))
    statistics.capture(battle, history("CARD.0", "CARD.0", "CARD.1"))
    after, uses = statistics.view(current)
    assert after == stats
    assert uses == {"CARD.0": 2, "CARD.1": 1}
    assert DeckStatistics().view(current)[0] == stats


def test_average_energy_counts_only_playable_circulating_copies_with_fixed_cost() -> None:
    current = observation(1, 1, 3, 8, 8, 8, 8, native.CostSymbol.X, native.CostSymbol.NONE, None)
    objects = dict(current.objects)
    for ref, updates in {
        "c1": {"definition_id": "CARD.0"},
        "c3": {"card_type": "power"},
        "c4": {"keywords": ("exhaust",)},
        "c5": {"keywords": ("unplayable",)},
        "c6": {"playable": False},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)

    stats, _ = DeckStatistics().view(current.model_copy(update={"objects": objects}))
    assert stats["circulating"] == 8
    assert stats["circulating_avg_energy"] == 1.67
    no_fixed_cost, _ = DeckStatistics().view(observation(native.CostSymbol.X, None))
    assert "circulating_avg_energy" not in no_fixed_cost


def test_circulating_excludes_each_one_shot_card_once_and_choices_reset_each_act() -> None:
    current = observation(1, 2, 0, native.CostSymbol.NONE)
    objects = dict(current.objects)
    for ref, updates in {
        "c0": {"card_type": "power", "keywords": ("exhaust",)},
        "c1": {"keywords": ("exhaust",)},
        "c2": {"keywords": ("unplayable", "ethereal")},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)
    assert isinstance(current.state, native.RunState)
    current = current.model_copy(
        update={
            "objects": objects,
            "state": current.state.model_copy(
                update={
                    "player": current.state.player.model_copy(update={"base_draw": 4}),
                }
            ),
        }
    )
    statistics = DeckStatistics()
    statistics.record_choice(current, True)
    statistics.record_choice(current, False)
    stats, _ = statistics.view(current)
    assert stats["circulating"] == 1
    assert stats["unplayable"] == 1
    assert (stats["added"], stats["skipped"]) == (1, 1)
    next_act = current.model_copy(
        update={
            "state": current.state.model_copy(
                update={
                    "progress": current.state.progress.model_copy(update={"act": 2}),
                }
            )
        }
    )
    next_stats, _ = statistics.view(next_act)
    assert (next_stats["added"], next_stats["skipped"]) == (0, 0)


def test_basic_cards_and_cycle_turns_reflect_rarity_and_base_draw() -> None:
    current = observation(1, 1, 1, 1, 1, 1, 1)
    objects = dict(current.objects)
    for ref, updates in {
        "c0": {"rarity": "basic"},
        "c1": {"rarity": "BASIC"},
        "c2": {"rarity": "common"},
        "c3": {"rarity": "uncommon"},
        "c4": {"rarity": "rare", "card_type": "power"},
        "c5": {"rarity": None},
    }.items():
        card = objects[ref]
        assert isinstance(card, native.CardEntry)
        objects[ref] = card.model_copy(update=updates)
    assert isinstance(current.state, native.RunState)
    with_draw = current.model_copy(
        update={
            "objects": objects,
            "state": current.state.model_copy(
                update={"player": current.state.player.model_copy(update={"base_draw": 4})}
            ),
        }
    )
    stats, _ = DeckStatistics().view(with_draw)
    assert stats["cards"] == 7
    assert stats["circulating"] == 6
    assert stats["basic_cards"] == 2
    assert stats["cycle_turns"] == 1.5

    empty_circulating = with_draw.model_copy(
        update={"state": with_draw.state.model_copy(update={"deck": ("c4",)})}
    )
    empty_stats, _ = DeckStatistics().view(empty_circulating)
    assert empty_stats["circulating"] == 0
    assert empty_stats["cycle_turns"] == 0.0

    zero_draw = with_draw.model_copy(
        update={
            "state": with_draw.state.model_copy(
                update={"player": with_draw.state.player.model_copy(update={"base_draw": 0})}
            )
        }
    )
    zero_stats, _ = DeckStatistics().view(zero_draw)
    assert zero_stats["basic_cards"] == 2
    assert "cycle_turns" not in zero_stats

    missing_draw = with_draw.model_copy(
        update={
            "state": with_draw.state.model_copy(
                update={"player": with_draw.state.player.model_copy(update={"base_draw": MISSING})}
            )
        }
    )
    missing_stats, _ = DeckStatistics().view(missing_draw)
    assert "cycle_turns" not in missing_stats
