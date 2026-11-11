"""Complete state sections delivered to either gameplay role."""

from typing import Literal

from pydantic import MISSING, JsonValue

from sts2_harness.note_values import NotesView
from sts2_harness.results import SemanticValue


class RunObservation(SemanticValue):
    baseline: Literal[True] | MISSING = MISSING
    defs: tuple[dict[str, JsonValue], ...] | MISSING = MISSING
    location: dict[str, JsonValue] | MISSING = MISSING
    progress: dict[str, JsonValue] | MISSING = MISSING
    player: dict[str, JsonValue] | MISSING = MISSING
    deck: tuple[dict[str, JsonValue], ...] | MISSING = MISSING
    notes: NotesView | MISSING = MISSING
    deck_stats: dict[str, JsonValue] | None | MISSING = MISSING
    relics: tuple[dict[str, JsonValue], ...] | MISSING = MISSING
    potions: tuple[dict[str, JsonValue], ...] | MISSING = MISSING
    map_topology: dict[str, JsonValue] | None | MISSING = MISSING
    dynamic_map: dict[str, JsonValue] | None | MISSING = MISSING
    unknown_odds: dict[str, JsonValue] | None | MISSING = MISSING
    turn: int | None | MISSING = MISSING
    hand: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    draw: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    discard: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    exhaust: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    other: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    enemies: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    inactive_enemies: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    allies: tuple[dict[str, JsonValue], ...] | None | MISSING = MISSING
    orbs: dict[str, JsonValue] | None | MISSING = MISSING
    encounter: dict[str, JsonValue] | None | MISSING = MISSING
    mechanisms: tuple[dict[str, JsonValue], ...] | MISSING = MISSING
    generated: tuple[dict[str, JsonValue], ...] | MISSING = MISSING
    result: str | None | MISSING = MISSING
    last_observed_turn: int | None | MISSING = MISSING
    choice: dict[str, JsonValue] | MISSING = MISSING
