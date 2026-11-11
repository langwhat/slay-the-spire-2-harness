from test_mod_client import CARD, COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness.deck_stats import DeckStatistics


def observation(*costs: int | native.CostSymbol) -> native.Observation:
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


def test_current_deck_baseline_arithmetic_is_stable_and_independent_of_battle_history() -> None:
    current = observation(0, 1, 3, native.CostSymbol.X)
    statistics = DeckStatistics()
    stats, uses = statistics.view(current)
    # Six equiprobable two-card hands: one card is unaffordable in (1,3), (1,X), (3,X).
    assert stats == {
        "cards": 4,
        "attacks": 4,
        "skills": 0,
        "powers": 0,
        "unplayable": 0,
        "circulating": 4,
        "base_draw": 2,
        "base_energy": 3,
        "baseline_cycle_turns": 2.0,
        "baseline_hand_cost": 3.5,
        "baseline_unplayed_per_hand": 0.5,
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
    assert stats["baseline_hand_cost"] == 3.0
    assert stats["baseline_unplayed_per_hand"] == 1.0
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


def test_unknown_printed_cost_does_not_claim_a_known_hand_budget() -> None:
    current = observation(1)
    card = current.objects["c0"]
    assert isinstance(card, native.CardEntry)
    current = current.model_copy(
        update={
            "objects": {
                "c0": card.model_copy(
                    update={
                        "cost": native.ResourceCosts(energy=None),
                    }
                )
            }
        }
    )
    stats, _ = DeckStatistics().view(current)
    assert "baseline_hand_cost" not in stats
    assert "baseline_unplayed_per_hand" not in stats
