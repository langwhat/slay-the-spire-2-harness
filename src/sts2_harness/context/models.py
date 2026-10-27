"""Immutable gameplay messages retain their typed meaning beside rendered text."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.decisions import BattleReport, CombatHandoff
from sts2_harness.llm.messages import TextMessage, ToolResultMessage
from sts2_harness.results import (
    ActionOutcome,
    ActionToolResult,
    Outcome,
    QueryData,
    SemanticValue,
    ToolResult,
)


class TaskKind(StrEnum):
    ACT = "act"
    REVIEW = "review"
    HANDOFF = "handoff"


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


@dataclass(frozen=True, slots=True)
class RunActInput:
    strategy_notes: str | MISSING = MISSING
    handoff: CombatHandoff | None = None


@dataclass(frozen=True, slots=True)
class RunHandoffInput:
    handoff: CombatHandoff
    strategy_notes: str | MISSING = MISSING


class RunActContent(SemanticValue):
    task: Literal[TaskKind.ACT] = TaskKind.ACT
    instruction: str = "Make the next run decision using run_act."
    observation: ActObservation
    strategy_notes: str | MISSING = MISSING


class RunActHandoffContent(RunActContent):
    battle_report: BattleReport
    combat_review: str | None


class HandoffContent(SemanticValue):
    task: Literal[TaskKind.HANDOFF] = TaskKind.HANDOFF
    instruction: str = (
        "Assess the battle report and any Combat review. Update strategy notes if useful, "
        "then finish with a concise response. Do not perform game actions."
    )
    battle_report: BattleReport
    combat_review: str | None
    strategy_notes: str | MISSING = MISSING


type TaskInput = CombatActInput | CombatReviewInput | RunActInput | RunHandoffInput
type TaskContent = (
    ActContent | ReviewContent | RunActContent | RunActHandoffContent | HandoffContent
)


@dataclass(frozen=True, slots=True)
class TaskMessage(TextMessage):
    content: TaskContent
    task: TaskInput
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


class AutomaticProgress(SemanticValue):
    label: str
    reason: str


class AutomaticContent(SemanticValue):
    automatic_progress: AutomaticProgress
    outcome: ActionOutcome
    state: ShortState | None
    updates: Updates


@dataclass(frozen=True, slots=True)
class AutomaticMessage(TextMessage):
    content: AutomaticContent
    source: ActionToolResult
