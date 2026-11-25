"""Extract native semantic inputs from an exactly replayed source version."""

import asyncio
import json
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from hashlib import sha256

from pydantic import MISSING, BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from sts2_harness.capacity import CompactionDecision
from sts2_harness.client import models as native
from sts2_harness.context.map_projection import MapUnavailable, restore_stretches
from sts2_harness.context.models import (
    BaselineMessage,
    RunActContent,
    RunActInput,
    RunAutomaticMessage,
    RunHandoffInput,
    RunResultMessage,
    TaskMessage,
)
from sts2_harness.context.run_models import RunObservation
from sts2_harness.decisions import CardRewardChoice, CardRewardPlan, SelectStep
from sts2_harness.deck import CardChoiceKind, card_rewards, deck_observation
from sts2_harness.execution.rewards import reward_steps
from sts2_harness.fights import Fight, FightHistory
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
    PreparedRequest,
    RequestPreparation,
    TextMessage,
    TextRole,
    ToolCall,
    ToolDefinition,
)
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    CardRewardBatchResult,
    NotesText,
    NotesUpdated,
    PlanData,
    QueryToolResult,
    ReadCompleted,
)
from sts2_harness.route_plan import reconcile_plan
from sts2_harness.run import RunRole, run_observation
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness.tools import ToolName
from sts2_harness.tools import inputs as tool_inputs
from sts2_harness.user_input import UserInputQueue
from sts2_harness_eval._replay import (
    ReplayDocuments,
    ReplayGame,
    ReplayModel,
    ReplayPrompts,
    ReplayRejected,
    Timeline,
    document_view,
    prefix,
    unsupported,
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
    RewardGroupDecision,
    RouteState,
    TaskEvent,
)
from sts2_harness_eval.results import ModelAnchor, Reached, UnsupportedCause


class ExtractionError(ValueError):
    """The source cannot establish a portable semantic decision input."""


class _Captured(Exception):
    def __init__(self, input: ModelInput, tools: tuple[ToolDefinition, ...]) -> None:
        self.input = input
        self.tools = tools


class _SourceSession(RunSession):
    run_role: RunRole

    def _new_run(self) -> RunRole:
        self.run_role = super()._new_run()
        return self.run_role


type SingleDecisionInput = tool_inputs.RunAct | tool_inputs.TakeCard | tool_inputs.SkipCard
type DecisionInput = SingleDecisionInput | tool_inputs.RunDeckChoice


def decision_input(call: ToolCall) -> DecisionInput:
    """Decode a labeled game decision call into its typed tool input."""
    try:
        match call.name:
            case ToolName.RUN_DECK_CHOICE:
                try:
                    return tool_inputs.RunDeckChoice.model_validate_json(
                        call.arguments, strict=True
                    )
                except ValidationError:
                    # Frozen evidence from the single-reward interface retains its original shape.
                    return TypeAdapter(tool_inputs.TakeCard | tool_inputs.SkipCard).validate_json(
                        call.arguments, strict=True
                    )
            case ToolName.RUN_ACT:
                return tool_inputs.RunActInput.model_validate_json(call.arguments, strict=True).root
            case _:
                raise ExtractionError("A different game action deviated from the labeled decision.")
    except ValidationError:
        raise ExtractionError(
            f"The {call.name} arguments do not match its labeled decision input."
        ) from None


# Baseline content keeps every recorded field; only the fields used to order definitions are typed.
class _BaselineDefinition(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    id: str


class _BaselineObservation(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    defs: list[_BaselineDefinition]


class _BaselineEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    observation: _BaselineObservation


class _BaselineContent(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    context_baseline: _BaselineEnvelope


def _baseline_content(value: JsonValue) -> JsonValue:
    decoded = TypeAdapter(dict[str, JsonValue]).validate_python(value, strict=True)
    try:
        content = _BaselineContent.model_validate(decoded)
    except ValidationError:
        return decoded
    observation = content.context_baseline.observation
    if len({item.id for item in observation.defs}) != len(observation.defs):
        return decoded
    observation.defs = sorted(
        observation.defs, key=lambda item: json.dumps(item.model_dump(), sort_keys=True)
    )
    return content.model_dump()


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
        prepared: PreparedRequest | None = None,
    ) -> ModelReply:
        sequence = self._next_record().sequence
        try:
            reply = await super().complete(input, tools, preparation=preparation, prepared=prepared)
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


def choices_for(decision: DecisionInput, observation: native.Observation) -> tuple[str, ...]:
    """Resolve native instance refs without guessing from localized labels."""
    refs: list[str] = []
    match decision:
        case tool_inputs.RunDeckChoice():
            return tuple(
                choice for item in decision.choices for choice in choices_for(item, observation)
            )
        case tool_inputs.SkipCard():
            return ("skip",)
        case tool_inputs.TakeCard():
            refs.extend(decision.cards)
        case tool_inputs.RunAct():
            for step in decision.steps:
                match step:
                    case native.Select():
                        refs.extend(step.refs)
                    case native.Interact():
                        state = observation.state
                        action = next((a for a in state.actions if a.ref == step.action), None)
                        if action is None or not isinstance(action.subject, str):
                            raise ExtractionError("The event action exposes no native subject.")
                        refs.append(action.subject)
                    case _:
                        raise ExtractionError("The action is not a labeled native decision.")
    if not refs:
        return ("skip",)
    identities: list[str] = []
    for ref in refs:
        entry = observation.objects.get(ref)
        if entry is None or not isinstance(entry.definition_id, str):
            raise ExtractionError("The selected native object exposes no definition ID.")
        identities.append(entry.definition_id)
    if isinstance(decision, tool_inputs.TakeCard):
        return tuple(identities)
    return tuple(dict.fromkeys(identities))


def reward_groups_for(
    decision: DecisionInput, observation: native.Observation
) -> tuple[RewardGroupDecision, ...]:
    if isinstance(decision, tool_inputs.RunAct):
        return ()
    choices = decision.choices if isinstance(decision, tool_inputs.RunDeckChoice) else (decision,)
    return tuple(
        RewardGroupDecision(
            offer=None if item.offer is MISSING else item.offer,
            choices=choices_for(item, observation),
            reason=item.reason,
        )
        for item in choices
    )


def _state(
    message: TaskMessage,
    previous: HarnessState,
    original: native.Observation,
    originals: tuple[native.Observation, ...],
) -> HarnessState:
    content = message.content
    if not isinstance(content, RunActContent):
        return previous
    return _state_from_observation(content.observation, previous, original, originals)


def _state_from_observation(
    observation: RunObservation,
    previous: HarnessState,
    original: native.Observation,
    originals: tuple[native.Observation, ...],
) -> HarnessState:
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
    map_view = observation.map
    if isinstance(map_view, dict):
        plan = map_view.get("plan")
        if plan is None:
            route = None
        elif isinstance(plan, list) and all(isinstance(item, str) for item in plan):
            if original.map is MISSING:
                raise ExtractionError("The recorded plan has no native map.")
            try:
                restored = restore_stretches(
                    original.map,
                    original.state.progress.position,
                    tuple(item for item in plan if isinstance(item, str)),
                )
            except MapUnavailable as error:
                raise ExtractionError("The recorded map is incomplete.") from error
            if restored.plan is None:
                raise ExtractionError("The recorded route cannot be reconstructed.")
            route = RouteState(
                position=restored.plan.position,
                edges=tuple((edge.source, edge.destination) for edge in restored.plan.edges),
            )
        else:
            raise ExtractionError("The recorded route is not an ordered stretch plan.")
    elif map_view is None:
        route = None

    fights = previous.fights if observation.fights is MISSING else observation.fights
    if any(fight.encounter is None and fight.floor is not None for fight in fights):
        history = FightHistory()
        run_identity: tuple[str, str | None] | None = None
        nodes: dict[int, set[str | None]] = {}
        encounters: dict[int, set[tuple[str, str]]] = {}
        for raw in originals[: originals.index(original) + 1]:
            current_identity = (raw.location.game_instance_id, raw.location.run_id)
            if current_identity != run_identity:
                run_identity = current_identity
                nodes.clear()
                encounters.clear()
            history.observe(raw)
            if isinstance(raw.state, native.CombatState) and raw.state.progress.floor is not None:
                floor = raw.state.progress.floor
                nodes.setdefault(floor, set()).add(raw.location.node_id)
                encounter = raw.encounter
                if encounter is not MISSING and encounter.definition_id is not MISSING:
                    encounters.setdefault(floor, set()).add(
                        (encounter.definition_id, encounter.name)
                    )
        enriched: list[Fight] = []
        for fight in fights:
            candidates = [
                candidate
                for candidate in history.recent
                if candidate.model_copy(update={"encounter": None})
                == fight.model_copy(update={"encounter": None})
            ]
            if (
                fight.encounter is None
                and fight.floor is not None
                and sum(item.floor == fight.floor for item in fights) == 1
                and sum(item.floor == fight.floor for item in history.recent) == 1
                and len(nodes.get(fight.floor, ())) == 1
                and len(encounters.get(fight.floor, ())) == 1
                and len(candidates) == 1
                and candidates[0].encounter is not None
            ):
                fight = fight.model_copy(update={"encounter": candidates[0].encounter})
            enriched.append(fight)
        fights = tuple(enriched)

    elites = FightHistory()
    for raw in originals[: originals.index(original) + 1]:
        elites.observe(raw)

    return HarnessState(
        notes=notes,
        added=added,
        skipped=skipped,
        observed_plays=plays,
        route=route,
        fights=fights,
        elites_fought=elites.act_elites,
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
        if deck_observation(raw, after.offers) == delivered:
            offers, _ = card_rewards(raw)
            visible = {offer.ref for offer in after.offers}
            return raw, tuple(offer.ref for offer in offers if offer.ref not in visible)
        declined = tuple(offer.ref for offer in before.offers if offer not in after.offers)
        if run_observation(raw) == delivered:
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
            state = _state(message, state, observation, originals)
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
                action=isinstance(source, (ActionToolResult, CardRewardBatchResult)),
                observation=source.observation,
            )
            if source.observation is not None:
                raw, declined = _native_observation(source.observation, originals)
                event = event.model_copy(update={"observation": raw, "declined_offers": declined})
            shown = message.content.observation
            if shown is not None and shown.notes is not MISSING:
                event = event.model_copy(update={"notes_after": shown.notes})
                state = state.model_copy(
                    update={
                        "notes": TypeAdapter(dict[str, JsonValue]).validate_json(
                            shown.notes.model_dump_json()
                        )
                    }
                )
            if isinstance(source, QueryToolResult) and source.data is not MISSING:
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
                    notes_after = shown.notes if shown is not None else MISSING
                    event = event.model_copy(
                        update={
                            "notes_updated": True,
                            "notes_summary": data.summary,
                            "notes_after": notes_after,
                        }
                    )
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
            auto_state: HarnessState | None = None
            if message.content.observation is not None and raw is not None:
                state = _state_from_observation(message.content.observation, state, raw, originals)
                auto_state = state
            events.append(
                AutomaticEvent(
                    progress=message.content.automatic_progress,
                    outcome=message.source.outcome,
                    observation=raw,
                    state=auto_state,
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


class SourceDocuments(ReplayDocuments):
    def __init__(self, timeline: Timeline, source: Documents | None) -> None:
        super().__init__(timeline)
        self.source = source

    async def lookup(self, subjects: tuple[SubjectId, ...]) -> tuple[Document, ...]:
        snapshots = await self._lookup_snapshots(subjects)
        source = (
            {
                (document.id, document.resource): document
                for document in await self.source.lookup(subjects)
            }
            if self.source is not None and any(item.auto_roles is MISSING for item in snapshots)
            else {}
        )
        archived: list[Document] = []
        for snapshot in snapshots:
            if snapshot.subjects is MISSING:
                raise ExtractionError("The archived document has no subjects.")
            roles = snapshot.auto_roles
            if roles is MISSING:
                if self.source is None:
                    unsupported(self._timeline.sequence, UnsupportedCause.INCOMPLETE_DEPENDENCY)
                match = source.get((snapshot.id, snapshot.resource))
                if (
                    match is None
                    or match.subjects != snapshot.subjects
                    or match.name.version != snapshot.name.version
                    or match.text.version != snapshot.text.version
                ):
                    raise ExtractionError("The source document package does not match the archive.")
                roles = match.auto_roles
            view = document_view(snapshot)
            archived.append(
                Document(
                    id=view.id,
                    name=view.name,
                    text=view.text,
                    resources=view.resources,
                    resource=view.resource,
                    subjects=snapshot.subjects,
                    auto_roles=roles,
                )
            )
        return tuple(archived)


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


def _source_decision(
    call: ToolCall, tools: tuple[ToolDefinition, ...]
) -> tool_inputs.RunAct | tool_inputs.RunDeckChoice:
    schema = next((tool.parameters for tool in tools if tool.name == call.name), None)
    if schema is None:
        raise ExtractionError("The recorded decision has no source tool schema.")
    try:
        validated = schema.model_validate_json(call.arguments, strict=True)
    except (ValueError, ValidationError):
        raise ExtractionError(
            "The recorded decision was rejected by its source tool schema."
        ) from None
    if isinstance(validated, tool_inputs.RunActInput):
        return validated.root
    if isinstance(validated, tool_inputs.RunDeckChoice):
        return validated
    raise ExtractionError("The recorded decision has no labeled decision input.")


def _recorded_reward_batch(
    journal: Journal,
    source: DecisionSource,
    call: ToolCall,
    decision: tool_inputs.RunDeckChoice,
    task: TaskEvent,
) -> dict[str, JsonValue]:
    observation = task.observation
    available, _ = card_rewards(observation)
    by_ref = {offer.ref: offer for offer in available}
    choices: list[CardRewardChoice] = []
    for item in decision.choices:
        offer = None if item.offer is MISSING else by_ref.get(item.offer)
        if item.offer is not MISSING and offer is None:
            raise ExtractionError("The source frame does not contain the recorded reward group.")
        refs = item.cards if isinstance(item, tool_inputs.TakeCard) else ()
        choices.append(CardRewardChoice(offer, refs, item.reason))
    plan = CardRewardPlan(
        choices=tuple(choices), before=observation, resolved=frozenset(task.declined_offers)
    )
    record = journal.request(source.model_sequence)
    completions = tuple(
        item
        for item in journal.card_choices()
        if item.stream == record.stream and item.call_id == call.id
    )
    if len(completions) != len(choices):
        raise ExtractionError("The reward batch has incomplete typed group evidence.")
    following = journal.requests(start=source.model_sequence + 1, stop=source.model_sequence + 200)
    stop = following[0].sequence - 1 if following else source.model_sequence + 200
    settlements = iter(
        (operation.sequence, operation.request, operation.reply)
        for operation in journal.operations(start=source.model_sequence + 1, stop=stop)
        if isinstance(operation.request, Settle)
    )
    route = native.RouteKey.model_validate(observation.location.model_dump())
    groups: list[JsonValue] = []
    for index, (item, completion) in enumerate(zip(decision.choices, completions, strict=True)):
        steps = reward_steps(plan, index, completion.before)
        offer_ref = None if item.offer is MISSING else item.offer
        chosen = item.card if isinstance(item, tool_inputs.TakeCard) else None
        if (
            completion.batch_index != index + 1
            or completion.offer_ref != offer_ref
            or completion.choice.value != item.choice
            or completion.card_ref != chosen
            or steps is None
        ):
            raise ExtractionError("The reward completion does not match its ordered source group.")
        sequences: list[JsonValue] = []
        after = completion.after
        if steps:
            if (
                completion.local_decline
                or not isinstance(completion.outcome, ActionCompleted)
                or completion.outcome.completed_steps != len(steps)
                or after is None
            ):
                raise ExtractionError("The recorded reward group was not confirmed complete.")
            gained = Counter(
                entry.definition_id
                for ref in plan.choices[index].refs
                if isinstance(entry := observation.objects.get(ref), native.CardEntry)
            )
            if sum(gained.values()) != len(plan.choices[index].refs) or any(
                _deck_count(after, identity) - _deck_count(completion.before, identity) < count
                for identity, count in gained.items()
            ):
                raise ExtractionError("The source deck does not confirm the selected reward cards.")
        elif (
            not completion.local_decline
            or completion.choice is not CardChoiceKind.SKIP
            or not isinstance(completion.outcome, ReadCompleted)
            or after != completion.before
        ):
            raise ExtractionError("The local reward decline has no valid completion proof.")
        for step in steps:
            expected: native.NativeStep
            if isinstance(step, SelectStep):
                expected = native.Select(kind=native.StepKind.SELECT, refs=step.refs)
            else:
                assert step.ref is not None
                expected = native.Interact(kind=native.StepKind.INTERACT, action=step.ref)
            settled = next(settlements, None)
            if settled is None:
                raise ExtractionError("The reward batch has missing native receipts.")
            sequence, request, reply = settled
            if (
                request.route != route
                or request.step != expected
                or reply is None
                or not isinstance(reply.outcome, Settlement)
                or not isinstance(reply.outcome.receipt, native.ReceiptCompleted)
            ):
                raise ExtractionError(
                    "The native receipts do not follow the submitted reward order."
                )
            sequences.append(sequence)
        groups.append({"offer": offer_ref, "native_sequences": sequences})
    return {"kind": "reward_batch", "stream": record.stream, "call_id": call.id, "groups": groups}


def _recorded_selection(
    journal: Journal,
    source: DecisionSource,
    call: ToolCall,
    decision: tool_inputs.RunAct,
    observation: native.Observation,
) -> dict[str, JsonValue]:
    if len(decision.steps) != 1 or not isinstance(decision.steps[0], native.Select):
        raise ExtractionError("The recorded decision is not one native selection.")
    step = decision.steps[0]
    following = journal.requests(start=source.model_sequence + 1, stop=source.model_sequence + 200)
    next_request = following[0] if following else None
    if next_request is not None:
        for message in next_request.input:
            if not isinstance(message, ResultSnapshot) or message.call_id != call.id:
                continue
            if (
                not isinstance(message.outcome, ActionCompleted)
                or not message.outcome.completed_steps
            ):
                raise ExtractionError("The recorded decision did not complete in its source run.")
            return {"kind": "tool_result", "sequence": next_request.sequence, "call_id": call.id}
    stop = next_request.sequence - 1 if next_request is not None else source.model_sequence + 200
    route = native.RouteKey.model_validate(observation.location.model_dump())
    for operation in journal.operations(start=source.model_sequence + 1, stop=stop):
        if (
            isinstance(operation.request, Settle)
            and operation.request.route == route
            and operation.request.step == step
            and operation.reply is not None
            and isinstance(operation.reply.outcome, Settlement)
            and isinstance(operation.reply.outcome.receipt, native.ReceiptCompleted)
        ):
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
            prepared: PreparedRequest | None,
        ) -> ModelReply:
            raise _Captured(input, tools)

        def model(role: ModelPurpose, stream: str) -> ModelClient:
            client = _SourceModel(timeline, role.owner, stream, target, reached, capture)
            client.audit = evidence
            models.append(client)
            return client

        documents = SourceDocuments(timeline, source_documents)
        dependencies = SessionDependencies(
            ReplayGame(timeline),
            ReplayPrompts(timeline),
            documents,
            model,
        )
        viewer = any(
            isinstance(record.request, PromptRead) and record.request.name == "viewer-input"
            for record in journal.materials(stop=source.model_sequence)
        )
        captured_input: ModelInput | None = None
        captured_tools: tuple[ToolDefinition, ...] = ()
        session = _SourceSession(
            journal.manifest, dependencies, user_input=UserInputQueue() if viewer else None
        )
        try:
            async with session:
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
        if record.stream.startswith(f"{ModelPurpose.DECK.value}:"):
            plan = reconcile_plan(
                session.run_role.route_plan, last_task.observation.state.progress.position
            ).plan
            if plan is not None:
                route = RouteState(
                    position=plan.position,
                    edges=tuple((edge.source, edge.destination) for edge in plan.edges),
                )
                state = last_task.state.model_copy(update={"route": route})
                task = last_task.model_copy(update={"state": state})
                events = tuple(task if event is last_task else event for event in events)
        reply = record.attempts[-1].reply
        if reply is None:
            raise ExtractionError("The source decision has no recorded reply.")
        calls = AssistantMessage(reply.native_reply).tool_calls
        actions = tuple(
            call
            for call in calls
            if call.name
            in (
                ToolName.RUN_DECK_CHOICE,
                ToolName.RUN_ACT,
                ToolName.RUN_NEXT,
                ToolName.RUN_PLAN,
            )
        )
        if len(actions) != 1 or actions[0].name not in (
            ToolName.RUN_DECK_CHOICE,
            ToolName.RUN_ACT,
        ):
            raise ExtractionError("The source reply must contain one labeled game decision.")
        action = actions[0]
        decision = _source_decision(action, captured_tools)
        evidence.recorded_completion = (
            _recorded_reward_batch(journal, source, action, decision, last_task)
            if isinstance(decision, tool_inputs.RunDeckChoice)
            else _recorded_selection(journal, source, action, decision, last_task.observation)
        )
        groups = reward_groups_for(decision, last_task.observation)
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
            events=events,
            native_reads=tuple(reads),
            decision_tool=action.name,
            decision_offer=(groups[0].offer if len(groups) == 1 else None),
            recorded_choices=choices_for(decision, last_task.observation),
            recorded_reason=(
                "\n".join(group.reason for group in groups)
                if groups
                else decision.reason
                if isinstance(decision, tool_inputs.RunAct) and decision.reason is not MISSING
                else ""
            ),
            recorded_reward_groups=groups,
        )
