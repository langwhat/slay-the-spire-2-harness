"""Current deck counts, native base resources, and observed card use."""

from collections import Counter
from enum import StrEnum

from pydantic import MISSING, JsonValue

from sts2_harness.client import models as native


class _CardType(StrEnum):
    ATTACK = "attack"
    SKILL = "skill"
    POWER = "power"
    CURSE = "curse"
    STATUS = "status"


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

    def _record_battle(
        self, observation: native.Observation, cards: tuple[native.NamedDefinition, ...]
    ) -> None:
        self._enter(observation)
        self._battles[observation.location.node_id] = cards
        if len(self._battles) > 5:
            del self._battles[next(iter(self._battles))]

    def capture(self, observation: native.Observation, history: native.HistoryData) -> None:
        if not isinstance(observation.state, native.CombatState):
            return
        self._record_battle(observation, history.cards_played_this_combat)

    def observe(self, observation: native.Observation) -> None:
        if (
            isinstance(observation.state, native.RunState)
            and observation.state.battle_cards_played is not MISSING
        ):
            self._record_battle(observation, observation.state.battle_cards_played)

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
        card_types = [
            card.card_type.casefold() if card.card_type is not None else None for card in cards
        ]
        kinds = Counter(card_types)
        keywords = [set(keyword.casefold() for keyword in card.keywords) for card in cards]
        choices = self._choices.get(state.progress.act, Counter())
        circulating = [
            (card, card_type, words)
            for card, card_type, words in zip(cards, card_types, keywords, strict=True)
            if card_type != _CardType.POWER
            and _Keyword.EXHAUST not in words
            and not {_Keyword.UNPLAYABLE, _Keyword.ETHEREAL}.issubset(words)
        ]
        fixed_costs = [
            cost
            for card, _, words in circulating
            if _Keyword.UNPLAYABLE not in words
            and card.playable is not False
            and isinstance(cost := card.cost.energy, int)
        ]
        curses_and_statuses = kinds[_CardType.CURSE] + kinds[_CardType.STATUS]
        stats: dict[str, JsonValue] = {
            "cards": len(cards),
            "attacks": kinds[_CardType.ATTACK],
            "skills": kinds[_CardType.SKILL],
            "powers": kinds[_CardType.POWER],
            "basic_cards": sum(
                card.rarity is not None and card.rarity.casefold() == _CardRarity.BASIC
                for card in cards
            ),
            "curses_and_statuses": curses_and_statuses,
            "unplayable": sum(_Keyword.UNPLAYABLE in words for words in keywords),
            "circulating": len(circulating),
            "added": choices[True],
            "skipped": choices[False],
        }
        if circulating:
            junk_circulating = sum(
                (card.rarity is not None and card.rarity.casefold() == _CardRarity.BASIC)
                or card_type in (_CardType.CURSE, _CardType.STATUS)
                for card, card_type, _ in circulating
            )
            stats["basic_and_curse_pct"] = round(100 * junk_circulating / len(circulating), 1)
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
