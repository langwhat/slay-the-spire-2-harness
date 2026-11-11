"""Strict local inputs retain the approved provider-independent schemas."""

from enum import StrEnum
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import (
    MISSING,
    Field,
    GetJsonSchemaHandler,
    JsonValue,
    RootModel,
    TypeAdapter,
    field_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from sts2_harness.client import models as native


class Definition(native.WireModel):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


DEFINITIONS = TypeAdapter(tuple[Definition, ...]).validate_json(
    files(__package__).joinpath("definitions.json").read_text(), strict=True
)


class StateQuery(native.WireModel):
    area: Literal["state"]


class UpgradeQuery(native.WireModel):
    area: Literal["upgrades"]
    refs: Annotated[tuple[native.Nonempty, ...], Field(min_length=1)] | MISSING = MISSING


class InspectInput(RootModel[StateQuery | native.MapQuery | UpgradeQuery]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(DEFINITIONS[0].input_schema)


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


class RunAct(native.WireModel):
    steps: Annotated[
        tuple[native.Interact | native.UsePotion | native.DiscardPotion | native.Select, ...],
        Field(min_length=1),
    ]
    reason: str | MISSING = MISSING


class RunActInput(RootModel[RunAct]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_act"))


class DeckChoice(StrEnum):
    TAKE = "take"
    SKIP = "skip"


class DeckClass(StrEnum):
    ENGINE = "engine"
    PIVOT = "pivot"
    ANSWER = "answer"
    FILLER = "filler"


class TakeCard(native.WireModel):
    offer: native.Nonempty
    choice: Literal[DeckChoice.TAKE]
    card: native.Nonempty
    classification: DeckClass = Field(alias="class")
    links: tuple[native.Nonempty, ...]
    reason: str

    @field_validator("links")
    @classmethod
    def distinct_links(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Links must be distinct native IDs.")
        return value


class SkipCard(native.WireModel):
    offer: native.Nonempty
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


class RouteEdge(native.WireModel):
    source: native.Nonempty = Field(alias="from")
    destination: native.Nonempty = Field(alias="to")


class RunPlan(native.WireModel):
    reason: native.Nonempty
    edges: Annotated[tuple[RouteEdge, ...], Field(min_length=1)]

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_plan"))


class RunNext(native.WireModel):
    reason: native.Nonempty

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_next"))


class NotesMode(StrEnum):
    READ = "read"
    REPLACE = "replace"
    EDIT = "edit"


class NotesReadInput(native.WireModel):
    mode: Literal[NotesMode.READ]


class NotesReplaceInput(native.WireModel):
    mode: Literal[NotesMode.REPLACE]
    text: str


class NotesEditInput(native.WireModel):
    mode: Literal[NotesMode.EDIT]
    old_text: native.Nonempty
    new_text: str


class NotesInput(RootModel[NotesReadInput | NotesReplaceInput | NotesEditInput]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_notes"))
