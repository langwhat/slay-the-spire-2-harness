"""Immutable gameplay messages retain their typed meaning beside rendered text."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import MISSING, Field

from sts2_harness.client import models as native
from sts2_harness.context.results import ContextValue, Outcome, QueryData, ToolResult
from sts2_harness.llm.messages import TextMessage, ToolResultMessage


class TaskKind(StrEnum):
    ACT = "act"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ActTask:
    opening_notes: str | MISSING = MISSING


class Updates(ContextValue):
    objects: native.Directory[native.ObjectEntry] | MISSING = MISSING
    descriptions: native.Directory[str | None] | MISSING = MISSING
    modifier_descriptions: native.Directory[native.ModifierDescriptions] | MISSING = MISSING
    upgrades: native.Directory[native.UpgradePreview] | MISSING = MISSING
    generated: native.Directory[tuple[native.GeneratedCard, ...]] | MISSING = MISSING
    mechanisms: tuple[native.MechanismView, ...] | MISSING = MISSING
    map: native.PublicMap | MISSING = MISSING


class ForeignState(ContextValue):
    control: native.StateControl
    progress: native.Progress
    relics: tuple[str, ...]
    potions: tuple[str, ...]


class ForeignRunState(ForeignState):
    phase: Literal[native.Phase.RUN]
    screen: native.RunScreen
    player: native.RunPlayer
    deck: tuple[str, ...]
    event: native.EventState | None


class ForeignCombatState(ForeignState):
    phase: Literal[native.Phase.COMBAT]
    screen: native.CombatScreen
    player: native.CombatPlayer
    turn: int | None
    piles: native.Piles
    enemies: tuple[native.Enemy, ...]
    inactive_enemies: tuple[native.Enemy, ...]
    orbs: native.Orbs | MISSING = MISSING
    allies: tuple[native.Ally, ...] | MISSING = MISSING


class FinishedState(ForeignState):
    phase: Literal[native.Phase.TERMINAL]
    screen: native.TerminalScreen
    player: native.Player
    result: native.TerminalResult
    deck: tuple[str, ...]
    last_observed_turn: int | None | MISSING = MISSING
    enemies: tuple[native.Enemy, ...] | MISSING = MISSING


type ShortState = native.State | ForeignRunState | ForeignCombatState | FinishedState


class VisibleObservation(ContextValue):
    state: ShortState
    objects: native.Directory[native.ObjectEntry]
    descriptions: native.Directory[str | None]
    modifier_descriptions: native.Directory[native.ModifierDescriptions]
    upgrades: native.Directory[native.UpgradePreview]
    generated: native.Directory[tuple[native.GeneratedCard, ...]]
    mechanisms: tuple[native.MechanismView, ...]
    map: native.PublicMap | MISSING = MISSING


class ActObservation(ContextValue):
    state: ShortState
    updates: Updates


class ActContent(ContextValue):
    task: Literal[TaskKind.ACT] = TaskKind.ACT
    instruction: str = "Make the next battle decision using combat_act."
    observation: ActObservation
    opening_notes: str | MISSING = MISSING


class BattleLocation(ContextValue):
    act: Annotated[int, Field(ge=0)] | None
    floor: Annotated[int, Field(ge=0)] | None
    room_type: str | None


class BoundaryValues(ContextValue):
    hp: Annotated[int, Field(ge=0)]
    max_hp: Annotated[int, Field(ge=0)]
    gold: Annotated[int, Field(ge=0)]


class BattleReport(ContextValue):
    location: BattleLocation
    result: native.TerminalResult
    last_observed_turn: Annotated[int, Field(ge=0)] | None
    before: BoundaryValues
    after: BoundaryValues
    encounter: native.NamedDefinition | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class ReviewTask:
    battle_report: BattleReport


class ReviewContent(ContextValue):
    task: Literal[TaskKind.REVIEW] = TaskKind.REVIEW
    instruction: str = (
        "Review this battle for Run using its retained history and the supplied report. "
        "Do not perform game actions."
    )
    battle_report: BattleReport


type DecisionTask = ActTask | ReviewTask


@dataclass(frozen=True, slots=True)
class TaskMessage(TextMessage):
    content: ActContent | ReviewContent
    task: DecisionTask
    observation: native.Observation | None


class ProjectedResult(ContextValue):
    outcome: Outcome
    state: ShortState | None
    updates: Updates
    data: QueryData | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class ResultMessage(ToolResultMessage):
    content: ProjectedResult
    source: ToolResult
