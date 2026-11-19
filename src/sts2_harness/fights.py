"""Recent completed battles, measured at trusted native observation boundaries."""

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from pydantic import MISSING

from sts2_harness.client import models as native


class FightKind(StrEnum):
    NORMAL = "normal"
    ELITE = "elite"
    BOSS = "boss"


class FightEncounter(native.WireModel):
    definition_id: str
    name: str


class Fight(native.WireModel):
    """One battle; each per-turn tuple has one entry per observed player turn."""

    floor: int | None
    kind: FightKind
    encounter: FightEncounter | None = None
    hp_in: int
    hp_out: int
    deck_added: tuple[str, ...] | MISSING = MISSING
    deck_removed: tuple[str, ...] | MISSING = MISSING
    damage: tuple[int, ...]
    damage_one: tuple[int, ...]
    incoming: tuple[int | None, ...]
    hp_lost: tuple[int, ...]
    potions: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _Boundary:
    """State at the start of a player turn, or after the battle."""

    turn: int | None
    hp: int
    enemy_hp: dict[str, int]
    acting: frozenset[str]
    incoming: dict[str, int | None]
    potions: Counter[str]


def _view_key(observation: native.Observation, ref: str) -> str:
    entry = observation.objects.get(ref)
    if entry is None or entry.definition_id is MISSING:
        return ref
    if isinstance(entry, native.CardEntry) and entry.upgrades:
        return f"{entry.definition_id}+{entry.upgrades}"
    return entry.definition_id


def _intent_damage(enemy: native.Enemy) -> int | None:
    total = 0
    for intent in enemy.intents:
        if isinstance(intent, native.HiddenIntent):
            return None
        if intent.damage_per_hit is None:
            return None
        if intent.damage_per_hit is MISSING:
            continue
        hits = intent.hits if isinstance(intent.hits, int) else 1
        total += intent.damage_per_hit * hits
    return total


def _boundary(observation: native.Observation) -> _Boundary:
    state = observation.state
    potions = Counter(_view_key(observation, ref) for ref in state.potions)
    if not isinstance(state, native.CombatState):
        return _Boundary(None, state.player.hp, {}, frozenset(), {}, potions)
    enemies = (*state.enemies, *state.inactive_enemies)
    return _Boundary(
        turn=state.turn,
        hp=state.player.hp,
        enemy_hp={enemy.ref: enemy.hp for enemy in enemies if enemy.hp is not None},
        acting=frozenset(enemy.ref for enemy in state.enemies if enemy.hp != 0),
        incoming={enemy.ref: _intent_damage(enemy) for enemy in state.enemies},
        potions=potions,
    )


def _deck(observation: native.Observation) -> Counter[str]:
    return Counter(_view_key(observation, ref) for ref in observation.state.deck)


def _sorted(counter: Counter[str]) -> tuple[str, ...]:
    return tuple(sorted(counter.elements()))


class FightHistory:
    def __init__(self) -> None:
        self.recent: tuple[Fight, ...] = ()
        self._identity: tuple[str, str | None] | None = None
        self._opening: native.Observation | None = None
        self._encounters: set[tuple[str, str]] = set()
        self._encounter_names: set[str] = set()
        self._interrupted = False
        self._ambiguous = False
        self._boundaries: list[_Boundary] = []
        self._previous_deck: Counter[str] | None = None
        self._deck_change: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())

    def _reset_opening(self) -> None:
        self._opening = None
        self._encounters.clear()
        self._encounter_names.clear()
        self._interrupted = False
        self._ambiguous = False
        self._boundaries = []

    def observe(self, observation: native.Observation) -> None:
        location, state = observation.location, observation.state
        identity = (location.game_instance_id, location.run_id)
        if identity != self._identity:
            self.recent = ()
            self._reset_opening()
            self._previous_deck = None
            self._identity = identity
        if isinstance(state, native.TerminalState):
            self._reset_opening()
            return
        if isinstance(state, native.CombatState):
            if self._opening is None or location.node_id != self._opening.location.node_id:
                self._reset_opening()
                self._opening = observation
                deck = _deck(observation)
                previous = self._previous_deck
                self._deck_change = (
                    ((), ())
                    if previous is None
                    else (_sorted(deck - previous), _sorted(previous - deck))
                )
                self._previous_deck = deck
            elif self._interrupted:
                self._ambiguous = True
            self._interrupted = False
            if state.turn is not None and (
                not self._boundaries or self._boundaries[-1].turn != state.turn
            ):
                self._boundaries.append(_boundary(observation))
            encounter = observation.encounter
            if encounter is not MISSING:
                self._encounter_names.add(encounter.name)
                if encounter.definition_id is not MISSING:
                    self._encounters.add((encounter.definition_id, encounter.name))
            return
        if self._opening is None:
            return
        settled = observation.input_readiness in (
            native.InputReadiness.READY,
            native.InputReadiness.SELECTION,
        )
        if not settled or state.battle_turns is None:
            self._interrupted = True
            return
        opening = self._opening.state
        assert isinstance(opening, native.CombatState)
        if state.progress.floor != opening.progress.floor:
            self._reset_opening()
            return
        match opening.progress.room_type:
            case "elite":
                kind = FightKind.ELITE
            case "boss":
                kind = FightKind.BOSS
            case _:
                kind = FightKind.NORMAL
        encounter = None
        if len(self._encounters) == len(self._encounter_names) == 1 and not self._ambiguous:
            definition_id, name = next(iter(self._encounters))
            encounter = FightEncounter(definition_id=definition_id, name=name)
        added, removed = self._deck_change
        boundaries = (*self._boundaries, _boundary(observation))
        damage: list[int] = []
        damage_one: list[int] = []
        incoming: list[int | None] = []
        hp_lost: list[int] = []
        potions: list[tuple[str, ...]] = []
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            # A round is a player turn and the enemy turn after it; an enemy
            # absent at its end has died or left, and one killed first never acted.
            lost = [max(0, hp - end.enemy_hp.get(ref, 0)) for ref, hp in start.enemy_hp.items()]
            damage.append(sum(lost))
            damage_one.append(max(lost, default=0))
            acted = [start.incoming.get(ref) for ref in start.acting & end.acting]
            incoming.append(None if None in acted else sum(value or 0 for value in acted))
            hp_lost.append(max(0, start.hp - end.hp))
            potions.append(_sorted(start.potions - end.potions))
        self.recent = (
            *self.recent,
            Fight(
                floor=opening.progress.floor,
                kind=kind,
                encounter=encounter,
                hp_in=opening.player.hp,
                hp_out=state.player.hp,
                deck_added=added or MISSING,
                deck_removed=removed or MISSING,
                damage=tuple(damage),
                damage_one=tuple(damage_one),
                incoming=tuple(incoming),
                hp_lost=tuple(hp_lost),
                potions=tuple(potions),
            ),
        )[-6:]
        self._reset_opening()
