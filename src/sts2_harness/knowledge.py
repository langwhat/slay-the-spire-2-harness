"""Supplementary documents keep their identity separate from native subjects."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import MISSING, Field, JsonValue, TypeAdapter, model_serializer

from sts2_harness.card_projection import card_rules, upgrade_changes
from sts2_harness.client import models as native
from sts2_harness.material_text import MaterialText, MaterialUnavailable

_JSON = TypeAdapter(dict[str, JsonValue])


def _compact(value: JsonValue, inherited: JsonValue = None) -> JsonValue:
    if isinstance(value, list):
        return [_compact(child) for child in value]
    if not isinstance(value, dict):
        return value
    known = inherited if isinstance(inherited, dict) else {}
    result: dict[str, JsonValue] = {}
    for name, child in value.items():
        previous = value if name == "upgrade" else known.get(name)
        if name == "stars" and child == "none" and previous in (None, "none"):
            continue
        if (
            name
            in ("should_retain_this_turn", "exhaust_on_next_play", "has_turn_end_in_hand_effect")
            and child is False
            and previous in (None, False)
        ):
            continue
        compact = _compact(child, previous)
        if compact is None or compact == [] or compact == {}:
            if previous is None or previous == [] or previous == {}:
                continue
        result[name] = compact
    return result


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


class MaterialRole(StrEnum):
    RUN = "run"
    DECK = "deck"
    COMBAT = "combat"


class Document(DocumentView):
    subjects: tuple[SubjectId, ...]
    auto_roles: tuple[MaterialRole, ...] = (
        MaterialRole.RUN,
        MaterialRole.COMBAT,
        MaterialRole.DECK,
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
        definition = self.definition
        facts = definition.native
        if isinstance(definition, native.CardDefinition):
            result: dict[str, JsonValue] = {
                "category": definition.category,
                **card_rules(definition.native, definition.native.text),
                "upgrade": upgrade_changes(
                    definition.native, definition.native.text, definition.upgrade
                ),
            }
            raw = _JSON.validate_json(definition.native.to_json())
            for name in (
                "local_keywords",
                "cost_without_global_effects",
                "should_retain_this_turn",
                "exhaust_on_next_play",
                "has_turn_end_in_hand_effect",
                "enchantment",
                "affliction",
            ):
                if name == "local_keywords" and raw.get(name) == raw.get("keywords"):
                    continue
                if name == "cost_without_global_effects" and raw.get(name) == raw.get("cost"):
                    continue
                if name in raw:
                    result[name] = raw[name]
        else:
            result = {
                "category": definition.category,
                "id": definition.definition_id,
                **{
                    name: value
                    for name, value in _JSON.validate_json(facts.to_json()).items()
                    if name != "definition_id"
                },
            }
        compact = _compact(result)
        assert isinstance(compact, dict)
        result = compact
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
