"""Immutable gameplay messages retain their typed meaning beside rendered text."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.context.run_models import RunObservation
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
    player: native.RunPlayer
    deck: tuple[str, ...]
    encounter: native.NamedDefinition | MISSING = MISSING


class FinishedState(ForeignState):
    phase: Literal[native.Phase.TERMINAL]
    screen: native.TerminalScreen
    player: native.Player
    result: native.TerminalResult
    deck: tuple[str, ...]
    last_observed_turn: int | None | MISSING = MISSING
    enemies: tuple[native.Enemy, ...] | MISSING = MISSING


class RunFinishedState(ForeignState):
    phase: Literal[native.Phase.TERMINAL]
    screen: native.TerminalScreen
    player: native.RunPlayer
    result: native.TerminalResult
    deck: tuple[str, ...]
    last_observed_turn: int | None | MISSING = MISSING


type ShortState = (
    native.State | ForeignRunState | ForeignCombatState | FinishedState | RunFinishedState
)


class VisibleObservation(SemanticValue):
    state: ShortState
    objects: native.Directory[native.ObjectEntry]
    descriptions: native.Directory[str | None]
    modifier_descriptions: native.Directory[native.ModifierDescriptions]
    upgrades: native.Directory[native.UpgradePreview]
    generated: native.Directory[tuple[native.GeneratedCard, ...]]
    mechanisms: tuple[native.MechanismView, ...]
    map: native.PublicMap | MISSING = MISSING


class ActContent(SemanticValue):
    task: Literal[TaskKind.ACT] = TaskKind.ACT
    instruction: str = "Make the next battle decision using combat_act."
    observation: RunObservation
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
    observation: RunObservation
    strategy_notes: str | MISSING = MISSING


class RunActHandoffContent(RunActContent):
    battle_report: BattleReport
    combat_review: str | None


class HandoffContent(SemanticValue):
    task: Literal[TaskKind.HANDOFF] = TaskKind.HANDOFF
    instruction: str = (
        "Assess the battle report and any Combat review using available evidence. "
        "Update strategy notes if useful, then finish with a concise response. "
        "Do not perform game actions."
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


class RunProjectedResult(SemanticValue):
    outcome: Outcome
    observation: RunObservation | None
    data: QueryData | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class RunResultMessage(ToolResultMessage):
    content: RunProjectedResult
    source: ToolResult


class AutomaticProgress(SemanticValue):
    label: str
    reason: str


class RunAutomaticContent(SemanticValue):
    automatic_progress: AutomaticProgress
    outcome: ActionOutcome
    observation: RunObservation | None


@dataclass(frozen=True, slots=True)
class RunAutomaticMessage(TextMessage):
    content: RunAutomaticContent
    source: ActionToolResult
