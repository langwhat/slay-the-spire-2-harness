"""Model-independent decisions and immutable combat facts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import MISSING, Field

from sts2_harness.client import models as native
from sts2_harness.results import ActionToolResult, SemanticValue


class ActionKind(StrEnum):
    PLAY_CARD = "play_card"
    USE_POTION = "use_potion"
    DISCARD_POTION = "discard_potion"
    END_TURN = "end_turn"
    INTERACT = "interact"


@dataclass(frozen=True, slots=True)
class ActionStep:
    kind: ActionKind
    ref: str | None = None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class SelectStep:
    refs: tuple[str, ...]


type Step = ActionStep | SelectStep


@dataclass(frozen=True, slots=True)
class ActionProposal:
    steps: tuple[Step, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActionDecision:
    proposal: ActionProposal
    handoff_processed: bool = False


@dataclass(frozen=True, slots=True)
class TaskComplete:
    text: str | None = None


type Decision = ActionDecision | TaskComplete


class BattleLocation(SemanticValue):
    act: Annotated[int, Field(ge=0)] | None
    floor: Annotated[int, Field(ge=0)] | None
    room_type: str | None


class BoundaryValues(SemanticValue):
    hp: Annotated[int, Field(ge=0)]
    max_hp: Annotated[int, Field(ge=0)]
    gold: Annotated[int, Field(ge=0)]


class BattleReport(SemanticValue):
    location: BattleLocation
    result: native.TerminalResult
    last_observed_turn: Annotated[int, Field(ge=0)] | None
    before: BoundaryValues
    after: BoundaryValues
    encounter: native.NamedDefinition | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class CombatHandoff:
    report: BattleReport
    review: str | None = None


@dataclass(frozen=True, slots=True)
class ActTask:
    expected_route: native.RouteKey
    handoff: CombatHandoff | None = None


@dataclass(frozen=True, slots=True)
class ReviewTask:
    report: BattleReport


@dataclass(frozen=True, slots=True)
class HandoffTask:
    handoff: CombatHandoff


type DecisionTask = ActTask | ReviewTask | HandoffTask


class DecisionSource(Protocol):
    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision: ...

    async def accept(self, outcome: ActionToolResult) -> None: ...
