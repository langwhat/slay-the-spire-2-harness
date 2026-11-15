"""Recent completed battles, measured at trusted native observation boundaries."""

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

    def observe(self, observation: native.Observation) -> None:
        location, state = observation.location, observation.state
        identity = (location.game_instance_id, location.run_id)
        if identity != self._identity:
            self.recent = ()
            self._opening = None
            self._encounters.clear()
            self._identity = identity
        if isinstance(state, native.TerminalState):
            self._opening = None
            self._encounters.clear()
            return
        if isinstance(state, native.CombatState):
            if self._opening is None or location.node_id != self._opening.location.node_id:
                self._opening = observation
                self._encounters.clear()
            encounter = observation.encounter
            if encounter is not MISSING and encounter.definition_id is not MISSING:
                self._encounters.add((encounter.definition_id, encounter.name))
            return
        if self._opening is None or observation.input_readiness not in (
            native.InputReadiness.READY,
            native.InputReadiness.SELECTION,
        ):
            return
        if state.battle_turns is None:
            return
        opening = self._opening.state
        assert isinstance(opening, native.CombatState)
        if state.progress.floor != opening.progress.floor:
            self._opening = None
            self._encounters.clear()
            return
        match opening.progress.room_type:
            case "elite":
                kind = FightKind.ELITE
            case "boss":
                kind = FightKind.BOSS
            case _:
                kind = FightKind.NORMAL
        encounter = None
        if len(self._encounters) == 1:
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
        self._opening = None
        self._encounters.clear()
