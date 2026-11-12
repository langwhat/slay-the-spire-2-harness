"""Strategy values shared by notes, tools and recorded model inputs."""

from typing import Annotated, Literal

from pydantic import Field

from sts2_harness.client import models as native


class NotesState(native.WireModel):
    plan: Annotated[str, Field(max_length=800)] = ""
    other: str = ""


class NotesView(NotesState):
    def to_json(self) -> str:
        return self.model_dump_json()


type InitialNotes = NotesState | Literal[""]
