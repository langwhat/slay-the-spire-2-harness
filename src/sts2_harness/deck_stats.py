"""Keep measured battle-history prefixes without claiming complete fight telemetry."""

from collections import Counter
from dataclasses import dataclass

from pydantic import MISSING, JsonValue

from sts2_harness.client import models as native


@dataclass(frozen=True, slots=True)
class _BattlePrefix:
    turn: int | None
    plays: tuple[native.NamedDefinition, ...]


class DeckStatistics:
    def __init__(self) -> None:
        self._session: tuple[str, str | None] | None = None
        self._battles: dict[str | None, _BattlePrefix] = {}

    def capture(self, observation: native.Observation, history: native.HistoryData) -> None:
        state = observation.state
        if not isinstance(state, native.CombatState):
            return
        location = observation.location
        session = (location.game_instance_id, location.run_id)
        if session != self._session:
            self._battles.clear()
            self._session = session
        self._battles[location.node_id] = _BattlePrefix(
            state.turn, history.cards_played_this_combat
        )
        if len(self._battles) > 5:
            del self._battles[next(iter(self._battles))]

    def view(self, card_count: int) -> tuple[dict[str, JsonValue], dict[str, int]]:
        stats: dict[str, JsonValue] = {
            "cards": card_count,
            "fights": len(self._battles),
            "history_complete": False,
            "metric_scope": "observed_history_prefix",
        }
        if not self._battles:
            return stats, {}
        plays = [card for prefix in self._battles.values() for card in prefix.plays]
        stats["observed_plays"] = len(plays)
        turns = [prefix.turn for prefix in self._battles.values()]
        if all(turn is not None for turn in turns):
            stats["observed_turns"] = sum(turn for turn in turns if turn is not None)
        counts = Counter(card.definition_id for card in plays if card.definition_id is not MISSING)
        return stats, dict(counts)
