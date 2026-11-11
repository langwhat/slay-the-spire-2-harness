"""Deck arithmetic under explicit native base draw and energy assumptions."""

from collections import Counter
from enum import StrEnum
from itertools import combinations
from math import comb
from random import Random

from pydantic import MISSING, JsonValue

from sts2_harness.client import models as native


class _CardType(StrEnum):
    ATTACK = "attack"
    SKILL = "skill"
    POWER = "power"


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

    def record_choice(self, observation: native.Observation, taken: bool) -> None:
        self._enter(observation)
        self._choices.setdefault(observation.state.progress.act, Counter())[taken] += 1

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
        circulating = sum(
            card.card_type != _CardType.POWER
            and _Keyword.EXHAUST not in words
            and not {_Keyword.UNPLAYABLE, _Keyword.ETHEREAL}.issubset(words)
            for card, words in zip(cards, keywords, strict=True)
        )
        stats: dict[str, JsonValue] = {
            "cards": len(cards),
            "attacks": kinds[_CardType.ATTACK],
            "skills": kinds[_CardType.SKILL],
            "powers": kinds[_CardType.POWER],
            "unplayable": sum(_Keyword.UNPLAYABLE in words for words in keywords),
            "circulating": circulating,
            "added": choices[True],
            "skipped": choices[False],
        }
        draw, energy = state.player.base_draw, state.player.base_energy
        if draw is not MISSING:
            stats["base_draw"] = draw
            if draw > 0:
                stats["baseline_cycle_turns"] = round(circulating / draw, 1)
        if energy is not MISSING:
            stats["base_energy"] = energy
        if draw is not MISSING and energy is not MISSING:
            costs = [
                None if _Keyword.UNPLAYABLE in words else _energy_cost(card, energy)
                for card, words in zip(cards, keywords, strict=True)
            ]
            unknown_cost = any(
                cost is None and _Keyword.UNPLAYABLE not in words
                for cost, words in zip(costs, keywords, strict=True)
            )
            if not unknown_cost:
                costs.sort(key=lambda cost: cost if cost is not None else float("inf"))
                hand_size = min(draw, len(costs))
                stats["baseline_hand_cost"] = (
                    round(
                        sum(cost for cost in costs if cost is not None) * hand_size / len(costs), 1
                    )
                    if costs
                    else 0.0
                )
                stats["baseline_unplayed_per_hand"] = _unplayed(costs, hand_size, energy)
        counts = Counter(
            card.definition_id
            for plays in self._battles.values()
            for card in plays
            if card.definition_id is not MISSING
        )
        return stats, dict(counts)


def _energy_cost(card: native.CardEntry, energy: int) -> int | None:
    cost = card.cost.energy
    if cost is native.CostSymbol.X:
        return energy
    if cost is native.CostSymbol.NONE:
        return 0
    return cost if isinstance(cost, int) else None


def _unplayed(costs: list[int | None], draw: int, energy: int) -> float:
    if draw == 0:
        return 0.0
    rng = Random(0)
    hands = (
        combinations(costs, draw)
        if comb(len(costs), draw) <= 2000
        else (tuple(rng.sample(costs, draw)) for _ in range(2000))
    )
    total, samples = 0, 0
    for hand in hands:
        remaining, played = energy, 0
        for cost in sorted(cost for cost in hand if cost is not None):
            if cost > remaining:
                break
            remaining -= cost
            played += 1
        total += draw - played
        samples += 1
    return round(total / samples, 1)
