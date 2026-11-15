"""Read skill and supplementary documents from explicit file mappings."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pydantic import MISSING

from sts2_harness.client.models import DecisionOwner
from sts2_harness.knowledge import (
    Document,
    DocumentView,
    MaterialRole,
    Resource,
    SkillRead,
    SkillSummary,
    SubjectId,
    UnknownDocument,
)
from sts2_harness.material_text import MaterialText, MaterialUnavailable


@dataclass(frozen=True, slots=True)
class ResourceSource:
    key: str
    description: str
    path: Path


@dataclass(frozen=True, slots=True)
class DocumentSource:
    id: str
    name: str
    description: str
    path: Path
    resources: tuple[ResourceSource, ...] = ()
    subjects: tuple[SubjectId, ...] = ()
    auto_roles: tuple[MaterialRole, ...] = (MaterialRole.RUN, MaterialRole.COMBAT)


class FileDocuments:
    def __init__(self, sources: tuple[DocumentSource, ...] = ()) -> None:
        self._sources = {source.id: source for source in sources}
        if len(self._sources) != len(sources):
            raise ValueError("Document identities must be unique.")
        for source in sources:
            if len({resource.key for resource in source.resources}) != len(source.resources):
                raise ValueError("Resource keys must be unique within a document.")

    async def list(self, role: DecisionOwner) -> tuple[SkillSummary, ...]:
        return tuple(
            SkillSummary(
                id=source.id,
                name=MaterialText.create(f"document:{quote(source.id, safe='')}:name", source.name),
                description=MaterialText.create(
                    f"document:{quote(source.id, safe='')}:description", source.description
                ),
            )
            for source in self._sources.values()
        )

    async def read(self, request: SkillRead) -> DocumentView:
        source = self._sources.get(request.id)
        if source is None:
            raise UnknownDocument(f"Document {request.id!r} is not configured.")
        path = source.path
        identity = f"document:{quote(source.id, safe='')}:text"
        if request.resource is not None:
            resource = next(
                (item for item in source.resources if item.key == request.resource), None
            )
            if resource is None:
                raise UnknownDocument(
                    f"Resource {request.resource!r} is not advertised by document {source.id!r}."
                )
            path = resource.path
            identity = (
                f"document:{quote(source.id, safe='')}:resource:{quote(resource.key, safe='')}:text"
            )
        label = f"Document {source.id!r}"
        if request.resource is not None:
            label += f" resource {request.resource!r}"
        try:
            text = MaterialText.read(identity, path)
        except MaterialUnavailable as error:
            raise MaterialUnavailable(f"{label} could not be read.") from error
        return DocumentView(
            id=source.id,
            name=MaterialText.create(f"document:{quote(source.id, safe='')}:name", source.name),
            text=text,
            resources=tuple(
                Resource(
                    key=item.key,
                    description=MaterialText.create(
                        f"document:{quote(source.id, safe='')}:resource:"
                        f"{quote(item.key, safe='')}:description",
                        item.description,
                    ),
                )
                for item in source.resources
            ),
            resource=request.resource if request.resource is not None else MISSING,
        )

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        documents: list[Document] = []
        for source in self._sources.values():
            if not any(subject in subjects for subject in source.subjects):
                continue
            view = await self.read(SkillRead(id=source.id))
            documents.append(
                Document(
                    id=view.id,
                    name=view.name,
                    text=view.text,
                    resources=view.resources,
                    subjects=source.subjects,
                    auto_roles=source.auto_roles,
                )
            )
        return tuple(documents)
