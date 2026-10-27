"""Immutable gameplay messages retain their typed meaning beside rendered text."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.decisions import BattleReport
from sts2_harness.llm.messages import TextMessage, ToolResultMessage
from sts2_harness.results import Outcome, QueryData, SemanticValue, ToolResult


class TaskKind(StrEnum):
    ACT = "act"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class CombatActInput:
    opening_notes: str | MISSING = MISSING


class Updates(SemanticValue):
    objects: native.Directory[native.ObjectEntry] | MISSING = MISSING
    descriptions: native.Directory[str | None] | MISSING = MISSING
    modifier_descriptions: native.Directory[native.ModifierDescriptions] | MISSING = MISSING
    upgrades: native.Directory[native.UpgradePreview] | MISSING = MISSING
    generated: native.Directory[tuple[native.GeneratedCard, ...]] | MISSING = MISSING
    mechanisms: tuple[native.MechanismView, ...] | MISSING = MISSING
    map: native.PublicMap | MISSING = MISSING


class ForeignState(SemanticValue):
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


class VisibleObservation(SemanticValue):
    state: ShortState
    objects: native.Directory[native.ObjectEntry]
    descriptions: native.Directory[str | None]
    modifier_descriptions: native.Directory[native.ModifierDescriptions]
    upgrades: native.Directory[native.UpgradePreview]
    generated: native.Directory[tuple[native.GeneratedCard, ...]]
    mechanisms: tuple[native.MechanismView, ...]
    map: native.PublicMap | MISSING = MISSING


class ActObservation(SemanticValue):
    state: ShortState
    updates: Updates


class ActContent(SemanticValue):
    task: Literal[TaskKind.ACT] = TaskKind.ACT
    instruction: str = "Make the next battle decision using combat_act."
    observation: ActObservation
    opening_notes: str | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class CombatReviewInput:
    battle_report: BattleReport


class ReviewContent(SemanticValue):
    task: Literal[TaskKind.REVIEW] = TaskKind.REVIEW
    instruction: str = (
        "Review this battle for Run using its retained history and the supplied report. "
        "Do not perform game actions."
    )
    battle_report: BattleReport


type CombatInput = CombatActInput | CombatReviewInput


@dataclass(frozen=True, slots=True)
class TaskMessage(TextMessage):
    content: ActContent | ReviewContent
    task: CombatInput
    observation: native.Observation | None


class ProjectedResult(SemanticValue):
    outcome: Outcome
    state: ShortState | None
    updates: Updates
    data: QueryData | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class ResultMessage(ToolResultMessage):
    content: ProjectedResult
    source: ToolResult
