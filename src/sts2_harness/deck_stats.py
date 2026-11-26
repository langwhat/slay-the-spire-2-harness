"""Current deck counts, native base resources, and observed card use."""

from collections import Counter
from enum import StrEnum

from pydantic import MISSING, JsonValue

from sts2_harness.client import models as native


class _CardType(StrEnum):
    ATTACK = "attack"
    SKILL = "skill"
    POWER = "power"


class _CardRarity(StrEnum):
    BASIC = "basic"


class _Keyword(StrEnum):
    UNPLAYABLE = "unplayable"
    EXHAUST = "exhaust"
    ETHEREAL = "ethereal"


class DeckStatistics:
    def __init__(self) -> None:
        self._session: tuple[str, str | None] | None = None
        self._battles: dict[str | None, tuple[native.NamedDefinition, ...]] = {}
        self._choices: dict[int | None, Counter[bool]] = {}

    def _enter(self, observation: native.Observation) -> None:
        location = observation.location
        session = (location.game_instance_id, location.run_id)
        if session == self._session:
            return
        self._battles.clear()
        self._choices.clear()
        self._session = session

    def capture(self, observation: native.Observation, history: native.HistoryData) -> None:
        if not isinstance(observation.state, native.CombatState):
            return
        self._enter(observation)
        self._battles[observation.location.node_id] = history.cards_played_this_combat
        if len(self._battles) > 5:
            del self._battles[next(iter(self._battles))]

    def record_choice(
        self, observation: native.Observation, taken: bool, *, count: int = 1
    ) -> None:
        self._enter(observation)
        self._choices.setdefault(observation.state.progress.act, Counter())[taken] += count

    def view(self, observation: native.Observation) -> tuple[dict[str, JsonValue], dict[str, int]]:
        state = observation.state
        if not isinstance(state, native.RunState):
            raise ValueError("Deck statistics require a Run observation.")
        self._enter(observation)
        cards: list[native.CardEntry] = []
        for reference in state.deck:
            card = observation.objects[reference]
            if not isinstance(card, native.CardEntry):
                raise ValueError("A permanent deck reference must identify a card.")
            cards.append(card)
        kinds = Counter(card.card_type for card in cards)
        keywords = [set(keyword.casefold() for keyword in card.keywords) for card in cards]
        choices = self._choices.get(state.progress.act, Counter())
        circulating = [
            (card, words)
            for card, words in zip(cards, keywords, strict=True)
            if card.card_type != _CardType.POWER
            and _Keyword.EXHAUST not in words
            and not {_Keyword.UNPLAYABLE, _Keyword.ETHEREAL}.issubset(words)
        ]
        fixed_costs = [
            cost
            for card, words in circulating
            if _Keyword.UNPLAYABLE not in words
            and card.playable is not False
            and isinstance(cost := card.cost.energy, int)
        ]
        stats: dict[str, JsonValue] = {
            "cards": len(cards),
            "attacks": kinds[_CardType.ATTACK],
            "skills": kinds[_CardType.SKILL],
            "powers": kinds[_CardType.POWER],
            "basic_cards": sum(
                card.rarity is not None and card.rarity.casefold() == _CardRarity.BASIC
                for card in cards
            ),
            "unplayable": sum(_Keyword.UNPLAYABLE in words for words in keywords),
            "circulating": len(circulating),
            "added": choices[True],
            "skipped": choices[False],
        }
        if fixed_costs:
            stats["circulating_avg_energy"] = round(sum(fixed_costs) / len(fixed_costs), 2)
        draw, energy = state.player.base_draw, state.player.base_energy
        if draw is not MISSING:
            stats["base_draw"] = draw
            if draw > 0:
                stats["cycle_turns"] = round(len(circulating) / draw, 1)
        if energy is not MISSING:
            stats["base_energy"] = energy
        counts = Counter(
            card.definition_id
            for plays in self._battles.values()
            for card in plays
            if card.definition_id is not MISSING
        )
        return stats, dict(counts)
