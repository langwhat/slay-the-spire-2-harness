"""Fixed role query tools return semantic values; Context owns model delivery."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import MISSING, BaseModel, ValidationError

from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, InspectResult
from sts2_harness.decisions import (
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    DecisionTask,
    SelectStep,
)
from sts2_harness.deck_stats import DeckStatistics
from sts2_harness.fights import FightHistory
from sts2_harness.game import GameReadError, Presented
from sts2_harness.knowledge import (
    DocumentView,
    KnowledgeEntry,
    KnowledgeLookup,
    SkillList,
    SkillProvider,
    SkillRead,
    SubjectId,
    SupplementProvider,
    UnknownDocument,
)
from sts2_harness.llm.messages import ToolCall, ToolDefinition
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.note_values import OTHER_SOFT_LIMIT, SUMMARY_SOFT_LIMIT, NotesState
from sts2_harness.notes import Notes

if TYPE_CHECKING:
    from sts2_harness.diagnostics import Diagnostics, NoteBudgetField
from sts2_harness.results import (
    Failed,
    NotesText,
    NotesUpdated,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
from sts2_harness.tools.inputs import (
    DEFINITIONS,
    CombatActInput,
    CombatInspectInput,
    KnowledgeInput,
    MinimalRunActInput,
    MinimalRunNext,
    NotesInput,
    NotesPatch,
    NotesReadInput,
    RunAct,
    RunActInput,
    RunDeckChoice,
    RunInspectInput,
    RunMapQuery,
    RunNext,
    RunPlan,
    SkillInput,
    SkillListInput,
    StateQuery,
    tool_schema,
)


class QueryView(StrEnum):
    ROLE = "role"
    PUBLIC = "public"


class ToolName(StrEnum):
    COMBAT_ACT = "combat_act"
    RUN_ACT = "run_act"
    RUN_DECK_CHOICE = "run_deck_choice"
    RUN_PLAN = "run_plan"
    RUN_NEXT = "run_next"
    RUN_NOTES = "run_notes"
    RUN_SKILL = "run_skill"
    COMBAT_SKILL = "combat_skill"
    RUN_INSPECT = "run_inspect"
    RUN_KNOWLEDGE = "run_knowledge"
    COMBAT_INSPECT = "combat_inspect"
    COMBAT_KNOWLEDGE = "combat_knowledge"


def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
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
        present: Callable[
            [native.RouteKey, tuple[native.PresentationEdge, ...]], Awaitable[Presented]
        ]
        | None = None,
        display: Callable[[str | None], Awaitable[None]] | None = None,
        supplements: SupplementProvider | None = None,
        skills: SkillProvider | None = None,
        notes: Notes | None = None,
        query_view: QueryView = QueryView.ROLE,
        notes_write_guard: Callable[[native.Observation | None], Awaitable[QueryToolResult | None]]
        | None = None,
        minimal: bool = False,
        deck_stats: DeckStatistics | None = None,
        focused_deck: bool = False,
        fights: FightHistory | None = None,
        diagnostics: "Diagnostics | None" = None,
    ) -> None:
        if role is native.DecisionOwner.RUN and notes is None:
            raise ValueError("Run tools require the run's notes owner.")
        self._notes_owner = notes
        self.minimal = minimal
        self.deck_stats = deck_stats
        self.fights = fights
        self._diagnostics = diagnostics
        self._query_view = query_view
        self._notes_write_guard = notes_write_guard
        self.action_name = (
            ToolName.COMBAT_ACT if role is native.DecisionOwner.COMBAT else ToolName.RUN_ACT
        )
        self._role = role
        self._observe = observe
        self._inspect = inspect
        self._catalog = catalog
        self._present = present
        self._display = display
        self._supplements = supplements
        self._skills = skills
        inputs: dict[ToolName, type[BaseModel]] = {
            ToolName.RUN_ACT: MinimalRunActInput if minimal else RunActInput,
            ToolName.RUN_DECK_CHOICE: RunDeckChoice,
            ToolName.RUN_PLAN: RunPlan,
            ToolName.RUN_NEXT: MinimalRunNext if minimal else RunNext,
            ToolName.COMBAT_ACT: CombatActInput,
            ToolName.RUN_NOTES: NotesInput,
            ToolName.RUN_INSPECT: RunInspectInput,
            ToolName.COMBAT_INSPECT: CombatInspectInput,
            ToolName.RUN_SKILL: SkillInput,
            ToolName.COMBAT_SKILL: SkillInput,
            ToolName.RUN_KNOWLEDGE: KnowledgeInput,
            ToolName.COMBAT_KNOWLEDGE: KnowledgeInput,
        }
        self._base_definitions = tuple(
            ToolDefinition(
                item.name,
                MaterialText.create(
                    f"tool:{item.name}:description",
                    item.minimal_description
                    if minimal and item.minimal_description is not None
                    else item.description,
                ),
                inputs[ToolName(item.name)],
            )
            for item in DEFINITIONS
            if item.name.startswith(role.value + "_")
            and (
                ToolName(item.name) is ToolName.RUN_DECK_CHOICE
                if focused_deck
                else ToolName(item.name) is not ToolName.RUN_DECK_CHOICE
            )
            and (
                not minimal
                or ToolName(item.name)
                in {
                    ToolName.RUN_ACT,
                    ToolName.RUN_DECK_CHOICE,
                    ToolName.RUN_PLAN,
                    ToolName.RUN_NEXT,
                    ToolName.COMBAT_ACT,
                }
            )
        )
        self._definitions_by_language: dict[str | None, tuple[ToolDefinition, ...]] = {}
        self.definitions = self.definitions_for(None)
        self._names = frozenset(item.name for item in self.definitions)

    def definitions_for(self, game_language: str | None) -> tuple[ToolDefinition, ...]:
        cached = self._definitions_by_language.get(game_language)
        if cached is not None:
            return cached
        resolved = tuple(
            ToolDefinition(
                item.name,
                item.description,
                item.parameters,
                json.dumps(
                    tool_schema(item.name, minimal=self.minimal, game_language=game_language),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            for item in self._base_definitions
        )
        self._definitions_by_language[game_language] = resolved
        return resolved

    @property
    def notes(self) -> Notes | None:
        return self._notes_owner

    async def display_reason(self, reason: str | None) -> None:
        if self._display is not None:
            await self._display(reason)

    async def dispatch_action(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation | None = None,
    ) -> ActionProposal | QueryToolResult:
        if call.name != self.action_name or call.name not in self._names:
            return QueryToolResult(
                _problem(ReasonCode.WRONG_ROLE, "This action tool is not available."), observation
            )
        if not isinstance(task, ActTask):
            return QueryToolResult(
                Rejected(
                    reason=Reason(
                        code=ReasonCode.WRONG_ROLE,
                        message="Complete this maintenance task without game actions.",
                    ),
                    suggested_actions=(
                        Suggestion(
                            message=(
                                "Finish this task with a concise text response; "
                                "it does not take game actions."
                            )
                        ),
                    ),
                ),
                observation,
            )
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = (
                CombatActInput.model_validate_json(call.arguments, strict=True).root
                if self._role is native.DecisionOwner.COMBAT
                else RunActInput.model_validate_json(call.arguments, strict=True).root
            )
            if self.minimal and isinstance(query, RunAct) and query.notes is not MISSING:
                raise ValueError("Strategy notes are unavailable in minimal mode.")
        except (ValueError, ValidationError):
            return QueryToolResult(
                _problem(
                    ReasonCode.INVALID_ARGUMENTS, "The action does not match the tool schema."
                ),
                observation,
            )
        if isinstance(query, RunAct) and query.notes is not MISSING:
            current = await self._current()
            if current is not None and current.location != native.Location.model_validate(
                task.expected_route.model_dump()
            ):
                return QueryToolResult(
                    _problem(ReasonCode.ROLE_CHANGED, "The action route changed."), current
                )
            written = await self.write_notes(query.notes, current)
            if not isinstance(written.outcome, ReadCompleted):
                return written
        steps: list[ActionStep | SelectStep] = []
        for step in query.steps:
            if isinstance(step, native.Select):
                steps.append(SelectStep(step.refs))
            elif isinstance(step, native.PlayCard):
                steps.append(
                    ActionStep(
                        ActionKind.PLAY_CARD,
                        step.card,
                        None if step.target is MISSING else step.target,
                    )
                )
            elif isinstance(step, native.UsePotion):
                steps.append(
                    ActionStep(
                        ActionKind.USE_POTION,
                        step.potion,
                        None if step.target is MISSING else step.target,
                    )
                )
            elif isinstance(step, native.DiscardPotion):
                steps.append(ActionStep(ActionKind.DISCARD_POTION, step.potion))
            elif isinstance(step, native.Interact):
                steps.append(ActionStep(ActionKind.INTERACT, step.action))
            else:
                steps.append(ActionStep(ActionKind.END_TURN))
        return ActionProposal(tuple(steps), None if query.reason is MISSING else query.reason)

    async def _notes(self, call: ToolCall, *, handoff: bool) -> QueryToolResult:
        observation = await self._current()
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = NotesInput.model_validate_json(call.arguments, strict=True).root
        except (ValueError, ValidationError):
            return QueryToolResult(
                _problem(ReasonCode.INVALID_ARGUMENTS, "The query does not match the tool schema."),
                observation,
            )
        assert self.notes is not None
        if isinstance(query, NotesReadInput):
            return QueryToolResult(ReadCompleted(), observation, NotesText(notes=self.notes.read()))
        if query.summary is not MISSING and not handoff:
            return QueryToolResult(
                _problem(ReasonCode.WRONG_ROLE, "A notes summary is only allowed during handoff."),
                observation,
            )
        result = await self.write_notes(query, observation)
        if isinstance(result.data, NotesUpdated) and query.summary is not MISSING:
            if len(query.summary) > SUMMARY_SOFT_LIMIT:
                from sts2_harness.diagnostics import NoteBudgetField

                self._emit_note_budget(
                    NoteBudgetField.SUMMARY, len(query.summary), SUMMARY_SOFT_LIMIT
                )
            return replace(result, data=NotesUpdated(summary=query.summary))
        return result

    def _emit_note_budget(self, field: "NoteBudgetField", length: int, soft_limit: int) -> None:
        if self._diagnostics is None:
            return
        from sts2_harness.diagnostics import NoteBudgetExceeded

        self._diagnostics.emit(
            NoteBudgetExceeded(
                role=self._role,
                field=field,
                length=length,
                soft_limit=soft_limit,
            )
        )

    async def write_notes(
        self, query: NotesPatch, observation: native.Observation | None
    ) -> QueryToolResult:
        assert self.notes is not None
        if observation is None or observation.state.control is not native.StateControl.READY:
            return QueryToolResult(
                _problem(ReasonCode.BUSY, "Current writable run state is unavailable."), observation
            )
        if self._notes_write_guard is not None:
            refused = await self._notes_write_guard(observation)
            if refused is not None:
                return refused
        note_fields = {"core", "plan", "other"}
        fields = NotesState.model_validate_json(query.model_dump_json(include=note_fields))
        self.notes.write(fields, query.model_fields_set & note_fields)
        if query.other is not MISSING and len(query.other) > OTHER_SOFT_LIMIT:
            from sts2_harness.diagnostics import NoteBudgetField

            self._emit_note_budget(NoteBudgetField.OTHER, len(query.other), OTHER_SOFT_LIMIT)
        return QueryToolResult(ReadCompleted(), observation, NotesUpdated())

    async def _read_skill(self, call: ToolCall) -> QueryToolResult:
        observation = await self._current()
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = SkillInput.model_validate_json(call.arguments, strict=True).root
        except (ValueError, ValidationError):
            return QueryToolResult(
                _problem(ReasonCode.INVALID_ARGUMENTS, "The query does not match the tool schema."),
                observation,
            )
        try:
            if isinstance(query, SkillListInput):
                summaries = await self._skills.list(self._role) if self._skills is not None else ()
                text = query.text.strip().casefold() if query.text is not MISSING else ""
                matches = sorted(
                    (
                        item
                        for item in summaries
                        if any(
                            text in value.casefold()
                            for value in (item.id, item.name.body, item.description.body)
                        )
                    ),
                    key=lambda item: item.id,
                )
                offset = query.offset if query.offset is not MISSING else 0
                limit = query.limit if query.limit is not MISSING else 20
                end = offset + limit
                return QueryToolResult(
                    ReadCompleted(),
                    observation,
                    SkillList(
                        skills=tuple(matches[offset:end]),
                        total=len(matches),
                        next_offset=end if end < len(matches) else None,
                    ),
                )
            if self._skills is None:
                raise UnknownDocument("The requested document is not configured.")
            resource = query.resource if query.resource is not MISSING else None
            document = await self._skills.read(SkillRead(query.id, resource))
            return QueryToolResult(ReadCompleted(), observation, document)
        except UnknownDocument as error:
            return QueryToolResult(_problem(ReasonCode.INVALID_ARGUMENTS, str(error)), observation)
        except (MaterialUnavailable, OSError) as error:
            return QueryToolResult(
                _problem(
                    ReasonCode.CONTENT_UNAVAILABLE,
                    str(error)
                    if isinstance(error, MaterialUnavailable)
                    else "The enabled document source could not be read.",
                    failed=True,
                ),
                observation,
            )

    async def _current(self) -> native.Observation | None:
        try:
            return await self._observe()
        except (ClientUnavailable, GameReadError):
            return None

    async def current(self) -> native.Observation | None:
        return await self._current()

    async def capture_history(self, observation: native.Observation) -> None:
        if self.deck_stats is None:
            return
        result = await self._inspect(native.MapQuery(area=native.InspectArea.COMBAT_HISTORY))
        if (
            isinstance(result, native.InspectCompleted)
            and isinstance(result.data, native.HistoryData)
            and result.observation.location == observation.location
        ):
            self.deck_stats.capture(result.observation, result.data)

    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> Presented | None:
        return await self._present(route, edges) if self._present is not None else None

    async def dispatch(self, call: ToolCall, *, handoff: bool = False) -> QueryToolResult:
        if call.name not in self._names or call.name == self.action_name:
            return QueryToolResult(
                _problem(ReasonCode.WRONG_ROLE, "This tool is not available to the current role."),
                await self._current(),
            )
        if call.name == ToolName.RUN_NOTES:
            return await self._notes(call, handoff=handoff)
        if ToolName(call.name) in (ToolName.RUN_SKILL, ToolName.COMBAT_SKILL):
            return await self._read_skill(call)
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = (
                RunInspectInput.model_validate_json(call.arguments, strict=True).root
                if call.name == ToolName.RUN_INSPECT
                else CombatInspectInput.model_validate_json(call.arguments, strict=True).root
                if call.name == ToolName.COMBAT_INSPECT
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
                except (MaterialUnavailable, OSError) as error:
                    return QueryToolResult(
                        _problem(
                            ReasonCode.CONTENT_UNAVAILABLE,
                            str(error)
                            if isinstance(error, MaterialUnavailable)
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

    async def _read_details(
        self, query: StateQuery | native.MapQuery | RunMapQuery
    ) -> QueryToolResult:
        if isinstance(query, StateQuery):
            return QueryToolResult(ReadCompleted(), await self._observe())
        native_query = native.MapQuery(area=query.area)
        try:
            result = await self._inspect(native_query)
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
            self._query_view is QueryView.ROLE
            and native_query.area
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
