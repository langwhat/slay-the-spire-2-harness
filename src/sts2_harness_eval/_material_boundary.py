"""Keep experiment material refusals outside normal gameplay error recovery."""

from collections.abc import Callable

from sts2_harness.client import models as native
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    SkillRead,
    SkillSummary,
    SubjectId,
)
from sts2_harness.material_text import MaterialText
from sts2_harness.prompts import PromptReader
from sts2_harness_eval._replay import ReplayRejected
from sts2_harness_eval.variants import VariantError


class RejectingPrompts:
    def __init__(self, source: Callable[[], PromptReader]) -> None:
        self._source = source

    def read(self, name: str) -> MaterialText:
        try:
            return self._source().read(name)
        except VariantError as error:
            raise ReplayRejected(error.rejection) from error


class RejectingDocuments:
    def __init__(self, source: Callable[[], Documents]) -> None:
        self._source = source

    async def list(self, role: native.DecisionOwner) -> tuple[SkillSummary, ...]:
        try:
            return await self._source().list(role)
        except VariantError as error:
            raise ReplayRejected(error.rejection) from error

    async def read(self, request: SkillRead) -> DocumentView:
        try:
            return await self._source().read(request)
        except VariantError as error:
            raise ReplayRejected(error.rejection) from error

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        try:
            return await self._source().lookup(subjects)
        except VariantError as error:
            raise ReplayRejected(error.rejection) from error
