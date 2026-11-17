"""Serve supplementary documents from a fixed per-evaluation snapshot."""

import json
from hashlib import sha256
from typing import Self

from pydantic import MISSING, JsonValue, model_validator

from sts2_harness.client import models as native
from sts2_harness.knowledge import (
    Document,
    DocumentView,
    SkillRead,
    SkillSummary,
    SubjectId,
)
from sts2_harness.material_text import MaterialText
from sts2_harness_eval.frozen_catalog import SnapshotIncomplete


class FrozenSkillList(native.WireModel):
    role: native.DecisionOwner
    skills: tuple[SkillSummary, ...]


class FrozenDocuments(native.WireModel):
    complete_subjects: bool
    covered_subjects: tuple[SubjectId, ...]
    documents: tuple[Document, ...]
    resources: tuple[DocumentView, ...] = ()
    skill_lists: tuple[FrozenSkillList, ...] = ()

    @model_validator(mode="after")
    def verified_text_versions(self) -> Self:
        texts = [
            text
            for document in (*self.documents, *self.resources)
            for text in (
                document.name,
                document.text,
                *(item.description for item in document.resources),
            )
        ]
        texts.extend(
            text
            for listing in self.skill_lists
            for skill in listing.skills
            for text in (skill.name, skill.description)
        )
        if any(text.version != sha256(text.body.encode("utf-8")).hexdigest() for text in texts):
            raise ValueError("A frozen material version does not match its text.")
        return self

    def snapshot_json(self) -> str:
        def text(value: MaterialText) -> dict[str, JsonValue]:
            return {"id": value.id, "version": value.version, "body": value.body}

        def view(value: DocumentView) -> dict[str, JsonValue]:
            result: dict[str, JsonValue] = {
                "id": value.id,
                "name": text(value.name),
                "text": text(value.text),
                "resources": [
                    {"key": item.key, "description": text(item.description)}
                    for item in value.resources
                ],
            }
            if value.resource is not MISSING:
                result["resource"] = value.resource
            return result

        documents: list[JsonValue] = []
        for document in self.documents:
            encoded = view(document)
            encoded["subjects"] = [
                {"category": subject.category, "definition_id": subject.definition_id}
                for subject in document.subjects
            ]
            encoded["auto_roles"] = [role.value for role in document.auto_roles]
            documents.append(encoded)
        return (
            json.dumps(
                {
                    "complete_subjects": self.complete_subjects,
                    "covered_subjects": [
                        {"category": subject.category, "definition_id": subject.definition_id}
                        for subject in self.covered_subjects
                    ],
                    "documents": documents,
                    "resources": [view(resource) for resource in self.resources],
                    "skill_lists": [
                        {
                            "role": listing.role.value,
                            "skills": [
                                {
                                    "id": skill.id,
                                    "name": text(skill.name),
                                    "description": text(skill.description),
                                }
                                for skill in listing.skills
                            ],
                        }
                        for listing in self.skill_lists
                    ],
                },
                indent=2,
            )
            + "\n"
        )

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        if not self.complete_subjects and any(
            subject not in self.covered_subjects for subject in subjects
        ):
            raise SnapshotIncomplete("The snapshot does not cover every requested subject.")
        return tuple(
            document
            for document in self.documents
            if any(subject in document.subjects for subject in subjects)
        )

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        listing = next((item for item in self.skill_lists if item.role is role), None)
        if listing is None:
            raise SnapshotIncomplete("The snapshot does not contain this role's skill list.")
        return listing.skills

    async def read(self, request: SkillRead) -> DocumentView:
        if request.resource is not None:
            resource = next(
                (
                    item
                    for item in self.resources
                    if item.id == request.id and item.resource == request.resource
                ),
                None,
            )
            if resource is None:
                raise SnapshotIncomplete("The requested resource is not frozen.")
            return resource
        document = next((item for item in self.documents if item.id == request.id), None)
        if document is None:
            raise SnapshotIncomplete("The requested document is not frozen.")
        view = document.view()
        assert view.resource is MISSING
        return view
