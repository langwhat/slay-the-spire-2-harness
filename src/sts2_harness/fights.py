"""Recent completed battles, measured at trusted native observation boundaries."""

from enum import StrEnum

from pydantic import MISSING

from sts2_harness.client import models as native


class FightKind(StrEnum):
    NORMAL = "normal"
    ELITE = "elite"
    BOSS = "boss"


def fight_kind(room_type: str | None) -> FightKind:
    match room_type:
        case "elite":
            return FightKind.ELITE
        case "boss":
            return FightKind.BOSS
        case _:
            return FightKind.NORMAL


class FightEncounter(native.WireModel):
    definition_id: str
    name: str


class Fight(native.WireModel):
    floor: int | None
    kind: FightKind
    encounter: FightEncounter | None = None
    turns: int | None
    hp_in: int
    hp_out: int


class FightHistory:
    def __init__(self) -> None:
        self.recent: tuple[Fight, ...] = ()
        self._identity: tuple[str, str | None] | None = None
        self._opening: native.Observation | None = None
        self._encounters: set[tuple[str, str]] = set()
        self._encounter_names: set[str] = set()
        self._interrupted = False
        self._ambiguous = False

    def _reset_opening(self) -> None:
        self._opening = None
        self._encounters.clear()
        self._encounter_names.clear()
        self._interrupted = False
        self._ambiguous = False

    def observe(self, observation: native.Observation) -> None:
        location, state = observation.location, observation.state
        identity = (location.game_instance_id, location.run_id)
        if identity != self._identity:
            self.recent = ()
            self._reset_opening()
            self._identity = identity
        if isinstance(state, native.TerminalState):
            self._reset_opening()
            return
        if isinstance(state, native.CombatState):
            if self._opening is None or location.node_id != self._opening.location.node_id:
                self._reset_opening()
                self._opening = observation
            elif self._interrupted:
                self._ambiguous = True
            self._interrupted = False
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
        kind = fight_kind(opening.progress.room_type)
        encounter = None
        if len(self._encounters) == len(self._encounter_names) == 1 and not self._ambiguous:
            definition_id, name = next(iter(self._encounters))
            encounter = FightEncounter(definition_id=definition_id, name=name)
        self.recent = (
            *self.recent,
            Fight(
                floor=opening.progress.floor,
                kind=kind,
                encounter=encounter,
                turns=None if state.battle_turns is MISSING else state.battle_turns,
                hp_in=opening.player.hp,
                hp_out=state.player.hp,
            ),
        )[-6:]
        self._reset_opening()
