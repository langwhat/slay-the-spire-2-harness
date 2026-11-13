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


class CardPurpose(StrEnum):
    CORE = "core"
    ENERGY = "energy"
    DRAW = "draw"
    BLOCK = "block"
    TRANSITION = "transition"


class TakeCard(native.WireModel):
    offer: native.Nonempty
    choice: Literal[DeckChoice.TAKE]
    card: native.Nonempty
    reason: str
    purpose: CardPurpose = Field(alias="for")
    over: native.Nonempty | MISSING = MISSING


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
    WRITE = "write"


class NotesReadInput(native.WireModel):
    mode: Literal[NotesMode.READ]


class NotesWriteInput(native.WireModel):
    mode: Literal[NotesMode.WRITE]
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
        if self.model_fields_set == {"mode"}:
            raise ValueError("Supply at least one strategy field to replace.")
        return self


class NotesInput(RootModel[NotesReadInput | NotesWriteInput]):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return dict(next(item.input_schema for item in DEFINITIONS if item.name == "run_notes"))
