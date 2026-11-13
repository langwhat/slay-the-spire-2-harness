"""Extract native semantic inputs from an exactly replayed source version."""

import asyncio
import json
from contextlib import closing
from dataclasses import dataclass, field
from hashlib import sha256

from pydantic import MISSING, JsonValue, TypeAdapter, ValidationError

from sts2_harness.capacity import CompactionDecision
from sts2_harness.client import models as native
from sts2_harness.context.map_projection import Plan
from sts2_harness.context.models import (
    BaselineMessage,
    RunActContent,
    RunActInput,
    RunAutomaticMessage,
    RunHandoffInput,
    RunResultMessage,
    TaskMessage,
)
from sts2_harness.game import Catalog, Cataloged, Inspect, Inspected, Pause, Settle, Settlement
from sts2_harness.journal import Journal, Record
from sts2_harness.journal_models import (
    BaselineSnapshot,
    ModelRecord,
    NativeSnapshot,
    PreparationEvidence,
    PromptRead,
    ResultSnapshot,
    archive_input,
)
from sts2_harness.knowledge import (
    Document,
    Documents,
    DocumentView,
    GuidanceContent,
    KnowledgeLookup,
    SkillList,
    SubjectId,
)
from sts2_harness.llm.messages import (
    AssistantMessage,
    ModelClient,
    ModelInput,
    ModelReply,
    RequestPreparation,
    TextMessage,
    TextRole,
    ToolCall,
    ToolDefinition,
)
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    NotesText,
    NotesUpdated,
    PlanData,
    ReadCompleted,
)
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness.user_input import UserInputQueue
from sts2_harness_eval._replay import (
    ReplayDocuments,
    ReplayGame,
    ReplayModel,
    ReplayPrompts,
    ReplayRejected,
    Timeline,
    prefix,
)
from sts2_harness_eval.decision_cases import (
    AssistantEvent,
    AutomaticEvent,
    BattleState,
    CorrectionEvent,
    DecisionCase,
    DecisionLabels,
    DecisionSource,
    HarnessState,
    HistoryEvent,
    NativeRead,
    ResultEvent,
    RouteState,
    TaskEvent,
)
from sts2_harness_eval.results import ModelAnchor, Reached


class ExtractionError(ValueError):
    """The source cannot establish a portable semantic decision input."""


class _Captured(Exception):
    def __init__(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> None:
        self.input = input
        self.tools = tools


def _baseline_content(value: JsonValue) -> JsonValue:
    decoded = TypeAdapter(dict[str, JsonValue]).validate_python(value, strict=True)
    envelope = decoded.get("context_baseline")
    if not isinstance(envelope, dict):
        return decoded
    observation = envelope.get("observation")
    if not isinstance(observation, dict):
        return decoded
    definitions = observation.get("defs")
    if not isinstance(definitions, list) or any(not isinstance(item, dict) for item in definitions):
        return decoded
    ids = [item.get("id") for item in definitions if isinstance(item, dict)]
    if any(not isinstance(identity, str) for identity in ids) or len(set(ids)) != len(ids):
        return decoded
    observation["defs"] = sorted(definitions, key=lambda item: json.dumps(item, sort_keys=True))
    return decoded


def unused_baseline_permutation(actual: PreparationEvidence, expected: PreparationEvidence) -> bool:
    """Allow ordering only in a baseline candidate that was never delivered."""
    if (
        actual.decision is not CompactionDecision.UNCHANGED
        or expected.decision is not CompactionDecision.UNCHANGED
        or actual.input_after != actual.input_before
        or actual.model_copy(update={"baseline": expected.baseline}) != expected
    ):
        return False
    left, right = actual.baseline, expected.baseline
    if (
        not isinstance(left, BaselineSnapshot)
        or not isinstance(right, BaselineSnapshot)
        or left.role != right.role
    ):
        return False
    return (
        json.loads(left.text) == left.content
        and json.loads(right.text) == right.content
        and _baseline_content(left.content) == _baseline_content(right.content)
    )


@dataclass(slots=True)
class ExtractionAudit:
    unused_baseline_permutations: list[dict[str, JsonValue]] = field(
        default_factory=lambda: list[dict[str, JsonValue]]()
    )
    validated_model_sequences: list[int] = field(default_factory=lambda: list[int]())
    recorded_completion: dict[str, JsonValue] = field(
        default_factory=lambda: dict[str, JsonValue]()
    )


class _SourceModel(ReplayModel):
    audit: ExtractionAudit

    async def complete(
        self,
        input: ModelInput,
        tools: tuple[ToolDefinition, ...],
        *,
        preparation: RequestPreparation | None = None,
    ) -> ModelReply:
        sequence = self._next_record().sequence
        try:
            reply = await super().complete(input, tools, preparation=preparation)
        except _Captured:
            self.audit.validated_model_sequences.append(sequence)
            raise
        self.audit.validated_model_sequences.append(sequence)
        return reply

    def _check_preparation(
        self, record: ModelRecord, preparation: RequestPreparation | None
    ) -> None:
        try:
            super()._check_preparation(record, preparation)
            return
        except ReplayRejected:
            expected = record.preparation
            if (
                preparation is None
                or expected is None
                or preparation.decision is not CompactionDecision.UNCHANGED
                or expected.decision is not CompactionDecision.UNCHANGED
            ):
                raise
            actual = PreparationEvidence(
                group_id=preparation.group_id,
                baseline=archive_input((preparation.baseline,))[0]
                if preparation.baseline is not None
                else None,
                input_before=archive_input(preparation.input_before),
                estimate_before=preparation.estimate_before,
                decision=preparation.decision,
                input_after=archive_input(preparation.input_after)
                if preparation.input_after is not None
                else None,
                estimate_after=preparation.estimate_after,
                retained_complete_group_id=preparation.retained_complete_group_id,
                active_group_id=preparation.active_group_id,
                validation=preparation.validation,
            )
            target = PreparationEvidence.model_validate(
                expected.model_dump(include=set(PreparationEvidence.model_fields))
            )
            if not unused_baseline_permutation(actual, target):
                raise
            left, right = actual.baseline, target.baseline
            assert isinstance(left, BaselineSnapshot) and isinstance(right, BaselineSnapshot)
            self.audit.unused_baseline_permutations.append(
                {
                    "sequence": record.sequence,
                    "actual_sha256": sha256(left.text.encode()).hexdigest(),
                    "source_sha256": sha256(right.text.encode()).hexdigest(),
                    "canonical_sha256": sha256(
                        json.dumps(
                            _baseline_content(left.content), ensure_ascii=False, sort_keys=True
                        ).encode()
                    ).hexdigest(),
                }
            )


def choices_for(call: ToolCall, observation: native.Observation) -> tuple[str, ...]:
    """Resolve native instance refs without guessing from localized labels."""
    arguments = TypeAdapter(dict[str, JsonValue]).validate_json(call.arguments)
    refs: list[str] = []
    if call.name == "run_deck_choice":
        if arguments.get("choice") == "skip":
            return ("skip",)
        card = arguments.get("card")
        if not isinstance(card, str):
            raise ExtractionError("The deck choice has no card reference.")
        refs.append(card)
    elif call.name == "run_act":
        steps = arguments.get("steps")
        if not isinstance(steps, list):
            raise ExtractionError("The action has no native steps.")
        for step in steps:
            if not isinstance(step, dict):
                raise ExtractionError("The action step is invalid.")
            if step.get("kind") == "select":
                selected = step.get("refs")
                if not isinstance(selected, list) or any(
                    not isinstance(ref, str) for ref in selected
                ):
                    raise ExtractionError("The selection has no native references.")
                refs.extend(ref for ref in selected if isinstance(ref, str))
            elif step.get("kind") == "interact":
                action_ref = step.get("action")
                state = observation.state
                action = next((a for a in state.actions if a.ref == action_ref), None)
                if action is None or not isinstance(action.subject, str):
                    raise ExtractionError("The event action exposes no native subject.")
                refs.append(action.subject)
            else:
                raise ExtractionError("The action is not a labeled native decision.")
    else:
        raise ExtractionError("A different game action deviated from the labeled decision.")
    if not refs:
        return ("skip",)
    identities: list[str] = []
    for ref in refs:
        entry = observation.objects.get(ref)
        if entry is None or not isinstance(entry.definition_id, str):
            raise ExtractionError("The selected native object exposes no definition ID.")
        identities.append(entry.definition_id)
    return tuple(dict.fromkeys(identities))


def _state(message: TaskMessage, previous: HarnessState) -> HarnessState:
    content = message.content
    if not isinstance(content, RunActContent):
        return previous
    observation = content.observation
    notes, added, skipped = previous.notes, previous.added, previous.skipped
    plays, route = previous.observed_plays, previous.route
    if observation.notes is not MISSING:
        notes = TypeAdapter(dict[str, JsonValue]).validate_json(observation.notes.model_dump_json())
    stats = observation.deck_stats
    if isinstance(stats, dict):
        count = stats.get("added")
        if isinstance(count, int):
            added = count
        count = stats.get("skipped")
        if isinstance(count, int):
            skipped = count
    if observation.deck is not MISSING:
        plays = {}
        for card in observation.deck:
            use, identity = card.get("use"), card.get("id")
            if isinstance(identity, str) and isinstance(use, dict):
                count = use.get("observed_plays")
                if isinstance(count, int):
                    plays[identity.split("+")[0]] = count
    dynamic = observation.dynamic_map
    if isinstance(dynamic, dict):
        plan, position = dynamic.get("plan"), dynamic.get("position")
        if isinstance(plan, dict) and isinstance(position, str):
            parsed = Plan.model_validate_json(json.dumps(plan))
            route = RouteState(
                position=position,
                edges=tuple((edge.source, edge.destination) for edge in parsed.edges),
            )
        else:
            route = None

    return HarnessState(
        notes=notes, added=added, skipped=skipped, observed_plays=plays, route=route
    )


def _native_observation(
    delivered: native.Observation, originals: tuple[native.Observation, ...]
) -> tuple[native.Observation, tuple[str, ...]]:
    candidates = (
        raw
        for raw in reversed(originals)
        if raw.stamp == delivered.stamp and raw.location == delivered.location
    )
    for raw in candidates:
        if raw == delivered:
            return raw, ()
        before, after = raw.state, delivered.state
        if not isinstance(before, native.RunState) or not isinstance(after, native.RunState):
            continue
        declined = tuple(offer.ref for offer in before.offers if offer not in after.offers)
        actions = {
            action for offer in before.offers if offer.ref in declined for action in offer.actions
        }
        filtered = raw.model_copy(
            update={
                "state": before.model_copy(
                    update={
                        "offers": tuple(
                            offer for offer in before.offers if offer.ref not in declined
                        ),
                        "actions": tuple(
                            action for action in before.actions if action.ref not in actions
                        ),
                    }
                )
            }
        )
        if filtered == delivered:
            return raw, declined
    raise ExtractionError("A context observation has no matching raw native reply.")


def _events(
    input: ModelInput, originals: tuple[native.Observation, ...]
) -> tuple[HistoryEvent, ...]:
    events: list[HistoryEvent] = []
    state = HarnessState()
    for message in input:
        if isinstance(message, BaselineMessage):
            raise ExtractionError("Compacted source history requires extraction before compaction.")
        if isinstance(message, TaskMessage):
            task = message.task
            if not isinstance(task, (RunActInput, RunHandoffInput)):
                raise ExtractionError("The source is not a Run decision.")
            if task.strategy_notes is not MISSING:
                state = state.model_copy(
                    update={
                        "notes": TypeAdapter(dict[str, JsonValue]).validate_json(
                            task.strategy_notes.model_dump_json()
                        )
                    }
                )
            state = _state(message, state)
            observation = message.observation
            if observation is None:
                observation = next(
                    (
                        event.observation
                        for event in reversed(events)
                        if isinstance(event, TaskEvent)
                    ),
                    None,
                )
            if observation is None:
                raise ExtractionError("A handoff-only source has no native observation.")
            observation, declined = _native_observation(observation, originals)
            handoff = task.handoff
            previous = task.previous_battle if isinstance(task, RunActInput) else None
            events.append(
                TaskEvent(
                    observation=observation,
                    state=state,
                    handoff=BattleState(report=handoff.report, review=handoff.review)
                    if handoff is not None
                    else None,
                    previous_battle=BattleState(report=previous.report, review=previous.review)
                    if previous is not None
                    else None,
                    handoff_only=isinstance(task, RunHandoffInput),
                    declined_offers=declined,
                )
            )
        elif isinstance(message, AssistantMessage):
            events.append(
                AssistantEvent(
                    reply=NativeSnapshot(
                        protocol=message.native.protocol, native_output=message.native.to_json()
                    )
                )
            )
        elif isinstance(message, RunResultMessage):
            source = message.source
            event = ResultEvent(
                call_id=message.call_id,
                outcome=source.outcome,
                action=isinstance(source, ActionToolResult),
                observation=source.observation,
            )
            if source.observation is not None:
                raw, declined = _native_observation(source.observation, originals)
                event = event.model_copy(update={"observation": raw, "declined_offers": declined})
            if not isinstance(source, ActionToolResult) and source.data is not MISSING:
                data = source.data
                if isinstance(data, DocumentView):
                    event = event.model_copy(
                        update={
                            "document": (
                                data.id,
                                None if data.resource is MISSING else data.resource,
                            )
                        }
                    )
                elif isinstance(data, KnowledgeLookup):
                    event = event.model_copy(
                        update={
                            "knowledge_ids": tuple(
                                entry.definition.definition_id for entry in data.entries
                            )
                        }
                    )
                elif isinstance(data, SkillList):
                    event = event.model_copy(update={"skill_listing": (0, len(data.skills))})
                elif isinstance(data, NotesText):
                    event = event.model_copy(update={"notes_read": True})
                elif isinstance(data, NotesUpdated):
                    event = event.model_copy(update={"notes_updated": True})
                elif isinstance(data, PlanData):
                    event = event.model_copy(update={"plan": data})
                else:
                    event = event.model_copy(update={"native_data": data})
            events.append(event)
        elif isinstance(message, RunAutomaticMessage):
            raw, declined = (
                _native_observation(message.source.observation, originals)
                if message.source.observation is not None
                else (None, ())
            )
            events.append(
                AutomaticEvent(
                    progress=message.content.automatic_progress,
                    outcome=message.source.outcome,
                    observation=raw,
                    invalidated=message.content.route_plan_status is not MISSING,
                    declined_offers=declined,
                )
            )
        elif isinstance(message, TextMessage):
            if message.role is TextRole.SYSTEM or isinstance(message.value, GuidanceContent):
                continue
            if message.text.startswith("[viewer] "):
                raise ExtractionError("Viewer intervention requires a separately labeled case.")
            events.append(CorrectionEvent(message=message.text))
    return tuple(events)


class _SourceDocuments(ReplayDocuments):
    def __init__(self, timeline: Timeline, source: Documents | None) -> None:
        super().__init__(timeline)
        self.source = source

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        archived = await super().lookup(subjects)
        if self.source is None:
            return archived
        # The archival document snapshot omits auto_roles. Recover only that metadata
        # from the matching source package; all delivered text remains archive-bound.
        source = {
            (document.id, document.resource): document
            for document in await self.source.lookup(subjects)
        }
        if any(
            (document.id, document.resource) not in source
            or source[(document.id, document.resource)].subjects != document.subjects
            for document in archived
        ):
            raise ExtractionError("The source document package does not match the archive.")
        return tuple(
            document.model_copy(
                update={"auto_roles": source[(document.id, document.resource)].auto_roles}
            )
            for document in archived
        )


def _deck_count(observation: native.Observation, identity: str) -> int:
    return (
        sum(
            isinstance(entry := observation.objects.get(ref), native.CardEntry)
            and entry.definition_id == identity
            for ref in observation.state.deck
        )
        if isinstance(observation.state, native.RunState)
        else 0
    )


def _recorded_completion(
    journal: Journal,
    source: DecisionSource,
    call: ToolCall,
    observation: native.Observation,
    tools: tuple[ToolDefinition, ...],
) -> dict[str, JsonValue]:
    schema = next((tool.parameters for tool in tools if tool.name == call.name), None)
    if schema is None:
        raise ExtractionError("The recorded decision has no source tool schema.")
    try:
        schema.model_validate_json(call.arguments, strict=True)
    except (ValueError, ValidationError):
        raise ExtractionError(
            "The recorded decision was rejected by its source tool schema."
        ) from None
    arguments = TypeAdapter(dict[str, JsonValue]).validate_json(call.arguments)
    chosen = arguments.get("card")
    if call.name == "run_act":
        steps = arguments.get("steps")
        if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
            raise ExtractionError("The recorded decision is not one native selection.")
        step = native.Select.model_validate_json(json.dumps(steps[0]))
    else:
        step = native.Select(
            kind=native.StepKind.SELECT, refs=(chosen,) if isinstance(chosen, str) else ()
        )
    following = journal.requests(start=source.model_sequence + 1, stop=source.model_sequence + 200)
    next_request = following[0] if following else None
    if next_request is not None:
        for message in next_request.input:
            if not isinstance(message, ResultSnapshot) or message.call_id != call.id:
                continue
            completed = (
                isinstance(message.outcome, ActionCompleted) and message.outcome.completed_steps > 0
            )
            skipped = arguments.get("choice") == "skip" and isinstance(
                message.outcome, ReadCompleted
            )
            if not completed and not skipped:
                raise ExtractionError("The recorded decision did not complete in its source run.")
            return {"kind": "tool_result", "sequence": next_request.sequence, "call_id": call.id}
    stop = next_request.sequence - 1 if next_request is not None else source.model_sequence + 200
    route = native.RouteKey.model_validate(observation.location.model_dump())
    operations = journal.operations(start=source.model_sequence + 1, stop=stop)
    for index, operation in enumerate(operations):
        if (
            not isinstance(operation.request, Settle)
            or operation.request.route != route
            or operation.request.step != step
            or operation.reply is None
            or not isinstance(operation.reply.outcome, Settlement)
            or not isinstance(operation.reply.outcome.receipt, native.ReceiptCompleted)
        ):
            continue
        if isinstance(chosen, str):
            candidate = observation.objects.get(chosen)
            if not isinstance(candidate, native.CardEntry):
                raise ExtractionError("The recorded card has no native definition identity.")
            identity = candidate.definition_id
            following_write = next(
                (
                    record.sequence
                    for record in operations[index + 1 :]
                    if isinstance(record.request, Settle)
                ),
                stop + 1,
            )
            if not any(
                _deck_count(after, identity) > _deck_count(observation, identity)
                for record in operations[index:]
                if record.sequence < following_write and record.reply is not None
                for after in record.reply.observations
            ):
                continue
        return {"kind": "native_completed", "sequence": operation.sequence, "call_id": call.id}
    raise ExtractionError("The recorded decision has no bounded source completion proof.")


async def extract_case(
    source: DecisionSource,
    labels: DecisionLabels,
    *,
    source_documents: Documents | None = None,
    audit: ExtractionAudit | None = None,
) -> DecisionCase:
    """Run with the source version's PYTHONPATH; this performs no live IO."""
    with closing(Journal.open(source.archive)) as journal:
        target = ModelAnchor(journal.manifest.recording_id, source.model_sequence)
        timeline = Timeline(prefix(journal, target))
        reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
        models: list[_SourceModel] = []
        evidence = audit if audit is not None else ExtractionAudit()

        async def capture(
            input: ModelInput,
            tools: tuple[ToolDefinition, ...],
            preparation: RequestPreparation | None,
        ) -> ModelReply:
            raise _Captured(input, tools)

        def model(role: native.DecisionOwner, stream: str) -> ModelClient:
            client = _SourceModel(timeline, role, stream, target, reached, capture)
            client.audit = evidence
            models.append(client)
            return client

        documents = _SourceDocuments(timeline, source_documents)
        dependencies = SessionDependencies(
            ReplayGame(timeline),
            ReplayPrompts(timeline),
            documents,
            model,
            documents if journal.manifest.materials.candidate_links_enabled else None,
        )
        viewer = any(
            isinstance(record.request, PromptRead) and record.request.name == "viewer-input"
            for record in journal.materials(stop=source.model_sequence)
        )
        captured_input: ModelInput | None = None
        captured_tools: tuple[ToolDefinition, ...] = ()
        try:
            async with RunSession(
                journal.manifest, dependencies, user_input=UserInputQueue() if viewer else None
            ) as session:
                first = timeline.records[0]
                if isinstance(first, Record) and first.request == Pause():
                    await session.start()
                await session.run()
        except _Captured as captured:
            captured_input = captured.input
            captured_tools = captured.tools
        finally:
            for client in models:
                await client.close()
        if not reached.done() or captured_input is None:
            raise ExtractionError("The source replay did not reach its model anchor.")
        originals = tuple(
            observation
            for operation in journal.operations(stop=source.model_sequence)
            if operation.reply is not None
            for observation in operation.reply.observations
        )
        events = _events(captured_input, originals)
        last_task = next(event for event in reversed(events) if isinstance(event, TaskEvent))
        record = journal.request(source.model_sequence)
        reply = record.attempts[-1].reply
        if reply is None:
            raise ExtractionError("The source decision has no recorded reply.")
        calls = AssistantMessage(reply.native_reply).tool_calls
        actions = tuple(
            call
            for call in calls
            if call.name
            in (
                "run_deck_choice",
                "run_act",
                "run_next",
                "run_plan",
            )
        )
        if len(actions) != 1 or actions[0].name not in ("run_deck_choice", "run_act"):
            raise ExtractionError("The source reply must contain one labeled game decision.")
        action = actions[0]
        evidence.recorded_completion = _recorded_completion(
            journal,
            source,
            action,
            last_task.observation,
            captured_tools,
        )
        arguments = json.loads(action.arguments)
        reads: list[NativeRead] = []
        for operation in journal.operations(stop=source.model_sequence):
            if operation.reply is None:
                continue
            outcome = operation.reply.outcome
            if isinstance(outcome, (Inspected, Cataloged)) and isinstance(
                outcome.result, (native.InspectCompleted, native.CatalogCompleted)
            ):
                assert isinstance(operation.request, (Inspect, Catalog))
                reads.append(NativeRead(request=operation.request, result=outcome.result))
        act = last_task.observation.state.progress.act
        return DecisionCase(
            source=source,
            labels=labels,
            minimal=journal.manifest.minimal,
            viewer=viewer,
            entry_documents_enabled=journal.manifest.materials.entry_documents_enabled,
            configured_guidance=journal.manifest.materials.act_guidance.get(act, ())
            if act is not None
            else (),
            candidate_links_enabled=journal.manifest.materials.candidate_links_enabled,
            events=events,
            native_reads=tuple(reads),
            decision_tool=action.name,
            decision_offer=arguments.get("offer"),
            recorded_choices=choices_for(action, last_task.observation),
            recorded_reason=arguments.get("reason", ""),
        )
