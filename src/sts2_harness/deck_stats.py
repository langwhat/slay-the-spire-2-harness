"""Current deck counts, native base resources, and observed card use."""

from collections import Counter
from dataclasses import dataclass
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


def is_playable_deck_card(card: native.CardEntry) -> bool:
    return card.playable is not False and _Keyword.UNPLAYABLE not in {
        keyword.casefold() for keyword in card.keywords
    }


def _playable_deck_ids(observation: native.Observation) -> frozenset[str]:
    state = observation.state
    if not isinstance(state, (native.RunState, native.CombatState)):
        return frozenset()
    return frozenset(
        card.definition_id
        for reference in state.deck
        if isinstance(card := observation.objects.get(reference), native.CardEntry)
        and is_playable_deck_card(card)
    )


@dataclass(frozen=True, slots=True)
class _BattleSample:
    played: tuple[native.NamedDefinition, ...]
    owned: frozenset[str]


class DeckStatistics:
    def __init__(self) -> None:
        self._session: tuple[str, str | None] | None = None
        self._battles: dict[str | None, _BattleSample] = {}
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
        node_id = observation.location.node_id
        existing = self._battles.get(node_id)
        owned = existing.owned if existing is not None else _playable_deck_ids(observation)
        self._battles[node_id] = _BattleSample(played=cards, owned=owned)
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
        run_added = sum(act_choices[True] for act_choices in self._choices.values())
        run_skipped = sum(act_choices[False] for act_choices in self._choices.values())
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
            "non_circulating": len(cards) - len(circulating),
            "high_cost_circulating": sum(1 for cost in fixed_costs if cost >= 2),
            "added": choices[True],
            "skipped": choices[False],
            "run_added": run_added,
            "run_skipped": run_skipped,
            "battles_observed": len(self._battles),
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
                stats["first_pass_turns"] = round(len(cards) / draw, 1)
                stats["cycle_turns"] = round(len(circulating) / draw, 1)
        if energy is not MISSING:
            stats["base_energy"] = energy
        counts = Counter(
            card.definition_id
            for sample in self._battles.values()
            for card in sample.played
            if card.definition_id is not MISSING
        )
        use_counts: dict[str, int] = dict(counts)
        if self._battles:
            observed_owned = {
                card_id for sample in self._battles.values() for card_id in sample.owned
            }
            for card in cards:
                if is_playable_deck_card(card) and card.definition_id in observed_owned:
                    use_counts.setdefault(card.definition_id, 0)
        return stats, use_counts
