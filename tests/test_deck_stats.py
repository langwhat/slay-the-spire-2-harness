from pydantic import MISSING
from test_mod_client import COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness.deck_stats import DeckStatistics


def combat(node: str, turn: int | None, *, run: str = "run") -> native.Observation:
    observation = native.Observation.model_validate_json(COMBAT)
    state = observation.state
    assert isinstance(state, native.CombatState)
    return observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"node_id": node, "run_id": run}),
            "state": state.model_copy(update={"turn": turn}),
        }
    )


def history(*cards: str | None) -> native.HistoryData:
    return native.HistoryData(
        cards_played_this_combat=tuple(
            native.NamedDefinition(name=card or "Unknown card", definition_id=card or MISSING)
            for card in cards
        ),
        cards_played_this_turn=(),
        cards_exhausted_this_turn=(),
    )


def test_history_snapshots_replace_the_battle_prefix_without_double_counting() -> None:
    statistics = DeckStatistics()
    empty, uses = statistics.view(12)
    assert "observed_plays" not in empty and "observed_turns" not in empty
    assert uses == {}
    statistics.capture(combat("first", 1), history("CARD.A"))
    statistics.capture(combat("first", 1), history("CARD.A"))
    statistics.capture(combat("first", 2), history("CARD.A", "CARD.B", "CARD.A", None))
    statistics.capture(native.Observation.model_validate_json(RUN), history("CARD.IGNORED"))

    stats, uses = statistics.view(13)
    assert stats == {
        "cards": 13,
        "fights": 1,
        "observed_turns": 2,
        "observed_plays": 4,
        "history_complete": False,
        "metric_scope": "observed_history_prefix",
    }
    assert uses == {"CARD.A": 2, "CARD.B": 1}


def test_statistics_keep_only_the_five_latest_sampled_battles() -> None:
    statistics = DeckStatistics()
    for number in range(1, 8):
        statistics.capture(
            combat(f"battle-{number}", number),
            history(*([f"CARD.{number}"] * number)),
        )
    stats, uses = statistics.view(30)
    assert stats["fights"] == 5
    assert stats["observed_plays"] == 25
    assert stats["observed_turns"] == 25
    assert uses == {"CARD.3": 3, "CARD.4": 4, "CARD.5": 5, "CARD.6": 6, "CARD.7": 7}


def test_unknown_turns_stay_absent_and_new_runs_reset_the_window() -> None:
    statistics = DeckStatistics()
    statistics.capture(combat("first", 2), history("CARD.A"))
    statistics.capture(combat("second", None), history("CARD.B", None))
    stats, uses = statistics.view(20)
    assert stats["observed_plays"] == 3 and "observed_turns" not in stats
    assert uses == {"CARD.A": 1, "CARD.B": 1}

    statistics.capture(combat("first", 3, run="new-run"), history("CARD.C"))
    stats, uses = statistics.view(10)
    assert stats["fights"] == 1 and stats["observed_turns"] == 3
    assert stats["observed_plays"] == 1
    assert uses == {"CARD.C": 1}
