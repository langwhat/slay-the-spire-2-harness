"""Declared material changes preserve typed history and its delivery boundaries."""

from dataclasses import dataclass

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.journal import ArchivedReplacement
from sts2_harness.journal_models import (
    ArchivedData,
    ArchivedText,
    DocumentData,
    DocumentLookupResult,
    DocumentReadResult,
    DocumentSnapshot,
    GuidanceText,
    KnowledgeData,
    MaterialRecord,
    MaterialReply,
    MaterialSnapshot,
    MessageSnapshot,
    ModelRecord,
    NamedText,
    PartsText,
    PromptReadResult,
    ResultSnapshot,
    SkillListingResult,
    SkillsData,
    SkillSnapshot,
    TextSnapshot,
    ToolSnapshot,
)
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    Resource,
    SkillRead,
    SkillSummary,
    SubjectId,
)
from sts2_harness.llm.messages import TextRole, ToolDefinition
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.prompts import PromptReader
from sts2_harness_eval.results import MaterialDifference, Rejected, RejectReason


@dataclass(frozen=True, slots=True)
class HistoricalStart:
    """Rebuild the complete recorded software prefix with frozen historical replies."""


@dataclass(frozen=True, slots=True)
class SceneStart:
    initial_notes: str


@dataclass(frozen=True, slots=True)
class MaterialReplacement:
    material_id: str
    expected_versions: frozenset[str]
    text: str


@dataclass(frozen=True, slots=True)
class MaterialVariant:
    start: HistoricalStart | SceneStart
    replacements: tuple[MaterialReplacement, ...]


class VariantError(MaterialUnavailable):
    """A declared source material cannot be verified or replaced."""

    @property
    def rejection(self) -> Rejected:
        return Rejected(RejectReason.INPUT_MISMATCH, None, MaterialDifference(str(self)))


class Materials:
    def __init__(self, replacements: tuple[ArchivedReplacement, ...]) -> None:
        self._replacements = {item.material_id: item for item in replacements}
        if len(self._replacements) != len(replacements):
            raise VariantError("A material ID is declared more than once.")
        for item in replacements:
            actual = MaterialText.create(item.material_id, item.replacement.body)
            if MaterialSnapshot.of(actual) != item.replacement or not item.expected_versions:
                raise VariantError("A replacement requires its exact identity and source versions.")

    def snapshot(self, source: MaterialSnapshot) -> MaterialSnapshot:
        replacement = self._replacements.get(source.id)
        if replacement is None:
            return source
        if MaterialSnapshot.of(MaterialText.create(source.id, source.body)) != source:
            raise VariantError(f"Material {source.id} has inconsistent source evidence.")
        if source.version not in replacement.expected_versions:
            raise VariantError(
                f"Material {source.id} has an undeclared source version: {source.version}."
            )
        return replacement.replacement

    def text(self, source: MaterialText) -> MaterialText:
        result = self.snapshot(MaterialSnapshot.of(source))
        return MaterialText(id=result.id, version=result.version, body=result.body)

    def document(self, source: DocumentView) -> DocumentView:
        return source.model_copy(
            update={
                "name": self.text(source.name),
                "text": self.text(source.text),
                "resources": tuple(
                    Resource(key=item.key, description=self.text(item.description))
                    for item in source.resources
                ),
            }
        )

    def skill(self, source: SkillSummary) -> SkillSummary:
        return source.model_copy(
            update={"name": self.text(source.name), "description": self.text(source.description)}
        )

    def tools(self, source: tuple[ToolDefinition, ...]) -> tuple[ToolDefinition, ...]:
        return tuple(
            ToolDefinition(item.name, self.text(item.description), item.parameters)
            for item in source
        )

    def document_snapshot(self, source: DocumentSnapshot) -> DocumentSnapshot:
        return source.model_copy(
            update={
                "name": self.snapshot(source.name),
                "text": self.snapshot(source.text),
                "resources": tuple(
                    item.model_copy(update={"description": self.snapshot(item.description)})
                    for item in source.resources
                ),
            }
        )

    def skill_snapshot(self, source: SkillSnapshot) -> SkillSnapshot:
        return source.model_copy(
            update={
                "name": self.snapshot(source.name),
                "description": self.snapshot(source.description),
            }
        )

    def data(self, source: ArchivedData | MISSING) -> ArchivedData | MISSING:
        if isinstance(source, DocumentData):
            return source.model_copy(update={"document": self.document_snapshot(source.document)})
        if isinstance(source, SkillsData):
            return source.model_copy(
                update={"skills": tuple(self.skill_snapshot(item) for item in source.skills)}
            )
        if isinstance(source, KnowledgeData):
            return source.model_copy(
                update={
                    "entries": tuple(
                        item.model_copy(
                            update={
                                "supplements": tuple(
                                    self.document_snapshot(doc) for doc in item.supplements
                                )
                            }
                        )
                        for item in source.entries
                    )
                }
            )
        return source

    def archived_text(self, source: ArchivedText) -> ArchivedText:
        if isinstance(source, NamedText):
            return source.model_copy(update={"material": self.snapshot(source.material)})
        if isinstance(source, PartsText):
            return source.model_copy(
                update={
                    "parts": tuple(
                        self.archived_text(part) if isinstance(part, NamedText) else part
                        for part in source.parts
                    )
                }
            )
        if isinstance(source, GuidanceText):
            return source.model_copy(
                update={"documents": tuple(self.document_snapshot(doc) for doc in source.documents)}
            )
        return source

    def messages(
        self, source: tuple[MessageSnapshot, ...], repeated_tasks: frozenset[int] = frozenset()
    ) -> tuple[MessageSnapshot, ...]:
        delivered: dict[tuple[str, str | None], DocumentSnapshot] = {}
        mapped_deliveries: set[tuple[str, str | None]] = set()
        result: list[MessageSnapshot] = []

        def remember(document: DocumentSnapshot, changed: bool) -> None:
            # Subjects describe lookup provenance, not the DocumentView delivered to Context.
            view = document.model_copy(update={"subjects": MISSING})
            key = (view.id, None if view.resource is MISSING else view.resource)
            delivered[key] = view
            if changed:
                mapped_deliveries.add(key)

        guidance_removed = False
        for index, message in enumerate(source):
            if guidance_removed and index in repeated_tasks:
                guidance_removed = False
                continue
            guidance_removed = False
            if isinstance(message, TextSnapshot):
                value = self.archived_text(message.value)
                if isinstance(value, GuidanceText):
                    documents: list[DocumentSnapshot] = []
                    assert isinstance(message.value, GuidanceText)
                    for original, document in zip(
                        message.value.documents, value.documents, strict=True
                    ):
                        key = (
                            document.id,
                            None if document.resource is MISSING else document.resource,
                        )
                        view = document.model_copy(update={"subjects": MISSING})
                        duplicate = delivered.get(key) == view
                        affected = original != document or key in mapped_deliveries
                        if message.role is TextRole.USER and affected and duplicate:
                            continue
                        documents.append(document)
                        remember(document, original != document)
                    if not documents and value.documents:
                        guidance_removed = True
                        continue
                    value = value.model_copy(update={"documents": tuple(documents)})
                result.append(message.model_copy(update={"value": value}))
                continue
            if isinstance(message, ResultSnapshot):
                data = self.data(message.data)
                if isinstance(data, DocumentData):
                    assert isinstance(message.data, DocumentData)
                    remember(data.document, message.data.document != data.document)
                elif isinstance(data, KnowledgeData):
                    assert isinstance(message.data, KnowledgeData)
                    for original, entry in zip(message.data.entries, data.entries, strict=True):
                        for old, document in zip(
                            original.supplements, entry.supplements, strict=True
                        ):
                            remember(document, old != document)
                result.append(message.model_copy(update={"data": data}))
                continue
            result.append(message)
        return tuple(result)

    def tool_snapshots(self, source: tuple[ToolSnapshot, ...]) -> tuple[ToolSnapshot, ...]:
        return tuple(
            item.model_copy(update={"description": self.snapshot(item.description)})
            for item in source
        )

    def reply(self, source: MaterialReply) -> MaterialReply:
        if isinstance(source, PromptReadResult):
            return source.model_copy(update={"prompt": self.snapshot(source.prompt)})
        if isinstance(source, DocumentReadResult):
            return source.model_copy(update={"document": self.document_snapshot(source.document)})
        if isinstance(source, DocumentLookupResult):
            return source.model_copy(
                update={"documents": tuple(self.document_snapshot(doc) for doc in source.documents)}
            )
        if isinstance(source, SkillListingResult):
            return source.model_copy(
                update={"skills": tuple(self.skill_snapshot(skill) for skill in source.skills)}
            )
        return source

    def preflight(self, records: tuple[MaterialRecord | ModelRecord, ...]) -> None:
        seen: dict[str, set[str]] = {identity: set() for identity in self._replacements}

        # The same typed traversal verifies reads and every assembled model request.
        class CollectingMaterials(Materials):
            def snapshot(self, source: MaterialSnapshot) -> MaterialSnapshot:
                if source.id in seen:
                    seen[source.id].add(source.version)
                return super().snapshot(source)

        collector = CollectingMaterials(tuple(self._replacements.values()))
        for record in records:
            if isinstance(record, ModelRecord):
                collector.messages(record.input)
                collector.tool_snapshots(record.tools)
                continue
            if record.reply is not None:
                collector.reply(record.reply)
        for identity, replacement in self._replacements.items():
            if frozenset(seen[identity]) != replacement.expected_versions:
                raise VariantError(
                    f"Material {identity} requires its complete recorded source-version set."
                )


def replacements(variant: MaterialVariant) -> tuple[ArchivedReplacement, ...]:
    return tuple(
        ArchivedReplacement(
            material_id=item.material_id,
            expected_versions=item.expected_versions,
            replacement=MaterialSnapshot.of(MaterialText.create(item.material_id, item.text)),
        )
        for item in variant.replacements
    )


class VariantPrompts:
    def __init__(self, source: PromptReader, materials: Materials) -> None:
        self._source = source
        self._materials = materials

    def read(self, name: str) -> MaterialText:
        return self._materials.text(self._source.read(name))


class VariantDocuments:
    def __init__(self, source: Documents, materials: Materials) -> None:
        self._source = source
        self._materials = materials

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        return tuple(self._materials.skill(item) for item in await self._source.list(role))

    async def read(self, request: SkillRead) -> DocumentView:
        return self._materials.document(await self._source.read(request))

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        result: list[Document] = []
        for document in await self._source.lookup(subjects):
            view = self._materials.document(document)
            result.append(
                Document(
                    id=view.id,
                    name=view.name,
                    text=view.text,
                    resources=view.resources,
                    resource=view.resource,
                    subjects=document.subjects,
                )
            )
        return tuple(result)
