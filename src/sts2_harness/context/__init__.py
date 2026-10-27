"""One synchronous owner of a role's conversation and delivered observations."""

from typing import overload

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.context.models import (
    ActContent,
    ActTask,
    DecisionTask,
    ProjectedResult,
    ResultMessage,
    ReviewContent,
    ReviewTask,
    ShortState,
    TaskMessage,
    Updates,
)
from sts2_harness.context.projection import Delivery
from sts2_harness.context.results import (
    ActionToolResult,
    QueryToolResult,
    ReadCompleted,
    ToolResult,
)
from sts2_harness.context.view import visible_observation
from sts2_harness.llm.messages import (
    AssistantMessage,
    ModelInput,
    TextMessage,
    TextRole,
    ToolCall,
    ToolResultMessage,
)

__all__ = [
    "ActTask",
    "ReviewTask",
    "Context",
    "QueryToolResult",
    "ReadCompleted",
    "ActionToolResult",
]


class Context:
    def __init__(self, role: native.DecisionOwner, *, system: tuple[TextMessage, ...] = ()) -> None:
        self._role = role
        self._history: list[TextMessage | AssistantMessage | ToolResultMessage] = list(system)
        self._delivery = Delivery.empty()

    def model_input(self) -> ModelInput:
        return tuple(self._history)

    @overload
    def append_task(self, task: ActTask, observation: native.Observation) -> None: ...

    @overload
    def append_task(self, task: ReviewTask) -> None: ...

    def append_task(
        self, task: DecisionTask, observation: native.Observation | None = None
    ) -> None:
        """Append approved Combat tasks; Run message assembly has its own contract."""
        if self._role is not native.DecisionOwner.COMBAT:
            raise ValueError("Combat tasks require a Combat context.")
        if isinstance(task, ReviewTask):
            if self._history and isinstance(last := self._history[-1], TaskMessage):
                if last.task == task:
                    return
            review = ReviewContent(battle_report=task.battle_report)
            self._history.append(TaskMessage(TextRole.USER, review.to_json(), review, task, None))
            return
        if observation is None:
            raise TypeError("An act task requires a current observation.")
        if self._history:
            last = self._history[-1]
            if isinstance(last, TaskMessage) and last.task == task and last.observation is not None:
                if self._same_observation(last.observation, observation):
                    return
            if (
                isinstance(last, ResultMessage)
                and isinstance(last.source, ActionToolResult)
                and task.opening_notes is MISSING
                and last.source.observation is not None
                and self._same_observation(last.source.observation, observation)
            ):
                return
        projected, delivered = self._delivery.project(
            visible_observation(observation, self._role), observation.location
        )
        content = ActContent(
            observation=projected,
            opening_notes=task.opening_notes,
        )
        message = TaskMessage(TextRole.USER, content.to_json(), content, task, observation)
        self._history.append(message)
        self._delivery = delivered

    def _same_observation(self, left: native.Observation, right: native.Observation) -> bool:
        return left.location == right.location and visible_observation(
            left, self._role
        ) == visible_observation(right, self._role)

    def append_reply(self, reply: AssistantMessage) -> None:
        self._history.append(reply)

    def append_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        delivered = self._delivery
        state: ShortState | None = None
        updates = Updates()
        if result.observation is not None:
            projected, delivered = self._delivery.project(
                visible_observation(result.observation, self._role), result.observation.location
            )
            state = projected.state
            updates = projected.updates
        content = ProjectedResult(
            outcome=result.outcome,
            state=state,
            updates=updates,
            data=result.data if isinstance(result, QueryToolResult) else MISSING,
        )
        message = ResultMessage(call.id, content.to_json(), content, result)
        self._history.append(message)
        self._delivery = delivered
