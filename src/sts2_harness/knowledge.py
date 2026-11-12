"""Supplementary documents keep their identity separate from native subjects."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import MISSING, ConfigDict, Field, JsonValue, TypeAdapter, model_serializer

from sts2_harness.client import models as native
from sts2_harness.material_text import MaterialText, MaterialUnavailable

_JSON = TypeAdapter(dict[str, JsonValue])


class Resource(native.WireModel):
    key: native.Nonempty
    description: MaterialText


class DocumentView(native.WireModel):
    id: native.Nonempty
    name: MaterialText
    text: MaterialText
    resources: tuple[Resource, ...]
    resource: native.Nonempty | MISSING = MISSING


class GuidanceContent(native.WireModel):
    guidance: tuple[DocumentView, ...]

    @property
    def body(self) -> str:
        return self.model_dump_json()


class SubjectId(native.WireModel):
    category: native.Nonempty
    definition_id: native.Nonempty


class Document(DocumentView):
    subjects: tuple[SubjectId, ...]
    auto_roles: tuple[native.DecisionOwner, ...] = (
        native.DecisionOwner.RUN,
        native.DecisionOwner.COMBAT,
    )

    def view(self) -> DocumentView:
        return DocumentView(id=self.id, name=self.name, text=self.text, resources=self.resources)


class SupplementUnavailable(MaterialUnavailable):
    """An enabled supplementary source could not supply its requested documents."""


class SupplementProvider(Protocol):
    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]: ...


class KnowledgeEntry(native.WireModel):
    definition: native.NativeDefinition
    supplements: tuple[DocumentView, ...] = ()

    @model_serializer
    def encode(self) -> dict[str, JsonValue]:
        # The native discriminated record is flattened only at the JSON encoding boundary.
        result = _JSON.validate_json(self.definition.model_dump_json(), strict=True)
        result["supplements"] = [
            _JSON.validate_json(document.model_dump_json(), strict=True)
            for document in self.supplements
        ]
        return result


class KnowledgeLookup(native.WireModel):
    entries: tuple[KnowledgeEntry, ...]
    missing_ids: tuple[native.Nonempty, ...]


class UnknownDocument(Exception):
    """The public document identity or advertised resource does not exist."""


@dataclass(frozen=True, slots=True)
class SkillRead:
    id: str
    resource: str | None = None


class SkillSummary(native.WireModel):
    id: native.Nonempty
    name: MaterialText
    description: MaterialText


class SkillList(native.WireModel):
    skills: tuple[SkillSummary, ...]
    total: Annotated[int, Field(ge=0)]
    next_offset: Annotated[int, Field(ge=0)] | None


class SkillProvider(Protocol):
    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]: ...
    async def read(self, request: SkillRead) -> DocumentView: ...


class Documents(SkillProvider, SupplementProvider, Protocol):
    """The document capabilities shared by tools and entry guidance."""


class LinkDirection(StrEnum):
    OWNED_TRIGGERS_CANDIDATE = "owned_triggers_candidate"
    CANDIDATE_TRIGGERS_OWNED = "candidate_triggers_owned"


class CandidateLink(native.WireModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    with_: native.Nonempty = Field(alias="with")
    event: native.Nonempty
    direction: LinkDirection


class CandidateLinks(Protocol):
    async def links(
        self, candidate_ids: tuple[str, ...], owned_ids: tuple[str, ...]
    ) -> dict[str, tuple[CandidateLink, ...]]: ...
