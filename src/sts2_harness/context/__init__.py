"""One synchronous owner of a role's conversation and delivered observations."""

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import overload

from pydantic import MISSING, JsonValue, TypeAdapter

from sts2_harness.client import models as native
from sts2_harness.context.entries import Collection, apply_entries, ordered
from sts2_harness.context.models import (
    ActContent,
    AutomaticProgress,
    CombatActInput,
    CombatReviewInput,
    HandoffContent,
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
from sts2_harness.context.run_models import RunObservation
from sts2_harness.context.run_projection import RunDelivery
from sts2_harness.context.view import visible_observation
from sts2_harness.knowledge import DocumentView, GuidanceContent, KnowledgeLookup
from sts2_harness.llm.messages import (
    AssistantMessage,
    ModelInput,
    TextMessage,
    TextRole,
    ToolCall,
    ToolResultMessage,
)
from sts2_harness.results import (
    ActionToolResult,
    QueryToolResult,
    ReadCompleted,
    ToolResult,
)
from sts2_harness.route_plan import RoutePlan


@dataclass(frozen=True, slots=True)
class _Group:
    id: int
    start: int
    observation: dict[str, JsonValue]
    catalog: dict[str, JsonValue]
    notes: str
    guidance: tuple[DocumentView, ...]
    route_plan: JsonValue


def _profile_references(value: JsonValue) -> set[str]:
    references: set[str] = set()
    if isinstance(value, list):
        for item in value:
            references.update(_profile_references(item))
        return references
    if not isinstance(value, dict):
        return references
    profile = value.get("profile")
    if isinstance(profile, str):
        references.add(profile)
    for child in value.values():
        references.update(_profile_references(child))
    return references


def _rename_profiles(value: JsonValue, names: dict[str, str]) -> JsonValue:
    if isinstance(value, list):
        return [_rename_profiles(item, names) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: names.get(child, child)
        if key == "profile" and isinstance(child, str)
        else _rename_profiles(child, names)
        for key, child in value.items()
    }


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


def repeats_action_result(
    role: native.DecisionOwner,
    last: RunResultMessage,
    task: TaskInput,
    observation: native.Observation,
) -> bool:
    if isinstance(task, CombatActInput):
        ordinary = task.opening_notes is MISSING
    elif isinstance(task, RunActInput):
        ordinary = task.strategy_notes is MISSING and task.handoff is None
    else:
        return False
    return (
        ordinary
        and isinstance(last.source, ActionToolResult)
        and last.source.observation is not None
        and same_observation(role, last.source.observation, observation)
    )


class Context:
    def __init__(self, role: native.DecisionOwner, *, system: tuple[TextMessage, ...] = ()) -> None:
        self._role = role
        self._history: list[TextMessage | AssistantMessage | ToolResultMessage] = list(system)
        self._system = system
        self._combat_delivery = RunDelivery(role=native.DecisionOwner.COMBAT)
        self._run_delivery = RunDelivery()
        self._documents: dict[tuple[str, str | None], DocumentView] = {}
        self._guidance: dict[tuple[str, str | None], DocumentView] = {}
        self._last_group: _Group | None = None
        self._active_group: _Group | None = None
        self._group_number = 0
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

    @property
    def group_ids(self) -> tuple[int | None, int | None]:
        return (
            self._last_group.id if self._last_group else None,
            self._active_group.id if self._active_group else None,
        )

    def begin_group(
        self, observation: native.Observation, notes: str, route_plan: JsonValue = None
    ) -> None:
        if self._active_group is not None:
            return
        self._group_number += 1
        delivery = deepcopy(self._delivery)
        known = set(delivery.catalog.values())
        projected = delivery.project(visible_observation(observation, self._role), observation)
        names = {
            ident: f"baseline{self._group_number}.{ident}"
            for ident in delivery.catalog.values()
            if ident not in known
        }
        wire = _JSON_OBJECT.validate_json(projected.to_json())
        wire.pop("catalog_additions", None)
        if delivery.scene is not None:
            wire["scene"] = delivery.scene
        if delivery.current:
            wire["current"] = dict(delivery.current)
        renamed = _rename_profiles(wire, names)
        assert isinstance(renamed, dict)
        baseline_references = _profile_references(renamed)
        catalog: dict[str, JsonValue] = {
            names.get(ident, ident): _JSON_OBJECT.validate_json(key)
            for key, ident in delivery.catalog.items()
            if names.get(ident, ident) in baseline_references
        }
        self._active_group = _Group(
            self._group_number,
            len(self._history),
            renamed,
            catalog,
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
        references = _profile_references(group.observation)
        additions: set[str] = set()
        for message in retained:
            projected = _message_observation(message)
            if projected is None:
                continue
            content = _JSON_OBJECT.validate_json(projected.to_json())
            references.update(_profile_references(content))
            entries = content.get("catalog_additions")
            if isinstance(entries, dict):
                additions.update(entries)
        needed = _profile_references(group.observation) | (references - additions)
        catalog: dict[str, JsonValue] = {
            ident: _JSON_OBJECT.validate_json(key)
            for key, ident in self._delivery.catalog.items()
            if ident in needed
        }
        catalog.update(group.catalog)
        profiles: dict[str, JsonValue] = {
            key: value for key, value in catalog.items() if key in needed
        }
        observation = dict(group.observation)
        if profiles:
            observation["catalog_additions"] = profiles
        values: dict[str, JsonValue] = {"observation": observation}
        if self._role is native.DecisionOwner.RUN:
            values.update(strategy_notes=group.notes, route_plan=group.route_plan)
        else:
            values["opening_notes"] = group.notes
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
        baseline_message = TextMessage(
            TextRole.USER, json.dumps(baseline, ensure_ascii=False, separators=(",", ":"))
        )
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
        if not isinstance(baseline_message, TextMessage):
            raise ValueError("The compaction baseline is missing.")
        envelope = _JSON_OBJECT.validate_json(baseline_message.text)
        baseline = envelope.get("context_baseline")
        if not isinstance(baseline, dict):
            raise ValueError("The compaction baseline is invalid.")
        initial = baseline.get("observation")
        if not isinstance(initial, dict):
            raise ValueError("The compaction observation is invalid.")
        RunObservation.model_validate_json(json.dumps(initial, ensure_ascii=False))
        catalog: dict[str, str] = {}
        known: set[str] = set()

        def receive(observation: dict[str, JsonValue]) -> None:
            entries = observation.get("catalog_additions")
            if isinstance(entries, dict):
                for ident, profile in entries.items():
                    key = json.dumps(profile, sort_keys=True, ensure_ascii=False)
                    catalog[key] = ident
                    known.add(ident)
            if not _profile_references(observation) <= known:
                raise ValueError("A retained profile reference has no definition.")

        reconstructed_scene: dict[str, JsonValue] | None = None
        reconstructed_current: dict[str, JsonValue] = {}

        def apply(observation: dict[str, JsonValue]) -> None:
            nonlocal reconstructed_scene, reconstructed_current
            receive(observation)
            new_scene = observation.get("scene")
            if isinstance(new_scene, dict):
                node_changed = reconstructed_scene is None or any(
                    new_scene.get(key) != reconstructed_scene.get(key)
                    for key in ("game_instance_id", "run_id", "node_id")
                )
                if node_changed:
                    updates = observation.get("current")
                    old_progress = reconstructed_current.get("progress")
                    new_progress = updates.get("progress") if isinstance(updates, dict) else None
                    same_act = (
                        reconstructed_scene is not None
                        and all(
                            new_scene.get(key) == reconstructed_scene.get(key)
                            for key in ("game_instance_id", "run_id")
                        )
                        and isinstance(old_progress, dict)
                        and isinstance(new_progress, dict)
                        and old_progress.get("act") == new_progress.get("act")
                    )
                    reconstructed_current = {
                        key: value
                        for key, value in reconstructed_current.items()
                        if key == "map_topology" and same_act
                    }
                previous = (
                    reconstructed_scene["bindings"]
                    if not node_changed and reconstructed_scene is not None
                    else []
                )
                reconstructed_scene = {
                    key: value for key, value in new_scene.items() if key != "removed"
                }
                reconstructed_scene["bindings"] = apply_entries(
                    Collection.BINDINGS,
                    previous,
                    new_scene["bindings"],
                    new_scene.get("removed", []),
                )
            updates = observation.get("current")
            if not isinstance(updates, dict):
                return
            decision = observation.get("decision")
            combat = isinstance(decision, dict) and decision.get("phase") == native.Phase.COMBAT
            for key, value in updates.items():
                if key == "removed" or key in Collection:
                    continue
                if value is None and not (key == "turn" and combat):
                    reconstructed_current.pop(key, None)
                else:
                    reconstructed_current[key] = value
            removals = updates.get("removed", {})
            assert isinstance(removals, dict)
            for collection in Collection:
                if collection in updates or collection in removals:
                    reconstructed_current[collection] = apply_entries(
                        collection,
                        reconstructed_current.get(collection, []),
                        updates.get(collection, []),
                        removals.get(collection, []),
                    )

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
        renamed = {
            old: catalog[key] for key, old in self._delivery.catalog.items() if key in catalog
        }
        scene = _rename_profiles(self._delivery.scene, renamed)
        current = _rename_profiles(self._delivery.current, renamed)
        if scene is not None and not isinstance(scene, dict):
            raise ValueError("The current scene is invalid.")
        if not isinstance(current, dict):
            raise ValueError("The current state is invalid.")
        for collection in Collection:
            if collection in current:
                current[collection] = ordered(collection, current[collection])
        if (
            has_retained_observation
            and not (_profile_references(scene) | _profile_references(current)) <= known
        ):
            raise ValueError("The current state has an undelivered profile.")
        if has_retained_observation and (
            reconstructed_scene != scene or reconstructed_current != current
        ):
            raise ValueError("The retained observations do not reproduce the current state.")
        if not install:
            return
        self._delivery.catalog = catalog
        self._delivery.scene = reconstructed_scene
        self._delivery.current = reconstructed_current
        if not has_retained_observation:
            decision = initial.get("decision")
            if isinstance(decision, dict) and isinstance(decision.get("phase"), str):
                self._delivery.phase = native.Phase(decision["phase"])
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
            if self._history and isinstance(last := self._history[-1], TaskMessage):
                if last.task == task:
                    return
            content: TaskContent = (
                ReviewContent(battle_report=task.battle_report)
                if isinstance(task, CombatReviewInput)
                else HandoffContent(
                    battle_report=task.handoff.report,
                    combat_review=task.handoff.review,
                    strategy_notes=task.strategy_notes,
                )
            )
            self._history.append(TaskMessage(TextRole.USER, content.to_json(), content, task, None))
            return
        if observation is None:
            raise TypeError("An act task requires a current observation.")
        if self._history:
            last = self._history[-1]
            if isinstance(last, TaskMessage) and last.task == task and last.observation is not None:
                if same_observation(self._role, last.observation, observation):
                    return
            if isinstance(last, RunResultMessage) and repeats_action_result(
                self._role, last, task, observation
            ):
                return
        if isinstance(task, CombatActInput):
            projected = self._combat_delivery.project(
                visible_observation(observation, self._role), observation
            )
            content = ActContent(observation=projected, opening_notes=task.opening_notes)
        else:
            run_projected = self._run_delivery.project(
                visible_observation(observation, self._role), observation
            )
            if task.handoff is not None:
                content = RunActHandoffContent(
                    observation=run_projected,
                    strategy_notes=task.strategy_notes,
                    battle_report=task.handoff.report,
                    combat_review=task.handoff.review,
                )
            else:
                content = RunActContent(
                    observation=run_projected, strategy_notes=task.strategy_notes
                )
        message = TaskMessage(TextRole.USER, content.to_json(), content, task, observation)
        self._history.append(message)

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
            route_plan_status="invalidated" if invalidated else MISSING,
            outcome=result.outcome,
            observation=observation,
        )
        self._history.append(RunAutomaticMessage(TextRole.USER, content.to_json(), content, result))

    def append_tool_result(self, call: ToolCall, result: ToolResult) -> None:
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
        content = RunProjectedResult(
            outcome=result.outcome,
            observation=observation,
            data=result.data if isinstance(result, QueryToolResult) else MISSING,
        )
        self._history.append(RunResultMessage(call.id, content, result))
        if isinstance(result, QueryToolResult):
            self._record_query_documents(result)

    def _record_query_documents(self, result: QueryToolResult) -> None:
        data = result.data
        if isinstance(data, DocumentView):
            self._record_documents((data,))
        elif isinstance(data, KnowledgeLookup):
            for entry in data.entries:
                self._record_documents(entry.supplements)
