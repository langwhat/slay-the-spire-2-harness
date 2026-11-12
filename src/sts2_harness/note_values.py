"""Structured strategy values shared by notes, tools and recorded model inputs."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import MISSING, Field

from sts2_harness.client import models as native

type CardId = native.Nonempty


class EngineKind(StrEnum):
    GROWTH = "growth"
    CYCLE = "cycle"
    BURST = "burst"


class Engine(native.WireModel):
    kind: EngineKind
    cards: Annotated[tuple[CardId, ...], Field(min_length=1, max_length=8)]
    turn: str
    ready_by: Annotated[int, Field(ge=1)]


class NotesState(native.WireModel):
    engine: Engine | None = None
    wants: Annotated[tuple[native.Nonempty, ...], Field(max_length=3)] = ()
    filler: tuple[CardId, ...] = ()
    evidence: str = ""
    other: str = ""


class NumberedWant(native.WireModel):
    number: Annotated[int, Field(ge=1, le=3)]
    text: native.Nonempty
    expired: Literal[True] | MISSING = MISSING


class NotesView(native.WireModel):
    engine: Engine | None = None
    wants: tuple[NumberedWant, ...] = ()
    filler: tuple[CardId, ...] = ()
    evidence: str = ""
    other: str = ""

    def to_json(self) -> str:
        return self.model_dump_json()


type InitialNotes = NotesState | Literal[""]
