"""Fixed role query tools return semantic values; Context owns model delivery."""

import json
from collections.abc import Awaitable, Callable
from enum import StrEnum

from pydantic import ValidationError

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, InspectResult
from sts2_harness.execution import GameReadError
from sts2_harness.knowledge import (
    DocumentView,
    KnowledgeEntry,
    KnowledgeLookup,
    SubjectId,
    SupplementProvider,
    SupplementUnavailable,
)
from sts2_harness.llm.messages import ToolCall, ToolDefinition
from sts2_harness.results import (
    Failed,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
from sts2_harness.tools.inputs import DEFINITIONS, InspectInput, KnowledgeInput, StateQuery


class ToolName(StrEnum):
    RUN_INSPECT = "run_inspect"
    RUN_KNOWLEDGE = "run_knowledge"
    COMBAT_INSPECT = "combat_inspect"
    COMBAT_KNOWLEDGE = "combat_knowledge"


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate fields are not permitted.")
        result[key] = value
    return result


def _problem(code: ReasonCode, message: str, *, failed: bool = False) -> Rejected | Failed:
    reason = Reason(code=code, message=message)
    if code is ReasonCode.INVALID_ARGUMENTS:
        suggestion = (
            "Check the tool schema, current state references, or catalog values as applicable, "
            "then correct the query before retrying."
        )
    elif code is ReasonCode.WRONG_ROLE:
        suggestion = "Use the current state; this tool or menu is handled by the other role."
    else:
        suggestion = (
            "Continue from the current state if available, or retry the same query later "
            "if the detail is still needed."
        )
    suggestions = (Suggestion(message=suggestion),)
    if failed:
        return Failed(reason=reason, suggested_actions=suggestions)
    return Rejected(reason=reason, suggested_actions=suggestions)


class ToolCatalog:
    def __init__(
        self,
        role: native.DecisionOwner,
        *,
        observe: Callable[[], Awaitable[native.Observation]],
        inspect: Callable[[native.InspectQuery], Awaitable[InspectResult]],
        catalog: Callable[[native.CatalogRequest], Awaitable[CatalogResult]],
        supplements: SupplementProvider | None = None,
    ) -> None:
        self._role = role
        self._observe = observe
        self._inspect = inspect
        self._catalog = catalog
        self._supplements = supplements
        self.definitions = tuple(
            ToolDefinition(
                item.name,
                item.description,
                InspectInput
                if ToolName(item.name) in (ToolName.RUN_INSPECT, ToolName.COMBAT_INSPECT)
                else KnowledgeInput,
            )
            for item in DEFINITIONS
            if item.name.startswith(role.value + "_")
        )
        self._names = frozenset(item.name for item in self.definitions)

    async def _current(self) -> native.Observation | None:
        try:
            return await self._observe()
        except (ClientUnavailable, GameReadError):
            return None

    async def dispatch(self, call: ToolCall) -> QueryToolResult:
        if call.name not in self._names:
            return QueryToolResult(
                _problem(ReasonCode.WRONG_ROLE, "This tool is not available to the current role."),
                await self._current(),
            )
        try:
            json.loads(call.arguments, object_pairs_hook=_unique_fields)
            query = (
                InspectInput.model_validate_json(call.arguments, strict=True).root
                if ToolName(call.name) in (ToolName.RUN_INSPECT, ToolName.COMBAT_INSPECT)
                else KnowledgeInput.model_validate_json(call.arguments, strict=True).root
            )
        except (ValueError, ValidationError):
            return QueryToolResult(
                _problem(ReasonCode.INVALID_ARGUMENTS, "The query does not match the tool schema."),
                await self._current(),
            )
        try:
            if isinstance(query, (native.CatalogQuery, native.SearchQuery, native.LookupQuery)):
                return await self._read_knowledge(query)
            return await self._read_details(query)
        except (ClientUnavailable, GameReadError) as error:
            return QueryToolResult(_problem(ReasonCode.QUERY_FAILED, str(error), failed=True), None)

    async def _read_knowledge(self, query: native.CatalogRequest) -> QueryToolResult:
        try:
            result = await self._catalog(query)
        except ClientUnavailable as error:
            return QueryToolResult(
                _problem(ReasonCode.QUERY_FAILED, str(error), failed=True), await self._current()
            )
        observation = await self._current()
        if isinstance(result, native.CatalogCompleted):
            data = result.data
            if isinstance(data, native.LookupData):
                try:
                    data = await self._supplement(data)
                except (SupplementUnavailable, OSError) as error:
                    return QueryToolResult(
                        _problem(
                            ReasonCode.CONTENT_UNAVAILABLE,
                            str(error)
                            if isinstance(error, SupplementUnavailable)
                            else "The enabled supplementary source could not be read.",
                            failed=True,
                        ),
                        observation,
                    )
            return QueryToolResult(ReadCompleted(), observation, data)
        if isinstance(result, (native.CatalogRejected, native.InvalidRequestReply)):
            return QueryToolResult(
                _problem(ReasonCode.INVALID_ARGUMENTS, result.error.message), observation
            )
        return QueryToolResult(
            _problem(ReasonCode.QUERY_FAILED, result.error.message, failed=True), observation
        )

    async def _supplement(self, data: native.LookupData) -> KnowledgeLookup:
        subjects = tuple(
            SubjectId(category=entry.category, definition_id=entry.definition_id)
            for entry in data.entries
        )
        documents = (
            await self._supplements.lookup(subjects)
            if self._supplements is not None and subjects
            else ()
        )
        entries: list[KnowledgeEntry] = []
        for definition, subject in zip(data.entries, subjects, strict=True):
            views: dict[str, DocumentView] = {}
            for document in documents:
                if subject in document.subjects:
                    views[document.id] = document.view()
            entries.append(KnowledgeEntry(definition=definition, supplements=tuple(views.values())))
        return KnowledgeLookup(entries=tuple(entries), missing_ids=data.missing_ids)

    async def _read_details(self, query: StateQuery | native.InspectQuery) -> QueryToolResult:
        if isinstance(query, StateQuery):
            return QueryToolResult(ReadCompleted(), await self._observe())
        try:
            result = await self._inspect(query)
        except ClientUnavailable as error:
            return QueryToolResult(
                _problem(ReasonCode.QUERY_FAILED, str(error), failed=True), await self._current()
            )
        observation = (
            result.observation
            if isinstance(
                result, (native.InspectCompleted, native.InspectRejected, native.InspectFailed)
            )
            else None
        )
        if observation is None:
            observation = await self._current()
        if (
            query.area
            in (native.InspectArea.OPTIONS, native.InspectArea.REWARDS, native.InspectArea.SHOP)
            and observation is not None
            and observation.location.owner is not self._role
        ):
            return QueryToolResult(
                _problem(ReasonCode.WRONG_ROLE, "The requested menu belongs to the other role."),
                observation,
            )
        if isinstance(result, native.InspectCompleted):
            return QueryToolResult(ReadCompleted(), observation, result.data)
        message = result.error.message
        if isinstance(result, (native.InspectRejected, native.InvalidRequestReply)):
            if isinstance(result.error, native.InvalidRefs):
                message += " Invalid references: " + ", ".join(result.error.invalid_refs) + "."
            return QueryToolResult(_problem(ReasonCode.INVALID_ARGUMENTS, message), observation)
        return QueryToolResult(_problem(ReasonCode.QUERY_FAILED, message, failed=True), observation)
