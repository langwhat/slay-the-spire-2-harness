"""Rebuild labeled decisions through the production Run and Context owners."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from pydantic import MISSING, JsonValue, TypeAdapter, ValidationError

from sts2_harness.capacity import CapacityPolicy, Measurement
from sts2_harness.client import models as native
from sts2_harness.config import ConfigurationError, load_configuration
from sts2_harness.context import Context
from sts2_harness.context.map_projection import MapUnavailable, replace_stretches, restore_stretches
from sts2_harness.decisions import (
    ActionProposal,
    ActTask,
    CombatHandoff,
    DecisionTask,
    HandoffTask,
    LocalProgress,
    SelectStep,
)
from sts2_harness.deck import deck_context, deck_observation
from sts2_harness.deck_role import DeckAgent
from sts2_harness.deck_stats import DeckStatistics
from sts2_harness.entry_materials import EntryMaterials, MaterialContext, entry_materials
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.game import (
    Admitted,
    Catalog,
    Cataloged,
    GameReply,
    Inspect,
    Inspected,
    Observed,
    Presented,
    Resumed,
    Settlement,
)
from sts2_harness.knowledge import (
    CandidateLinks,
    Documents,
    DocumentView,
    GuidanceContent,
    MaterialRole,
)
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    AssistantMessage,
    CapacityRecovery,
    FailureKind,
    ModelClient,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    TextMessage,
    TextRole,
    ToolCall,
    ToolDefinition,
)
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.note_values import NotesState
from sts2_harness.notes import Notes
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import (
    ActionToolResult,
    NotesText,
    NotesUpdated,
    PlanAccepted,
    PlanRejected,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
)
from sts2_harness.route_plan import Edge, RoutePlan, reconcile_plan
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import RunPlan
from sts2_harness.user_input import UserInputQueue
from sts2_harness_eval.decision_cases import (
    AssistantEvent,
    AutomaticEvent,
    DecisionCase,
    DecisionSample,
    DecisionVariant,
    DecisionVerdict,
    HarnessState,
    RecordedDecision,
    RequestSize,
    ResultEvent,
    TaskEvent,
    score,
)
from sts2_harness_eval.decision_extract import ExtractionError, choices_for

_JSON = TypeAdapter(dict[str, JsonValue])


class CaseGame:
    """An immutable native-read fixture; writes cannot reach any game process."""

    def __init__(self, case: DecisionCase, observation: native.Observation) -> None:
        self.case = case
        self.observation = observation

    async def admit(self) -> GameReply[Admitted]:
        return GameReply(outcome=Admitted(allowed=True), observations=(self.observation,))

    async def observe(self) -> GameReply[Observed]:
        return GameReply(
            outcome=Observed(observation=self.observation), observations=(self.observation,)
        )

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        request = Inspect(query=query)
        for read in reversed(self.case.native_reads):
            if read.request == request and isinstance(read.result, native.InspectCompleted):
                if read.result.observation != self.observation:
                    continue
                result = read.result
                return GameReply(outcome=Inspected(result=result), observations=(self.observation,))
        raise ExtractionError("The case has no raw native reply for this inspection.")

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        query = Catalog(request=request)
        for read in reversed(self.case.native_reads):
            if read.request == query and isinstance(read.result, native.CatalogCompleted):
                return GameReply(outcome=Cataloged(result=read.result), observations=())
        raise ExtractionError("The case has no raw native reply for this catalog request.")

    async def present(
        self, route: native.RouteKey, edges: tuple[native.PresentationEdge, ...]
    ) -> GameReply[Presented]:
        raise ExtractionError("Decision replay cannot present a route in a game.")

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        raise ExtractionError("Decision replay cannot submit a game action.")

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        raise ExtractionError("Decision replay cannot control a game session.")


class CaseStatistics(DeckStatistics):
    def __init__(self, state: HarnessState, observation: native.Observation) -> None:
        super().__init__()
        self.observed_plays = state.observed_plays
        for _ in range(state.added):
            self.record_choice(observation, taken=True)
        for _ in range(state.skipped):
            self.record_choice(observation, taken=False)

    def view(self, observation: native.Observation) -> tuple[dict[str, JsonValue], dict[str, int]]:
        stats, _ = super().view(observation)
        return stats, self.observed_plays


class _ChoiceMade(Exception):
    def __init__(self, call: ToolCall, choices: tuple[str, ...]) -> None:
        self.call = call
        self.choices = choices


class DecisionDeviation(ExtractionError):
    """The sample proposed an action outside the labeled native decision."""


class DecisionRun(DeckAgent):
    """Restore harness-owned history and stop at its first proposed native decision."""

    def _reward_view(self, observation: native.Observation) -> native.Observation:
        return observation

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | QueryToolResult | LocalProgress:
        if self.variant is DecisionVariant.DECK and call.name != ToolName.RUN_DECK_CHOICE:
            raise DecisionDeviation("The deck role proposed a tool outside its tool set.")
        if call.name not in (
            ToolName.RUN_ACT,
            ToolName.RUN_DECK_CHOICE,
            ToolName.RUN_NEXT,
            ToolName.RUN_PLAN,
        ):
            return await super()._dispatch(call, task, observation)
        if call.name != self.case.decision_tool:
            raise DecisionDeviation("The model proposed a different game action.")
        result = (
            await super()._dispatch(call, task, observation)
            if self.variant is DecisionVariant.DECK
            else await RunAgent._dispatch(self, call, task, observation)
        )
        if isinstance(result, QueryToolResult) and isinstance(result.outcome, Rejected):
            raise DecisionDeviation(result.outcome.reason.message)
        if call.name == ToolName.RUN_DECK_CHOICE:
            arguments = _JSON.validate_json(call.arguments)
            if arguments.get("offer") != self.case.decision_offer:
                raise DecisionDeviation("The model chose a different native card offer.")
        if call.name == ToolName.RUN_ACT:
            if not isinstance(result, ActionProposal) or len(result.steps) != 1:
                raise DecisionDeviation("The labeled selection requires one native select step.")
            step = result.steps[0]
            selection = observation.state.selection
            if not isinstance(step, SelectStep) or selection is None:
                raise DecisionDeviation("The model did not select from the labeled native menu.")
            if (
                len(set(step.refs)) != len(step.refs)
                or not set(step.refs).issubset(selection.candidates)
                or not selection.min <= len(step.refs) <= selection.max
            ):
                raise DecisionDeviation("The selection violates the current native menu.")
        raise _ChoiceMade(call, choices_for(call, observation))

    case: DecisionCase
    variant: DecisionVariant = DecisionVariant.RUN
    _resuming: bool = False

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        if self._resuming:
            self._resuming = False
            return
        super()._append_task(context, task, observation)

    async def prepared(self) -> PreparedRequest:
        assert self._context is not None
        prepared, _ = await self.compact(self._context)
        return prepared

    def reward_observation(
        self, observation: native.Observation, declined: tuple[str, ...]
    ) -> native.Observation:
        state = observation.state
        if not isinstance(state, native.RunState) or not declined:
            return observation
        excluded = {ref for offer in state.offers if offer.ref in declined for ref in offer.actions}
        return observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": tuple(
                            offer for offer in state.offers if offer.ref not in declined
                        ),
                        "actions": tuple(
                            action for action in state.actions if action.ref not in excluded
                        ),
                    }
                )
            }
        )

    def _initial_context(
        self, documents: tuple[DocumentView, ...], observation: native.Observation
    ) -> Context:
        if self.variant is DecisionVariant.RUN:
            return RunAgent._initial_context(self, documents, observation)
        return deck_context(self._prompts, documents, observation, minimal=self._tools.minimal)

    async def restore(
        self,
        case: DecisionCase,
        game: CaseGame,
        execution: Execution,
        documents: Documents | None,
    ) -> tuple[DecisionTask, native.Observation]:
        self.case = case
        first = next(event for event in case.events if isinstance(event, TaskEvent))
        self._notes.write(_notes_state(first.state), set(NotesState.model_fields))
        opening = self.reward_observation(first.observation, first.declined_offers)
        selected = await self._materials(opening) if self._materials is not None else ()
        context = self._initial_context(selected, opening)
        self._context = context
        self._llm = self._client()
        self._context_location = first.observation.location
        calls: dict[str, ToolCall] = {}
        pending_routes: set[str] = set()
        current = first.observation
        anchored = False
        task: DecisionTask = _task(first)
        for event in case.events:
            if isinstance(event, TaskEvent):
                game.observation = event.observation
                current = self.reward_observation(event.observation, event.declined_offers)
                anchored = True
                self._notes.write(_notes_state(event.state), set(NotesState.model_fields))
                self._tools.deck_stats = CaseStatistics(event.state, current)
                if self._tools.fights is not None:
                    self._tools.fights.recent = event.state.fights
                route = event.state.route
                self._plan = (
                    RoutePlan(route.position, "", tuple(Edge(*edge) for edge in route.edges))
                    if route is not None
                    else None
                )
                context.set_route_plan(self._plan)
                plan = self._plan_value(current)
                if route is not None and plan is None:
                    raise ExtractionError("The recorded route cannot be represented as stretches.")
                context.begin_group(current, self._notes.read(), plan)
                if self._materials is not None:
                    context.append_materials(await self._materials(current))
                await self._enrich(context, current)
                task = _task(event)
                self._append_task(context, task, current)
            elif isinstance(event, AssistantEvent):
                reply = AssistantMessage(event.reply.native_reply)
                context.append_reply(reply)
                calls.update((call.id, call) for call in reply.tool_calls)
                pending_routes.update(
                    call.id for call in reply.tool_calls if call.name == ToolName.RUN_PLAN
                )
            elif isinstance(event, ResultEvent):
                call = calls[event.call_id]
                if call.name == ToolName.RUN_PLAN:
                    route = _recorded_route(call, event, current if anchored else None)
                    pending_routes.discard(call.id)
                    if route is not None:
                        self._plan = route
                        context.set_route_plan(route)
                elif event.plan is not None:
                    raise ExtractionError("A recorded plan result belongs to a different tool.")
                recorded_observation = event.observation
                if recorded_observation is not None:
                    projected_observation = self.reward_observation(
                        recorded_observation, event.declined_offers
                    )
                    event = event.model_copy(update={"observation": projected_observation})
                    current = projected_observation
                    anchored = not isinstance(task, ActTask) or (
                        current.location
                        == native.Location.model_validate(task.expected_route.model_dump())
                    )
                    self._plan = reconcile_plan(self._plan, current.state.progress.position).plan
                    context.set_route_plan(self._plan)
                elif event.action and not isinstance(event.outcome, Rejected):
                    anchored = False
                if event.action:
                    outcome = TypeAdapter(ActionToolResult).validate_python(
                        {"outcome": event.outcome, "observation": event.observation}
                    )
                    context.append_tool_result(call, outcome, defer_observation=True)
                    if context.has_active_group:
                        context.finish_group()
                else:
                    result = await _query_result(event, call, self._notes, execution, documents)
                    context.append_tool_result(call, result, defer_observation=True)
                if recorded_observation is not None:
                    game.observation = recorded_observation
            elif isinstance(event, AutomaticEvent):
                observation = (
                    self.reward_observation(event.observation, event.declined_offers)
                    if event.observation is not None
                    else None
                )
                if observation is not None:
                    current = observation
                    assert event.observation is not None
                    game.observation = event.observation
                    anchored = not isinstance(task, ActTask) or (
                        current.location
                        == native.Location.model_validate(task.expected_route.model_dump())
                    )
                    self._plan = reconcile_plan(self._plan, current.state.progress.position).plan
                    context.set_route_plan(self._plan)
                else:
                    anchored = False
                context.append_automatic(
                    event.progress,
                    ActionToolResult(event.outcome, observation),
                    invalidated=event.invalidated,
                )
                if context.has_active_group:
                    context.finish_group()
            else:
                context.append_correction(event.message)
        if pending_routes:
            raise ExtractionError("A recorded route tool call has no result.")
        if not anchored:
            raise ExtractionError("The recorded decision has no current native anchor.")
        self._resuming = True
        return task, current


def _recorded_route(
    call: ToolCall, event: ResultEvent, anchor: native.Observation | None
) -> RoutePlan | None:
    if event.action:
        raise ExtractionError("The recorded route result is not a query result.")
    try:
        raw_arguments = json.loads(call.arguments)
    except ValueError as error:
        raise ExtractionError(
            "The recorded route interaction uses an incompatible schema."
        ) from error
    if isinstance(raw_arguments, dict) and "edges" in raw_arguments:
        raise ExtractionError("The recorded route interaction uses an incompatible schema.")
    try:
        json.loads(call.arguments, object_pairs_hook=unique_fields)
        arguments = RunPlan.model_validate_json(call.arguments, strict=True)
    except (ValueError, ValidationError) as error:
        if isinstance(event.outcome, Rejected) and (
            event.outcome.reason.code is ReasonCode.INVALID_ARGUMENTS
            and (event.plan is None or isinstance(event.plan.plan, PlanRejected))
            or event.outcome.reason.code is ReasonCode.BATCH_ABORTED
            and event.plan is None
        ):
            return None
        raise ExtractionError(
            "The recorded route interaction uses an incompatible schema."
        ) from error
    if event.plan is None:
        if isinstance(event.outcome, Rejected) and event.outcome.reason.code in (
            ReasonCode.INVALID_ARGUMENTS,
            ReasonCode.BATCH_ABORTED,
        ):
            return None
        raise ExtractionError("The recorded route result has no plan status.")
    accepted = isinstance(event.plan.plan, PlanAccepted)
    if isinstance(event.plan.plan, PlanAccepted):
        if not isinstance(event.outcome, ReadCompleted) or (
            event.plan.plan.next != arguments.stretches[0]
        ):
            raise ExtractionError("The recorded route acceptance is inconsistent.")
    else:
        if not isinstance(event.outcome, Rejected):
            raise ExtractionError("The recorded route rejection cannot use the current schema.")
        if event.outcome.reason.code in (ReasonCode.QUERY_FAILED, ReasonCode.ROLE_CHANGED):
            return None
    if anchor is None or not isinstance(anchor.state, native.RunState) or anchor.map is MISSING:
        raise ExtractionError("The recorded route has no current native anchor.")
    if anchor.location.owner is not native.DecisionOwner.RUN:
        raise ExtractionError("The recorded route acceptance is inconsistent.")
    after = event.observation
    if after is not None and (
        after.location != anchor.location
        or after.map != anchor.map
        or after.state.progress.position != anchor.state.progress.position
    ):
        raise ExtractionError("The recorded route has conflicting native anchors.")
    try:
        update = (
            restore_stretches(anchor.map, anchor.state.progress.position, arguments.stretches)
            if accepted and anchor.map.legal_destinations is None
            else replace_stretches(
                None,
                anchor.map,
                anchor.state.progress.position,
                arguments.reason,
                arguments.stretches,
            )
        )
    except MapUnavailable as error:
        raise ExtractionError("The recorded route has an incomplete native map.") from error
    if accepted:
        if update.plan is None:
            raise ExtractionError("The recorded route is inconsistent with its native anchor.")
        return RoutePlan(update.plan.position, arguments.reason, update.plan.edges)
    assert isinstance(event.outcome, Rejected)
    if update.rejection is None or update.rejection.value != event.outcome.reason.code.value:
        raise ExtractionError("The recorded route rejection conflicts with its native anchor.")
    return None


def _notes_state(state: HarnessState) -> NotesState:
    try:
        return NotesState.model_validate_json(json.dumps(state.notes, ensure_ascii=False))
    except ValidationError:
        raise ExtractionError(
            "The historical notes shape requires re-extraction or an explicit mapping."
        ) from None


def _task(event: TaskEvent) -> DecisionTask:
    handoff = (
        CombatHandoff(event.handoff.report, event.handoff.review)
        if event.handoff is not None
        else None
    )
    previous = (
        CombatHandoff(event.previous_battle.report, event.previous_battle.review)
        if event.previous_battle is not None
        else None
    )
    if event.handoff_only:
        assert handoff is not None
        return HandoffTask(handoff)
    return ActTask(
        native.RouteKey.model_validate(event.observation.location.model_dump()), handoff, previous
    )


async def _query_result(
    event: ResultEvent,
    call: ToolCall,
    notes: Notes,
    execution: Execution,
    documents: Documents | None,
) -> QueryToolResult:
    outcome = TypeAdapter(QueryToolResult).validate_python(
        {"outcome": event.outcome, "observation": event.observation}
    )
    if event.notes_read:
        return QueryToolResult(outcome.outcome, event.observation, NotesText(notes=notes.read()))
    if event.notes_updated:
        return QueryToolResult(outcome.outcome, event.observation, NotesUpdated())
    if event.plan is not None:
        return QueryToolResult(outcome.outcome, event.observation, event.plan)
    if (
        event.document is not None
        or event.knowledge_ids is not None
        or event.skill_listing is not None
    ):
        tools = ToolCatalog(
            native.DecisionOwner.RUN,
            observe=execution.observe,
            inspect=execution.inspect,
            catalog=execution.catalog,
            notes=notes,
            supplements=documents,
            skills=documents,
        )
        result = await tools.dispatch(call)
        return QueryToolResult(outcome.outcome, event.observation, result.data)
    return QueryToolResult(
        outcome.outcome,
        event.observation,
        event.native_data if event.native_data is not None else MISSING,
    )


@dataclass(frozen=True, slots=True)
class RebuiltDecision:
    agent: DecisionRun
    task: DecisionTask
    observation: native.Observation
    prepared: PreparedRequest


def _deck_case(case: DecisionCase) -> DecisionCase:
    event = next(event for event in reversed(case.events) if isinstance(event, TaskEvent))
    observation = event.observation
    state = observation.state
    if not isinstance(state, native.RunState):
        raise ExtractionError("The deck role requires a native Run reward.")
    offer = next((offer for offer in state.offers if offer.ref == case.decision_offer), None)
    if offer is None or offer.kind is not native.OfferKind.CARD:
        raise ExtractionError("The source does not identify one native card offer.")
    filtered = deck_observation(observation, offer)
    previous = event.previous_battle or event.handoff
    if previous is None:
        previous = next(
            (
                history.previous_battle or history.handoff
                for history in reversed(case.events)
                if isinstance(history, TaskEvent)
                and (history.previous_battle is not None or history.handoff is not None)
            ),
            None,
        )
    return case.model_copy(
        update={
            "events": (
                event.model_copy(
                    update={
                        "observation": filtered,
                        "previous_battle": previous,
                        "handoff": None,
                        "handoff_only": False,
                        "state": event.state.model_copy(update={"route": None}),
                    }
                ),
            )
        }
    )


async def rebuild_case(
    case: DecisionCase,
    client: ModelClient,
    *,
    materials: EntryMaterials | None = None,
    prompts: PromptReader | None = None,
    variant: DecisionVariant = DecisionVariant.RUN,
) -> RebuiltDecision:
    source_observation = next(
        event.observation for event in reversed(case.events) if isinstance(event, TaskEvent)
    )
    if variant is DecisionVariant.DECK:
        case = _deck_case(case)
    first = next(event for event in case.events if isinstance(event, TaskEvent))
    assert first.observation.location.run_id is not None
    game = CaseGame(case, first.observation)
    recovery = ToolRecoveryBudget()
    execution = Execution(
        game,
        game_instance_id=first.observation.location.game_instance_id,
        run_id=first.observation.location.run_id,
        recovery=recovery,
    )
    material_context = MaterialContext(first.observation.run_rules.ascension, case.minimal)
    documents = materials.documents(material_context) if materials is not None else None
    links: CandidateLinks | None = (
        materials.candidate_links(material_context)
        if materials is not None and materials.candidate_links is not None
        else None
    )
    notes = Notes(_notes_state(first.state))
    tools = ToolCatalog(
        native.DecisionOwner.RUN,
        observe=execution.observe,
        inspect=execution.inspect,
        catalog=execution.catalog,
        notes=notes,
        supplements=documents,
        skills=documents,
        minimal=case.minimal,
        focused_deck=variant is DecisionVariant.DECK,
        fights=execution.fights,
    )

    async def selected(observation: native.Observation) -> tuple[DocumentView, ...]:
        if documents is None or not case.entry_documents_enabled:
            return ()
        if (
            variant is DecisionVariant.DECK
            and observation.map is MISSING
            and source_observation.map is not MISSING
            and observation.stamp == source_observation.stamp
            and observation.location == source_observation.location
        ):
            observation = observation.model_copy(update={"map": source_observation.map})
        return await entry_materials(
            execution,
            documents,
            MaterialRole.DECK if variant is DecisionVariant.DECK else MaterialRole.RUN,
            observation,
            case.configured_guidance,
        )

    definitions = tools.definitions
    agent = DecisionRun(
        lambda: client,
        tools,
        recovery,
        prompts=prompts if prompts is not None else PackagedPrompts(),
        materials=selected,
        candidate_links=links if case.candidate_links_enabled else None,
        user_input=UserInputQueue() if case.viewer else None,
    )
    tools.definitions = definitions
    agent.variant = variant
    task, observation = await agent.restore(case, game, execution, documents)
    prepared = await agent.prepared()
    return RebuiltDecision(agent, task, observation, prepared)


def request_size(prepared: PreparedRequest) -> RequestSize:
    system, knowledge, volatile = 0, 0, 0
    for message in prepared.input:
        if isinstance(message, TextMessage) and isinstance(message.value, GuidanceContent):
            knowledge += len(message.text)
        elif isinstance(message, TextMessage) and message.role is TextRole.SYSTEM:
            system += len(message.text)
        elif isinstance(message, AssistantMessage):
            volatile += len(message.native.to_json())
        else:
            volatile += len(message.text)
    schemas = json.dumps(
        [
            {
                "name": tool.name,
                "description": tool.description.body,
                "parameters": tool.parameters.model_json_schema(),
            }
            for tool in prepared.tools
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    measurement = prepared.measurement
    return RequestSize(
        system_characters=system,
        tools_characters=len(schemas),
        knowledge_characters=knowledge,
        volatile_characters=volatile,
        encoded_bytes=measurement.encoded_bytes,
        estimated_input_tokens=measurement.estimated_input_tokens,
        digest=measurement.digest,
    )


class MeasuredModel:
    def __init__(self, client: ModelClient) -> None:
        self.client = client
        self.sizes: list[RequestSize] = []
        self.usage: list[dict[str, int | None]] = []
        self.calls: list[dict[str, JsonValue]] = []
        self.bodies: list[dict[str, JsonValue]] = []
        self.replies: list[str] = []
        self.attempts: list[dict[str, JsonValue]] = []

    @property
    def capacity_policy(self) -> CapacityPolicy:
        return self.client.capacity_policy

    def prepare(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> PreparedRequest:
        return self.client.prepare(input, tools)

    async def record_recovery(self, recovery: CapacityRecovery) -> None:
        await self.client.record_recovery(recovery)

    async def complete_prepared(
        self, prepared: PreparedRequest, *, preparation: RequestPreparation | None = None
    ) -> ModelReply:
        self.sizes.append(request_size(prepared))
        self.bodies.append(prepared.body)
        if isinstance(self.client, Llm):
            reply = await self.client.complete_prepared(
                prepared,
                preparation=preparation,
                evidence=_AttemptCapture(self.attempts, len(self.bodies)),
            )
        else:
            reply = await self.client.complete_prepared(prepared, preparation=preparation)
        self.replies.append(reply.assistant.native.to_json())
        usage = reply.usage
        self.usage.append(
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            }
        )
        self.calls.extend(
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in reply.assistant.tool_calls
        )
        return reply

    async def interrupt_preparation(
        self,
        preparation: RequestPreparation,
        tools: tuple[ToolDefinition, ...],
        failure: ModelUnavailable,
    ) -> None:
        await self.client.interrupt_preparation(preparation, tools, failure)

    async def complete(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> ModelReply:
        return await self.complete_prepared(self.prepare(input, tools))


class _AttemptCapture:
    def __init__(self, attempts: list[dict[str, JsonValue]], request: int) -> None:
        self.attempts = attempts
        self.request = request
        self.current: dict[str, JsonValue] | None = None

    def started(self, index: int) -> None:
        self.current = {"request": self.request, "attempt": index}
        self.attempts.append(self.current)

    def encoded(self, measurement: Measurement) -> None:
        assert self.current is not None
        self.current.update(
            {
                "digest": measurement.digest,
                "encoded_bytes": measurement.encoded_bytes,
                "estimated_input_tokens": measurement.estimated_input_tokens,
            }
        )

    def received(self, reply: ModelReply) -> None:
        assert self.current is not None
        self.current.update(
            {
                "native_output": reply.assistant.native.to_json(),
                "input_tokens": reply.usage.input_tokens,
                "output_tokens": reply.usage.output_tokens,
                "cache_read_tokens": reply.usage.cache_read_tokens,
                "cache_write_tokens": reply.usage.cache_write_tokens,
                "reasoning_tokens": reply.usage.reasoning_tokens,
                "response_model": reply.metadata.response_model,
                "response_service_tier": reply.metadata.response_service_tier,
            }
        )

    def failed(self, reason: FailureKind, *, capacity_signal: str | None = None) -> None:
        assert self.current is not None
        self.current.update({"failure": reason.value, "capacity_signal": capacity_signal})


async def sample_case(
    case: DecisionCase,
    client: ModelClient,
    *,
    sample: int = 1,
    materials: EntryMaterials | None = None,
    prompts: PromptReader | None = None,
    variant: DecisionVariant = DecisionVariant.RUN,
) -> DecisionSample:
    if variant is DecisionVariant.DECK and case.decision_tool != ToolName.RUN_DECK_CHOICE:
        return DecisionSample(
            source=case.source,
            sample=sample,
            verdict=DecisionVerdict.NOT_APPLICABLE,
            choices=(),
            reason="The deck role exposes card offers only.",
        )
    started = monotonic()
    measured = MeasuredModel(client)
    choices: tuple[str, ...] = ()
    reason, error, over = "", None, None
    verdict = DecisionVerdict.DEVIATED
    try:
        rebuilt = await rebuild_case(
            case, measured, materials=materials, prompts=prompts, variant=variant
        )
        await rebuilt.agent.decide(rebuilt.task, rebuilt.observation)
        error = "The role returned without making the labeled decision."
    except _ChoiceMade as chosen:
        choices = chosen.choices
        arguments = _JSON.validate_json(chosen.call.arguments)
        value = arguments.get("reason")
        reason = value if isinstance(value, str) else ""
        value = arguments.get("over")
        over = value if isinstance(value, str) else None
        verdict = score(case.labels, choices)
    except DecisionDeviation as failure:
        error = str(failure)
    except DecisionInterrupted as failure:
        error = str(failure)
        verdict = (
            DecisionVerdict.DEVIATED
            if isinstance(failure.reason, Reason)
            and failure.reason.code
            in (
                ReasonCode.INVALID_ARGUMENTS,
                ReasonCode.WRONG_ROLE,
            )
            else DecisionVerdict.UNSUPPORTED
        )
    except (ExtractionError, ModelUnavailable) as failure:
        error = str(failure)
        verdict = DecisionVerdict.UNSUPPORTED
    return DecisionSample(
        source=case.source,
        sample=sample,
        verdict=verdict,
        choices=choices,
        reason=reason,
        error=error,
        requests=tuple(measured.sizes),
        usage=tuple(measured.usage),
        elapsed_seconds=round(monotonic() - started, 3),
        over=over,
        raw_calls=tuple(measured.calls),
        request_bodies=tuple(measured.bodies),
        model_replies=tuple(measured.replies),
        attempts=tuple(measured.attempts),
    )


def recorded(case: RecordedDecision) -> DecisionSample:
    return DecisionSample(
        source=case.source,
        sample=0,
        verdict=score(case.labels, case.recorded_choices),
        choices=case.recorded_choices,
        reason=case.recorded_reason,
    )


def _usage_summary(values: Sequence[int | None]) -> str:
    if not values:
        return "0 (no requests)"
    known = tuple(value for value in values if value is not None)
    coverage = f"{len(known)}/{len(values)} known"
    if not known:
        return f"unknown ({coverage})"
    partial = "partial; " if len(known) != len(values) else ""
    return f"{sum(known)} ({partial}{coverage})"


def _report(samples: tuple[DecisionSample, ...]) -> str:
    rows = [
        "| Round / seed / floor | Sample | Choice | Result | Reason |",
        "|---|---:|---|---|---|",
    ]
    for sample in samples:
        source = sample.source
        reason = (sample.error or sample.reason).replace("|", "\\|").replace("\n", " ")
        rows.append(
            f"| {source.round} / {source.seed} / {source.floor} | {sample.sample} | "
            f"{', '.join(sample.choices)} | {sample.verdict.value} | {reason} |"
        )
    verdict_counts = {
        verdict: sum(sample.verdict is verdict for sample in samples) for verdict in DecisionVerdict
    }
    eligible = (
        len(samples)
        - verdict_counts[DecisionVerdict.UNSUPPORTED]
        - verdict_counts[DecisionVerdict.NOT_APPLICABLE]
    )
    rows.append(f"\nPassed {verdict_counts[DecisionVerdict.PASSED]}/{eligible} eligible samples.")
    rows.append(" ".join(f"{verdict.value}: {count}." for verdict, count in verdict_counts.items()))
    rows.extend(
        (
            "\n| Round / seed / floor | Sample | Requests / attempts | Input / output tokens | "
            "Cache read / write tokens | Seconds |",
            "|---|---:|---:|---:|---:|---:|",
        )
    )
    totals: dict[str, list[int | None]] = {
        key: []
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    }
    for sample in samples:
        usage = sample.attempts if sample.attempts else sample.usage
        entries = usage or tuple({} for _ in sample.requests)
        values = {
            key: [value if isinstance(value := entry.get(key), int) else None for entry in entries]
            for key in totals
        }
        for key, metric in values.items():
            totals[key].extend(metric)
        counts = {key: _usage_summary(metric) for key, metric in values.items()}
        source = sample.source
        rows.append(
            f"| {source.round} / {source.seed} / {source.floor} | {sample.sample} | "
            f"{len(sample.requests)} / {len(sample.attempts)} | "
            f"{counts['input_tokens']} / {counts['output_tokens']} | "
            f"{counts['cache_read_tokens']} / {counts['cache_write_tokens']} | "
            f"{sample.elapsed_seconds} |"
        )
    rows.append(
        f"\nTotal: {sum(len(sample.requests) for sample in samples)} logical requests, "
        f"{sum(len(sample.attempts) for sample in samples)} attempts, "
        f"{_usage_summary(totals['input_tokens'])} input and "
        f"{_usage_summary(totals['output_tokens'])} output tokens, "
        f"{_usage_summary(totals['cache_read_tokens'])} cache-read and "
        f"{_usage_summary(totals['cache_write_tokens'])} cache-write tokens, "
        f"{sum(sample.elapsed_seconds for sample in samples):.3f} seconds."
    )
    rows.append(
        "Request sizes (system, tools, knowledge, volatile text, encoded bytes and estimates), "
        "full request bodies, native replies and per-attempt evidence are in results.json."
    )
    return "\n".join(rows) + "\n"


def write_results(destination: Path, samples: tuple[DecisionSample, ...]) -> None:
    destination.joinpath("results.json").write_text(
        TypeAdapter(tuple[DecisionSample, ...]).dump_json(samples, indent=2).decode() + "\n"
    )
    destination.joinpath("report.md").write_text(_report(samples))


def load_case(path: Path, *, replay: bool = True) -> RecordedDecision:
    """Keep recorded metadata usable when a historical native schema cannot load."""
    text = path.read_text()
    fields = _JSON.validate_json(text)
    header = RecordedDecision.model_validate_json(
        json.dumps({key: fields.get(key) for key in RecordedDecision.model_fields})
    )
    if not replay:
        return header
    try:
        return DecisionCase.model_validate_json(text)
    except ValidationError:
        return header


def main(argv: Sequence[str] | None = None, *, materials: EntryMaterials | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("recorded", "replay"))
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--variant", choices=tuple(DecisionVariant), default=DecisionVariant.RUN)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        cases_path: Path = args.cases
        paths = sorted(cases_path.glob("*.json")) if cases_path.is_dir() else [cases_path]
        cases = tuple(load_case(path, replay=args.command == "replay") for path in paths)
        if args.samples < 1 or not cases:
            raise ValueError("Choose at least one case and sample.")
        destination: Path = args.output
        destination.mkdir(parents=True, exist_ok=False)
        variant = DecisionVariant(args.variant)
        if args.command == "recorded":
            samples = tuple(recorded(case) for case in cases)
        else:
            configuration = load_configuration(args.env_file, os.environ, {})
            if configuration.model is None:
                raise ConfigurationError("Decision replay requires a configured builtin model.")

            async def run() -> tuple[DecisionSample, ...]:
                assert configuration.model is not None
                results: list[DecisionSample] = []
                purpose = ModelPurpose.DECK if variant is DecisionVariant.DECK else ModelPurpose.RUN
                async with Llm(configuration.model.for_purpose(purpose)) as client:
                    for case in cases:
                        for index in range(args.samples):
                            if not isinstance(case, DecisionCase):
                                results.append(
                                    DecisionSample(
                                        source=case.source,
                                        sample=index + 1,
                                        verdict=DecisionVerdict.UNSUPPORTED,
                                        choices=(),
                                        reason="",
                                        error="The historical case schema requires re-extraction.",
                                    )
                                )
                                write_results(destination, tuple(results))
                                break
                            results.append(
                                await sample_case(
                                    case,
                                    client,
                                    sample=index + 1,
                                    materials=materials,
                                    variant=variant,
                                )
                            )
                            write_results(destination, tuple(results))
                return tuple(results)

            samples = asyncio.run(run())
        write_results(destination, samples)
        return 0
    except (OSError, ValueError, ValidationError, ConfigurationError) as error:
        print(f"Decision replay error: {type(error).__name__}.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
