"""One synchronous owner of a role's conversation and delivered observations."""

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import overload

from pydantic import MISSING, JsonValue, TypeAdapter

from sts2_harness.client import models as native
from sts2_harness.context.models import (
    ActContent,
    AutomaticProgress,
    BaselineMessage,
    CombatActInput,
    CombatReviewInput,
    HandoffContent,
    PreviousBattle,
    ReviewContent,
    RunActContent,
    RunActHandoffContent,
    RunActInput,
    RunAutomaticContent,
    RunAutomaticMessage,
    RunHandoffInput,
    RunProjectedResult,
    RunResultMessage,
    TaskContent,
    TaskInput,
    TaskMessage,
)
from sts2_harness.context.rendering import render
from sts2_harness.context.run_models import RunObservation
from sts2_harness.context.run_projection import RunDelivery, definition_references, mechanism_key
from sts2_harness.context.view import visible_observation
from sts2_harness.decisions import CombatHandoff
from sts2_harness.fights import Fight
from sts2_harness.knowledge import DocumentView, GuidanceContent, KnowledgeLookup
from sts2_harness.llm.messages import (
    AssistantMessage,
    ModelInput,
    TextMessage,
    TextRole,
    ToolCall,
    ToolResultMessage,
)
from sts2_harness.note_values import NotesView
from sts2_harness.results import (
    ActionToolResult,
    QueryToolResult,
    ReadCompleted,
    ToolResult,
)
from sts2_harness.route_plan import MovementStatus, RoutePlan
from sts2_harness.user_input_values import UserInput

_VIEWER_PREFIX = "[viewer] "
_VIEWER_TEXT_LIMIT = 50_000


@dataclass(frozen=True, slots=True)
class _Group:
    id: int
    start: int
    observation: dict[str, JsonValue]
    notes: NotesView
    guidance: tuple[DocumentView, ...]
    route_plan: JsonValue


_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _message_observation(
    message: TextMessage | AssistantMessage | ToolResultMessage,
) -> RunObservation | None:
    if isinstance(message, TaskMessage) and isinstance(
        message.content, (RunActContent, ActContent)
    ):
        return message.content.observation
    if isinstance(message, (RunResultMessage, RunAutomaticMessage)):
        return message.content.observation
    return None


__all__ = [
    "CombatActInput",
    "CombatReviewInput",
    "Context",
    "QueryToolResult",
    "ReadCompleted",
    "ActionToolResult",
]


def same_observation(
    role: native.DecisionOwner, left: native.Observation, right: native.Observation
) -> bool:
    return (
        left.game_language == right.game_language
        and left.location == right.location
        and visible_observation(left, role) == visible_observation(right, role)
    )


class Context:
    def __init__(
        self,
        role: native.DecisionOwner,
        *,
        system: tuple[TextMessage, ...] = (),
        minimal: bool = False,
    ) -> None:
        self._role = role
        self._minimal = minimal
        self._history: list[TextMessage | AssistantMessage | ToolResultMessage] = list(system)
        self._system = system
        self._combat_delivery = RunDelivery(role=native.DecisionOwner.COMBAT)
        self._run_delivery = RunDelivery()
        self._documents: dict[tuple[str, str | None], DocumentView] = {}
        self._guidance: dict[tuple[str, str | None], DocumentView] = {}
        self._last_group: _Group | None = None
        self._active_group: _Group | None = None
        self._group_number = 0
        self._previous_battle: CombatHandoff | None = None
        for message in system:
            if isinstance(message.value, GuidanceContent):
                self._record_documents(message.value.guidance)
                self._record_guidance(message.value.guidance)

    def _record_guidance(self, documents: tuple[DocumentView, ...]) -> None:
        for document in documents:
            key = (document.id, None if document.resource is MISSING else document.resource)
            self._guidance[key] = document

    def _record_documents(self, documents: tuple[DocumentView, ...]) -> None:
        for document in documents:
            key = (document.id, None if document.resource is MISSING else document.resource)
            self._documents[key] = document

    def append_materials(self, documents: tuple[DocumentView, ...]) -> None:
        candidates = {
            (document.id, None if document.resource is MISSING else document.resource): document
            for document in documents
        }
        changed = tuple(
            document for key, document in candidates.items() if self._documents.get(key) != document
        )
        self._record_guidance(documents)
        if not changed:
            return
        self._history.append(TextMessage(TextRole.USER, GuidanceContent(guidance=changed)))
        self._record_documents(changed)
        self._record_guidance(changed)

    @property
    def _delivery(self) -> RunDelivery:
        return (
            self._run_delivery if self._role is native.DecisionOwner.RUN else self._combat_delivery
        )

    def model_input(self) -> ModelInput:
        return tuple(self._history)

    @property
    def has_active_group(self) -> bool:
        return self._active_group is not None

    def set_route_plan(self, plan: RoutePlan | None) -> None:
        self._run_delivery.route_plan = plan
        self._combat_delivery.route_plan = plan

    def set_fights(self, fights: tuple[Fight, ...]) -> None:
        self._run_delivery.fights = fights

    def set_deck_stats(self, stats: dict[str, JsonValue], use: dict[str, int]) -> None:
        self._run_delivery.deck_stats = stats
        self._run_delivery.deck_use = use

    def set_notes(self, notes: NotesView) -> None:
        if not self._minimal:
            self._run_delivery.notes = notes

    @property
    def group_ids(self) -> tuple[int | None, int | None]:
        return (
            self._last_group.id if self._last_group else None,
            self._active_group.id if self._active_group else None,
        )

    def begin_group(
        self, observation: native.Observation, notes: NotesView, route_plan: JsonValue = None
    ) -> None:
        if self._active_group is not None:
            return
        self._group_number += 1
        delivery = deepcopy(self._delivery)
        if not delivery.sections:
            delivery.project(visible_observation(observation, self._role), observation)
        self._active_group = _Group(
            self._group_number,
            len(self._history),
            deepcopy(delivery.snapshot()),
            notes,
            tuple(self._guidance.values()),
            deepcopy(route_plan),
        )

    def finish_group(self) -> None:
        if self._active_group is None:
            raise RuntimeError("No interaction group is active.")
        self._last_group = self._active_group
        self._active_group = None

    def compaction_candidate(self) -> ModelInput:
        return self.compaction_input()[0]

    def compaction_input(self) -> tuple[ModelInput, TextMessage]:
        group = self._last_group or self._active_group
        if group is None:
            raise RuntimeError("No interaction group exists.")
        retained = tuple(self._history[group.start :])
        observation = deepcopy(group.observation)
        # Definitions needed by retained changes may predate this group's baseline.
        definitions: dict[str, JsonValue] = {}
        initial = observation.get("defs", [])
        assert isinstance(initial, list)
        for definition in initial:
            assert isinstance(definition, dict) and isinstance(definition["id"], str)
            definitions[definition["id"]] = definition
        for message in retained:
            projected = _message_observation(message)
            if projected is None:
                continue
            wire = _JSON_OBJECT.validate_json(projected.to_json())
            for key in definition_references(wire):
                if key in self._delivery.definitions:
                    definitions.setdefault(key, self._delivery.definitions[key])
        observation["defs"] = list(definitions.values())
        # A currently visible rule may have last appeared before the retained baseline.
        rules = observation.get("mechanisms", [])
        assert isinstance(rules, list)
        mechanisms: dict[tuple[str, str, str | None], JsonValue] = {}
        for rule in (*rules, *self._delivery.mechanisms):
            assert isinstance(rule, dict)
            mechanisms[mechanism_key(rule)] = rule
        if mechanisms:
            observation["mechanisms"] = list(mechanisms.values())
        values: dict[str, JsonValue] = {"observation": observation}
        if self._role is native.DecisionOwner.RUN:
            if self._previous_battle is not None:
                battle = PreviousBattle(
                    battle_report=self._previous_battle.report,
                    combat_review=self._previous_battle.review,
                )
                retained_battles = (
                    PreviousBattle(
                        battle_report=message.content.battle_report,
                        combat_review=message.content.combat_review,
                    )
                    if isinstance(message.content, (RunActHandoffContent, HandoffContent))
                    else message.content.previous_battle
                    for message in retained
                    if isinstance(message, TaskMessage)
                    and isinstance(message.content, (RunActContent, HandoffContent))
                )
                if battle not in retained_battles:
                    values["previous_battle"] = _JSON_OBJECT.validate_json(battle.to_json())
            current_map = observation.get("map")
            if not isinstance(current_map, dict):
                values["route_plan"] = group.route_plan
        elif not self._minimal:
            if not any(
                isinstance(message, TaskMessage)
                and isinstance(message.content, ActContent)
                and message.content.opening_notes is not MISSING
                for message in retained
            ):
                values["opening_notes"] = _JSON_OBJECT.validate_json(group.notes.to_json())
        baseline: dict[str, JsonValue] = {"context_baseline": values}
        system_guidance = {
            (document.id, None if document.resource is MISSING else document.resource): document
            for message in self._system
            if isinstance(message.value, GuidanceContent)
            for document in message.value.guidance
        }
        guidance = tuple(
            document
            for document in group.guidance
            if system_guidance.get(
                (document.id, None if document.resource is MISSING else document.resource)
            )
            != document
        )
        prefix = (
            (TextMessage(TextRole.USER, GuidanceContent(guidance=guidance)),) if guidance else ()
        )
        baseline_message = BaselineMessage(TextRole.USER, render(baseline), baseline)
        candidate: ModelInput = (*self._system, *prefix, baseline_message, *retained)
        return candidate, baseline_message

    def validate_compaction(self, candidate: ModelInput) -> None:
        self._check_compaction(candidate, install=False)

    def install_compaction(self, candidate: ModelInput) -> None:
        self._check_compaction(candidate, install=True)

    def _check_compaction(self, candidate: ModelInput, *, install: bool) -> None:
        if candidate != self.compaction_candidate():
            raise ValueError("The compaction candidate changed before installation.")
        group = self._last_group or self._active_group
        assert group is not None
        retained = tuple(self._history[group.start :])
        prefix_length = len(candidate) - len(retained)
        baseline_message = candidate[prefix_length - 1]
        if not isinstance(baseline_message, BaselineMessage):
            raise ValueError("The compaction baseline is missing.")
        envelope = baseline_message.content
        baseline = envelope.get("context_baseline")
        if not isinstance(baseline, dict):
            raise ValueError("The compaction baseline is invalid.")
        initial = baseline.get("observation")
        if not isinstance(initial, dict):
            raise ValueError("The compaction observation is invalid.")
        RunObservation.model_validate_json(json.dumps(initial, ensure_ascii=False))
        definitions: dict[str, dict[str, JsonValue]] = {}
        sections: dict[str, JsonValue] = {}
        mechanisms: set[tuple[str, str, str | None]] = set()

        def apply(observation: dict[str, JsonValue]) -> None:
            if observation.get("baseline") is True:
                sections.clear()
                mechanisms.clear()
            rules = observation.get("mechanisms", [])
            assert isinstance(rules, list)
            for rule in rules:
                assert isinstance(rule, dict)
                mechanisms.add(mechanism_key(rule))
            entries = observation.get("defs", [])
            assert isinstance(entries, list)
            for entry in entries:
                assert isinstance(entry, dict) and isinstance(entry["id"], str)
                definitions[entry["id"]] = entry
            for name, value in observation.items():
                if name not in ("baseline", "defs", "mechanisms"):
                    if value is None:
                        sections.pop(name, None)
                    else:
                        sections[name] = value
            missing = (
                definition_references(observation)
                - definitions.keys()
                - self._delivery.inline_card_ids
            )
            if missing:
                raise ValueError("A retained definition reference has no definition.")

        apply(initial)
        pending: list[str] = []
        has_retained_observation = False
        for message in retained:
            if isinstance(message, AssistantMessage):
                if pending:
                    raise ValueError("A retained tool call has no result.")
                pending.extend(call.id for call in message.tool_calls)
            elif isinstance(message, ToolResultMessage):
                if not pending or pending.pop(0) != message.call_id:
                    raise ValueError("A retained tool result has no matching call.")
            observation = _message_observation(message)
            if observation is not None:
                has_retained_observation = True
                apply(_JSON_OBJECT.validate_json(observation.to_json()))
        if pending:
            raise ValueError("A retained tool call has no result.")
        expected = {
            name: value for name, value in self._delivery.sections.items() if value is not None
        }
        if has_retained_observation and sections != expected:
            raise ValueError("The retained sections do not reproduce the current state.")
        if not install:
            return
        self._delivery.definitions = definitions
        self._delivery.delivered_defs = set(definitions)
        self._delivery.delivered_mechanisms = mechanisms
        self._delivery.sections = self._delivery.sections if has_retained_observation else sections
        self._history = list(candidate)
        if self._last_group is not None:
            self._last_group = replace(
                self._last_group, start=prefix_length + self._last_group.start - group.start
            )
        if self._active_group is not None:
            self._active_group = replace(
                self._active_group, start=prefix_length + self._active_group.start - group.start
            )
        self._documents = {}
        for message in candidate:
            if isinstance(message, TextMessage) and isinstance(message.value, GuidanceContent):
                self._record_documents(message.value.guidance)
            elif isinstance(message, RunResultMessage) and isinstance(
                message.source, QueryToolResult
            ):
                self._record_query_documents(message.source)

    @overload
    def append_task(self, task: CombatActInput, observation: native.Observation) -> None: ...

    @overload
    def append_task(self, task: CombatReviewInput) -> None: ...

    @overload
    def append_task(self, task: RunActInput, observation: native.Observation) -> None: ...

    @overload
    def append_task(self, task: RunHandoffInput) -> None: ...

    def append_task(self, task: TaskInput, observation: native.Observation | None = None) -> None:
        """Append a complete task and install only the observation actually delivered."""
        combat = isinstance(task, (CombatActInput, CombatReviewInput))
        if combat != (self._role is native.DecisionOwner.COMBAT):
            raise ValueError("The task requires its corresponding role context.")
        if isinstance(task, (CombatReviewInput, RunHandoffInput)):
            if isinstance(last := self._last_entry(), TaskMessage) and last.task == task:
                return
            content: TaskContent = (
                ReviewContent(battle_report=task.battle_report)
                if isinstance(task, CombatReviewInput)
                else HandoffContent(
                    battle_report=task.handoff.report,
                    combat_review=task.handoff.review,
                    notes=task.strategy_notes,
                )
            )
            if isinstance(task, RunHandoffInput):
                self._previous_battle = task.handoff
            self._history.append(
                TaskMessage(
                    TextRole.USER,
                    render(_JSON_OBJECT.validate_json(content.to_json())),
                    content,
                    task,
                    None,
                )
            )
            return
        if observation is None:
            raise TypeError("An act task requires a current observation.")
        if (last := self._last_entry()) is not None:
            if isinstance(last, TaskMessage) and last.task == task and last.observation is not None:
                if same_observation(self._role, last.observation, observation):
                    return
        if isinstance(task, RunActInput) and task.strategy_notes is not MISSING:
            self.set_notes(task.strategy_notes)
        projected = self._delivery.project(
            visible_observation(observation, self._role), observation
        )
        if isinstance(task, CombatActInput):
            content = ActContent(observation=projected, opening_notes=task.opening_notes)
        else:
            if task.handoff is not None and task.handoff != self._previous_battle:
                content = RunActHandoffContent(
                    observation=projected,
                    battle_report=task.handoff.report,
                    combat_review=task.handoff.review,
                )
            else:
                previous = task.previous_battle
                content = RunActContent(
                    observation=projected,
                    previous_battle=(
                        PreviousBattle(battle_report=previous.report, combat_review=previous.review)
                        if previous is not None and previous != self._previous_battle
                        else MISSING
                    ),
                )
            self._previous_battle = task.handoff or task.previous_battle or self._previous_battle
        message = TaskMessage(
            TextRole.USER,
            render(_JSON_OBJECT.validate_json(content.to_json())),
            content,
            task,
            observation,
        )
        self._history.append(message)

    def _last_entry(self) -> TextMessage | AssistantMessage | ToolResultMessage | None:
        # Viewer input can sit between a task and its retry without changing what the task repeats.
        for entry in reversed(self._history):
            if not (isinstance(entry, TextMessage) and entry.text.startswith(_VIEWER_PREFIX)):
                return entry
        return None

    def append_user_input(self, inputs: tuple[UserInput, ...]) -> None:
        # One line per input, so a viewer's line break cannot pass for anything but their own words.
        lines = (f"{_VIEWER_PREFIX}{' '.join(item.text.split())}" for item in inputs)
        # The limit keeps one delivery from exhausting a window that compaction could not recover.
        text = "\n".join(lines)[:_VIEWER_TEXT_LIMIT]
        self._history.append(TextMessage(TextRole.USER, text))

    def append_correction(self, message: str) -> None:
        self._history.append(TextMessage(TextRole.USER, message))

    def append_reply(self, reply: AssistantMessage) -> None:
        self._history.append(reply)

    def append_automatic(
        self, progress: AutomaticProgress, result: ActionToolResult, *, invalidated: bool = False
    ) -> None:
        delivery = (
            self._run_delivery if self._role is native.DecisionOwner.RUN else self._combat_delivery
        )
        observation = (
            delivery.project(
                visible_observation(result.observation, self._role), result.observation
            )
            if result.observation is not None
            else None
        )
        content = RunAutomaticContent(
            automatic_progress=progress,
            route_plan_status=MovementStatus.INVALIDATED if invalidated else MISSING,
            outcome=result.outcome,
            observation=observation,
        )
        self._history.append(
            RunAutomaticMessage(
                TextRole.USER,
                render(_JSON_OBJECT.validate_json(content.to_json())),
                content,
                result,
            )
        )

    def append_tool_result(
        self, call: ToolCall, result: ToolResult, *, defer_observation: bool = False
    ) -> None:
        delivery = (
            self._run_delivery if self._role is native.DecisionOwner.RUN else self._combat_delivery
        )
        observation = (
            delivery.project(
                visible_observation(result.observation, self._role), result.observation
            )
            if result.observation is not None
            and not (defer_observation and result.observation.location.owner is self._role)
            else None
        )
        content = RunProjectedResult(
            outcome=result.outcome,
            observation=observation,
            data=result.data if isinstance(result, QueryToolResult) else MISSING,
        )
        self._history.append(
            RunResultMessage(
                call_id=call.id,
                content=content,
                text=render(_JSON_OBJECT.validate_json(content.to_json())),
                source=result,
            )
        )
        if isinstance(result, QueryToolResult):
            self._record_query_documents(result)

    def _record_query_documents(self, result: QueryToolResult) -> None:
        data = result.data
        if isinstance(data, DocumentView):
            self._record_documents((data,))
        elif isinstance(data, KnowledgeLookup):
            for entry in data.entries:
                self._record_documents(entry.supplements)
