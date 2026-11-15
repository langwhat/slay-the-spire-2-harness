"""Serve supplementary documents from a fixed per-evaluation snapshot."""

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.knowledge import (
    CandidateLink,
    Document,
    DocumentView,
    SkillRead,
    SkillSummary,
    SubjectId,
)
from sts2_harness_eval.frozen_catalog import SnapshotIncomplete


class FrozenSkillList(native.WireModel):
    role: native.DecisionOwner
    skills: tuple[SkillSummary, ...]


class FrozenLinkRead(native.WireModel):
    candidates: tuple[str, ...]
    owned: tuple[str, ...]
    result: dict[str, tuple[CandidateLink, ...]]


class FrozenLinks(native.WireModel):
    reads: tuple[FrozenLinkRead, ...]

    async def links(
        self, candidate_ids: tuple[str, ...], owned_ids: tuple[str, ...]
    ) -> dict[str, tuple[CandidateLink, ...]]:
        read = next(
            (
                item
                for item in self.reads
                if item.candidates == candidate_ids and item.owned == owned_ids
            ),
            None,
        )
        if read is None:
            raise SnapshotIncomplete("The snapshot does not cover these candidate links.")
        return read.result.copy()


class FrozenDocuments(native.WireModel):
    complete_subjects: bool
    covered_subjects: tuple[SubjectId, ...]
    documents: tuple[Document, ...]
    resources: tuple[DocumentView, ...] = ()
    skill_lists: tuple[FrozenSkillList, ...] = ()

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
