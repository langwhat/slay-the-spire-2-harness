"""One synchronous owner of a role's conversation and delivered observations."""

from typing import overload

from pydantic import MISSING

from sts2_harness.client import models as native
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
        self._combat_delivery = RunDelivery(role=native.DecisionOwner.COMBAT)
        self._run_delivery = RunDelivery()
        self._documents: dict[tuple[str, str | None], DocumentView] = {}
        for message in system:
            if isinstance(message.value, GuidanceContent):
                self._record_documents(message.value.guidance)

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
        if not changed:
            return
        self._history.append(TextMessage(TextRole.USER, GuidanceContent(guidance=changed)))
        self._record_documents(changed)

    def model_input(self) -> ModelInput:
        return tuple(self._history)

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

    def append_automatic(self, progress: AutomaticProgress, result: ActionToolResult) -> None:
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
            automatic_progress=progress, outcome=result.outcome, observation=observation
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
