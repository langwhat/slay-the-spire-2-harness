"""Strict local inputs retain the approved provider-independent schemas."""

from enum import StrEnum
from importlib.resources import files
from typing import Annotated, Literal, Self

from pydantic import (
    MISSING,
    Field,
    GetJsonSchemaHandler,
    JsonValue,
    RootModel,
    TypeAdapter,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from sts2_harness.client import models as native
from sts2_harness.note_values import NotesState


class Definition(native.WireModel):
    name: str
    description: str
    input_schema: dict[str, JsonValue]
    minimal_description: str | None = None


DEFINITIONS = TypeAdapter(tuple[Definition, ...]).validate_json(
    files(__package__).joinpath("definitions.json").read_text(), strict=True
)


class StateQuery(native.WireModel):
    area: Literal["state"]


class RunMapQuery(native.WireModel):
    area: Literal[native.InspectArea.MAP]


class RunInspectInput(RootModel[StateQuery | RunMapQuery]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_inspect"))


class CombatInspectInput(RootModel[StateQuery | native.MapQuery]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(
            next(item.input_schema for item in DEFINITIONS if item.name == "combat_inspect")
        )


class KnowledgeInput(RootModel[native.CatalogRequest]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(DEFINITIONS[1].input_schema)


class SkillMode(StrEnum):
    LIST = "list"
    READ = "read"


class SkillListInput(native.WireModel):
    mode: Literal[SkillMode.LIST]
    text: str | MISSING = MISSING
    offset: Annotated[int, Field(ge=0)] | MISSING = MISSING
    limit: Annotated[int, Field(ge=1, le=20)] | MISSING = MISSING


class SkillReadInput(native.WireModel):
    mode: Literal[SkillMode.READ]
    id: native.Nonempty
    resource: native.Nonempty | MISSING = MISSING


class SkillInput(RootModel[SkillListInput | SkillReadInput]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_skill"))


class CombatAct(native.WireModel):
    steps: Annotated[
        tuple[
            native.PlayCard
            | native.UsePotion
            | native.DiscardPotion
            | native.Interact
            | native.EndTurn
            | native.Select,
            ...,
        ],
        Field(min_length=1),
    ]
    reason: str | MISSING = MISSING


class CombatActInput(RootModel[CombatAct]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "combat_act"))


class NotesPatch(native.WireModel):
    core: Annotated[tuple[native.Nonempty, ...], Field(max_length=5)] | MISSING = MISSING
    plan: Annotated[str, Field(max_length=300)] | MISSING = MISSING
    other: str | MISSING = MISSING

    @model_validator(mode="after")
    def valid_core(self) -> Self:
        if self.core is not MISSING:
            NotesState(core=self.core)
        return self

    @model_validator(mode="after")
    def supplied_fields(self) -> Self:
        if not self.model_fields_set - {"mode"}:
            raise ValueError("Supply at least one strategy field to replace.")
        return self


class RunAct(native.WireModel):
    steps: Annotated[
        tuple[native.Interact | native.UsePotion | native.DiscardPotion | native.Select, ...],
        Field(min_length=1),
    ]
    reason: str | MISSING = MISSING
    notes: NotesPatch | MISSING = MISSING


class RunActInput(RootModel[RunAct]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_act"))


class MinimalRunActInput(RunActInput):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return _without_notes(super().__get_pydantic_json_schema__(core_schema, handler))


class DeckChoice(StrEnum):
    TAKE = "take"
    SKIP = "skip"


class TakeCard(native.WireModel):
    offer: native.Nonempty | MISSING = MISSING
    choice: Literal[DeckChoice.TAKE]
    card: native.Nonempty | Annotated[tuple[native.Nonempty, ...], Field(min_length=1)]
    reason: str

    @property
    def cards(self) -> tuple[str, ...]:
        return (self.card,) if isinstance(self.card, str) else self.card


class SkipCard(native.WireModel):
    offer: native.Nonempty | MISSING = MISSING
    choice: Literal[DeckChoice.SKIP]
    reason: str


class RunDeckChoice(RootModel[TakeCard | SkipCard]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(
            next(item.input_schema for item in DEFINITIONS if item.name == "run_deck_choice")
        )


class RunPlan(native.WireModel):
    reason: native.Nonempty
    stretches: Annotated[tuple[native.Nonempty, ...], Field(min_length=1)]

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_plan"))


class RunNext(native.WireModel):
    reason: native.Nonempty
    notes: NotesPatch | MISSING = MISSING
    stretches: Annotated[tuple[native.Nonempty, ...], Field(min_length=1)] | MISSING = MISSING

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_next"))


class MinimalRunNext(RunNext):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return _without_notes(super().__get_pydantic_json_schema__(core_schema, handler))


def _without_notes(schema: JsonSchemaValue) -> JsonSchemaValue:
    return schema | {
        "properties": {
            name: value for name, value in schema["properties"].items() if name != "notes"
        }
    }


class NotesMode(StrEnum):
    READ = "read"
    WRITE = "write"


class NotesReadInput(native.WireModel):
    mode: Literal[NotesMode.READ]


class NotesWriteInput(NotesPatch):
    mode: Literal[NotesMode.WRITE]


class NotesInput(RootModel[NotesReadInput | NotesWriteInput]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_notes"))
