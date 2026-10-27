"""Supplementary documents keep their identity separate from native subjects."""

from typing import Protocol

from pydantic import JsonValue, TypeAdapter, model_serializer

from sts2_harness.client import models as native

_JSON = TypeAdapter(dict[str, JsonValue])


class Resource(native.WireModel):
    key: native.Nonempty
    description: str


class DocumentView(native.WireModel):
    id: native.Nonempty
    name: native.Nonempty
    text: str
    resources: tuple[Resource, ...]


class SubjectId(native.WireModel):
    category: native.Nonempty
    definition_id: native.Nonempty


class Document(DocumentView):
    subjects: tuple[SubjectId, ...]

    def view(self) -> DocumentView:
        return DocumentView(id=self.id, name=self.name, text=self.text, resources=self.resources)


class SupplementUnavailable(Exception):
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
