"""Strict local inputs retain the approved provider-independent schemas."""

from importlib.resources import files
from typing import Literal

from pydantic import GetJsonSchemaHandler, JsonValue, RootModel, TypeAdapter
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


class InspectInput(RootModel[StateQuery | native.InspectQuery]):
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
