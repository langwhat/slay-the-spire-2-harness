"""A shared model decision loop; execution owns submitted game effects."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.decisions import (
    ActionDecision,
    ActionProposal,
    ActTask,
    Decision,
    DecisionTask,
    TaskComplete,
)
from sts2_harness.execution.recovery import DecisionInterrupted, ModelFailure, ToolRecoveryBudget
from sts2_harness.game import GameLifetimeEnded
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.messages import ModelClient, ModelUnavailable, ToolCall
from sts2_harness.material_text import MaterialUnavailable
from sts2_harness.results import (
    ActionToolResult,
    Failed,
    QueryToolResult,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
    WriteUnknown,
)
from sts2_harness.tools import ToolCatalog

type MaterialReader = Callable[[native.Observation], Awaitable[tuple[DocumentView, ...]]]


class Agent(ABC):
    def __init__(
        self,
        llm: ModelClient | None,
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        materials: MaterialReader | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._recovery = recovery
        self._materials = materials
        self._context: Context | None = None
        self._pending_call: ToolCall | None = None

    @abstractmethod
    def _initial_context(self, documents: tuple[DocumentView, ...]) -> Context: ...

    @abstractmethod
    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None: ...

    async def _prepare(self, observation: native.Observation) -> Context:
        initializing = self._context is None
        try:
            documents = await self._materials(observation) if self._materials is not None else ()
            if self._context is None:
                self._context = self._initial_context(documents)
            else:
                self._context.append_materials(documents)
        except MaterialUnavailable as error:
            pause = True if initializing else self._recovery.record_failure()
            raise DecisionInterrupted(
                Reason(code=ReasonCode.CONTENT_UNAVAILABLE, message=str(error)), pause
            ) from error
        return self._context

    def _reject(self, message: str, observation: native.Observation) -> QueryToolResult:
        return QueryToolResult(
            Rejected(
                reason=Reason(code=ReasonCode.INVALID_ARGUMENTS, message=message),
                suggested_actions=(
                    Suggestion(
                        message=(
                            "Follow the current task and submit at most one permitted tool call."
                        )
                    ),
                ),
            ),
            observation,
        )

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if self._pending_call is not None:
            raise RuntimeError(
                "The previous action result must be accepted before another decision."
            )
        bound = observation.location
        context = await self._prepare(observation)
        self._append_task(context, task, observation)
        while True:
            try:
                assert self._llm is not None
                reply = await self._llm.complete(context.model_input(), self._tools.definitions)
            except ModelUnavailable as error:
                raise DecisionInterrupted(ModelFailure(error.reason), True) from error
            context.append_reply(reply.assistant)
            calls = reply.assistant.tool_calls
            if len(calls) == 0:
                if not isinstance(task, ActTask):
                    return TaskComplete(reply.assistant.text or None)
                message = (
                    "An action task requires its action tool; "
                    "a text answer performs no game action."
                )
                context.append_correction(message)
                if self._recovery.record_failure():
                    raise DecisionInterrupted(
                        Reason(code=ReasonCode.INVALID_ARGUMENTS, message=message), True
                    )
            elif len(calls) > 1:
                result = self._reject(
                    "Submit at most one tool call per response; none of these calls was executed.",
                    observation,
                )
                for call in calls:
                    context.append_tool_result(call, result)
                if self._recovery.record_failure():
                    raise DecisionInterrupted(
                        Reason(
                            code=ReasonCode.INVALID_ARGUMENTS,
                            message="Multiple tool calls were rejected.",
                        ),
                        True,
                    )
            else:
                call = calls[0]
                result = (
                    self._tools.dispatch_action(call, task)
                    if call.name == self._tools.action_name
                    else await self._tools.dispatch(call)
                )
                if isinstance(result, ActionProposal):
                    self._pending_call = call
                    return ActionDecision(
                        result, isinstance(task, ActTask) and task.handoff is not None
                    )
                context.append_tool_result(call, result)
                pause = self._recovery.record_outcome(result)
                if result.observation is not None:
                    observation = result.observation
                    if (
                        observation.location.game_instance_id != bound.game_instance_id
                        or observation.location.run_id != bound.run_id
                        or observation.state.phase is native.Phase.TERMINAL
                    ):
                        raise GameLifetimeEnded(observation.location)
                if pause:
                    assert isinstance(result.outcome, (Rejected, Failed, WriteUnknown))
                    raise DecisionInterrupted(result.outcome.reason, True)
                if result.observation is not None:
                    if isinstance(
                        task, ActTask
                    ) and observation.location != native.Location.model_validate(
                        task.expected_route.model_dump()
                    ):
                        raise DecisionInterrupted(
                            Reason(
                                code=ReasonCode.ROLE_CHANGED,
                                message="The action route changed; choose the current role again.",
                            ),
                            False,
                        )
            context = await self._prepare(observation)

    async def accept(self, outcome: ActionToolResult) -> None:
        if self._pending_call is None or self._context is None:
            raise RuntimeError("No model action is awaiting feedback.")
        self._context.append_tool_result(self._pending_call, outcome)
        self._pending_call = None
