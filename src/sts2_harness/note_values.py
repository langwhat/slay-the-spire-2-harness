"""Strategy values shared by notes, tools and recorded model inputs."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from sts2_harness.client import models as native


class NotesState(native.WireModel):
    core: Annotated[tuple[native.Nonempty, ...], Field(max_length=5)] = ()
    plan: Annotated[str, Field(max_length=300)] = ""
    other: str = ""

    @field_validator("core")
    @classmethod
    def distinct_core(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Core entries must be distinct.")
        return value


class NotesView(NotesState):
    def to_json(self) -> str:
        return self.model_dump_json()


type InitialNotes = NotesState | Literal[""]


def owned_definition_ids(observation: native.Observation) -> frozenset[str]:
    return frozenset(
        entry.definition_id
        for ref in (*observation.state.deck, *observation.state.relics)
        if isinstance(entry := observation.objects.get(ref), (native.CardEntry, native.RelicEntry))
    )
