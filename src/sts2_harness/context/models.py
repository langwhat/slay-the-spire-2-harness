"""Immutable gameplay messages retain their typed meaning beside rendered text."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import MISSING, JsonValue

from sts2_harness.client import models as native
from sts2_harness.context.run_models import RunObservation
from sts2_harness.decisions import BattleReport, CombatHandoff
from sts2_harness.llm.messages import TextMessage, ToolResultMessage
from sts2_harness.note_values import NotesView
from sts2_harness.results import (
    ActionOutcome,
    ActionToolResult,
    Outcome,
    QueryData,
    SemanticValue,
    ToolResult,
)
from sts2_harness.route_plan import MovementStatus


class TaskKind(StrEnum):
    ACT = "act"
    REVIEW = "review"
    HANDOFF = "handoff"


@dataclass(frozen=True, slots=True)
class CombatActInput:
    opening_notes: NotesView | MISSING = MISSING


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
    player: native.Player
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
    player: native.Player
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
    observation: RunObservation
    opening_notes: NotesView | MISSING = MISSING


@dataclass(frozen=True, slots=True)
class CombatReviewInput:
    battle_report: BattleReport


class ReviewContent(SemanticValue):
    task: Literal[TaskKind.REVIEW] = TaskKind.REVIEW
    battle_report: BattleReport


@dataclass(frozen=True, slots=True)
class RunActInput:
    strategy_notes: NotesView | MISSING = MISSING
    handoff: CombatHandoff | None = None
    previous_battle: CombatHandoff | None = None


@dataclass(frozen=True, slots=True)
class RunHandoffInput:
    handoff: CombatHandoff
    strategy_notes: NotesView | MISSING = MISSING


class PreviousBattle(SemanticValue):
    battle_report: BattleReport
    combat_review: str | None


class RunActContent(SemanticValue):
    task: Literal[TaskKind.ACT] = TaskKind.ACT
    observation: RunObservation
    previous_battle: PreviousBattle | MISSING = MISSING


class RunActHandoffContent(RunActContent):
    battle_report: BattleReport
    combat_review: str | None


class HandoffContent(SemanticValue):
    task: Literal[TaskKind.HANDOFF] = TaskKind.HANDOFF
    battle_report: BattleReport
    combat_review: str | None
    notes: NotesView | MISSING = MISSING


type TaskInput = CombatActInput | CombatReviewInput | RunActInput | RunHandoffInput
type TaskContent = (
    ActContent | ReviewContent | RunActContent | RunActHandoffContent | HandoffContent
)


@dataclass(frozen=True, slots=True)
class BaselineMessage(TextMessage):
    content: dict[str, JsonValue]


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
    route_plan_status: Literal[MovementStatus.INVALIDATED] | MISSING = MISSING
    outcome: ActionOutcome
    observation: RunObservation | None


@dataclass(frozen=True, slots=True)
class RunAutomaticMessage(TextMessage):
    content: RunAutomaticContent
    source: ActionToolResult
