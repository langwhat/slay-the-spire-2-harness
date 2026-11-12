"""A shared model decision loop; execution owns submitted game effects."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from sts2_harness.capacity import (
    CompactionDecision,
    CompactionValidation,
    RecoveryComparison,
    RecoveryDecision,
)
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.decisions import (
    ActionDecision,
    ActionProposal,
    ActTask,
    Decision,
    DecisionTask,
    ReviewTask,
    TaskComplete,
)
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.execution.recovery import DecisionInterrupted, ModelFailure, ToolRecoveryBudget
from sts2_harness.game import GameLifetimeEnded
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.messages import (
    CapacityRecovery,
    CapacityRejected,
    FailureKind,
    ModelClient,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    ToolCall,
)
from sts2_harness.material_text import MaterialUnavailable
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    Failed,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
    WriteUnknown,
)
from sts2_harness.tools import ToolCatalog, ToolName
from sts2_harness.user_input_values import UserInputSource

type MaterialReader = Callable[[native.Observation], Awaitable[tuple[DocumentView, ...]]]


class Agent(ABC):
    def __init__(
        self,
        llm: ModelClient | None,
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        materials: MaterialReader | None = None,
        diagnostics: Diagnostics | None = None,
        user_input: UserInputSource | None = None,
    ) -> None:
        self._diagnostics = diagnostics
        self._user_input = user_input
        self._llm = llm
        self._tools = tools
        self._recovery = recovery
        self._materials = materials
        self._context: Context | None = None
        self._pending_call: ToolCall | None = None

    @abstractmethod
    def _initial_context(
        self, documents: tuple[DocumentView, ...], observation: native.Observation
    ) -> Context: ...

    @abstractmethod
    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None: ...

    async def _prepare(self, observation: native.Observation) -> Context:
        initializing = self._context is None
        try:
            documents = await self._materials(observation) if self._materials is not None else ()
            if self._context is None:
                self._context = self._initial_context(documents, observation)
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
                        message=("Resubmit with at most one game-action tool, placed last.")
                    ),
                ),
            ),
            observation,
        )

    async def _request(self, context: Context) -> ModelReply:
        assert self._llm is not None
        prepared, evidence = await self.compact(context)
        try:
            return await self._llm.complete_prepared(prepared, preparation=evidence)
        except CapacityRejected as rejected:
            candidate, baseline = context.compaction_input()
            try:
                context.validate_compaction(candidate)
                compacted = self._llm.prepare(candidate, self._tools.definitions)
            except ValueError:
                await self._llm.record_recovery(
                    CapacityRecovery(
                        rejected, None, RecoveryComparison.INVALID, RecoveryDecision.INTERRUPT
                    )
                )
                raise ModelUnavailable(FailureKind.CAPACITY) from None
            if (
                compacted.measurement.estimated_input_tokens
                >= self._llm.capacity_policy.context_window
            ):
                comparison = RecoveryComparison.OVER_WINDOW
            elif (
                compacted.measurement.encoded_bytes >= prepared.measurement.encoded_bytes
                or compacted.measurement.estimated_input_tokens
                >= prepared.measurement.estimated_input_tokens
            ):
                comparison = RecoveryComparison.UNCHANGED
            else:
                comparison = RecoveryComparison.STRICTLY_SMALLER
            decision = (
                RecoveryDecision.RETRY
                if comparison is RecoveryComparison.STRICTLY_SMALLER
                else RecoveryDecision.INTERRUPT
            )
            await self._llm.record_recovery(
                CapacityRecovery(rejected, compacted, comparison, decision)
            )
            if decision is RecoveryDecision.INTERRUPT:
                raise ModelUnavailable(FailureKind.CAPACITY) from None
            complete_id, active_id = context.group_ids
            group_id = active_id or complete_id
            assert group_id is not None
            context.install_compaction(candidate)
            recovery = RequestPreparation(
                group_id,
                baseline,
                prepared.input,
                prepared.measurement.estimated_input_tokens,
                CompactionDecision.COMPACTED,
                candidate,
                compacted.measurement.estimated_input_tokens,
                complete_id,
                active_id,
                CompactionValidation.PASSED,
                rejected.reference,
            )
            return await self._llm.complete_prepared(compacted, preparation=recovery)

    async def compact(self, context: Context) -> tuple[PreparedRequest, RequestPreparation]:
        assert self._llm is not None
        client = self._llm
        tools = self._tools.definitions
        before = context.model_input()
        prepared = client.prepare(before, tools)
        estimate = prepared.measurement.estimated_input_tokens
        complete_id, active_id = context.group_ids
        group_id = active_id or complete_id
        assert group_id is not None
        candidate, baseline = context.compaction_input()
        policy = client.capacity_policy
        below_trigger = (
            estimate * policy.trigger_denominator < policy.context_window * policy.trigger_numerator
        )
        already_minimal = candidate == before and estimate < policy.context_window
        if below_trigger or already_minimal:
            evidence = RequestPreparation(
                group_id,
                baseline,
                before,
                estimate,
                CompactionDecision.UNCHANGED,
                before,
                estimate,
                complete_id,
                active_id,
                CompactionValidation.NOT_NEEDED,
            )
            return prepared, evidence
        try:
            context.validate_compaction(candidate)
        except ValueError:
            evidence = RequestPreparation(
                group_id,
                baseline,
                before,
                estimate,
                CompactionDecision.INVALID_CANDIDATE,
                None,
                None,
                complete_id,
                active_id,
                CompactionValidation.FAILED,
            )
            failure = ModelUnavailable(FailureKind.CAPACITY)
            await client.interrupt_preparation(evidence, tools, failure)
            raise failure from None
        compacted = client.prepare(candidate, tools)
        after = compacted.measurement.estimated_input_tokens
        if after >= policy.context_window:
            evidence = RequestPreparation(
                group_id,
                baseline,
                before,
                estimate,
                CompactionDecision.MINIMUM_EXCEEDS_WINDOW,
                candidate,
                after,
                complete_id,
                active_id,
                CompactionValidation.PASSED,
            )
            failure = ModelUnavailable(FailureKind.CAPACITY)
            await client.interrupt_preparation(evidence, tools, failure)
            raise failure
        context.install_compaction(candidate)
        evidence = RequestPreparation(
            group_id,
            baseline,
            before,
            estimate,
            CompactionDecision.COMPACTED,
            candidate,
            after,
            complete_id,
            active_id,
            CompactionValidation.PASSED,
        )
        return compacted, evidence

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if self._pending_call is not None:
            raise RuntimeError(
                "The previous action result must be accepted before another decision."
            )
        bound = observation.location
        context = await self._prepare(observation)
        self._append_task(context, task, observation)
        while True:
            latest: native.Observation | None = None
            self._deliver_user_input(context, task)
            try:
                reply = await self._request(context)
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
            elif any(
                call.name
                in (
                    ToolName.RUN_ACT,
                    ToolName.RUN_DECK_CHOICE,
                    ToolName.COMBAT_ACT,
                    ToolName.RUN_NEXT,
                )
                for call in calls[:-1]
            ):
                self._reject_action_layout(context, calls, observation)
            else:
                for index, call in enumerate(calls):
                    result = await self._dispatch(call, task, observation)
                    if isinstance(result, ActionProposal):
                        self._pending_call = call
                        return ActionDecision(
                            result, isinstance(task, ActTask) and task.handoff is not None
                        )
                    try:
                        current = self._close_query(context, task, bound, call, result)
                    except DecisionInterrupted:
                        self._abort_calls(context, calls[index + 1 :], index + 1, call)
                        raise
                    if current is not None:
                        observation = latest = current
                    if not isinstance(result.outcome, ReadCompleted):
                        self._abort_calls(context, calls[index + 1 :], index + 1, call)
                        break
            context = await self._prepare(observation)
            if isinstance(task, ActTask) and latest is not None:
                self._append_task(context, task, observation)

    def _deliver_user_input(self, context: Context, task: DecisionTask) -> None:
        # Each logical request freezes its input here, so HTTP retries reuse the same content
        # and later input waits for the next request. A battle's review is its Context's last
        # request, so its input waits for a decision that can still act on it.
        if self._user_input is None or isinstance(task, ReviewTask):
            return
        inputs = self._user_input.drain()
        if inputs:
            context.append_user_input(inputs)

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | QueryToolResult:
        if call.name == self._tools.action_name:
            return self._tools.dispatch_action(call, task)
        return await self._tools.dispatch(call)

    def _reject_action_layout(
        self, context: Context, calls: tuple[ToolCall, ...], observation: native.Observation
    ) -> None:
        result = self._reject(
            "At most one game-action tool is allowed per response, "
            "and it must be last; none of these calls was executed.",
            observation,
        )
        for call in calls:
            if self._diagnostics is not None:
                self._diagnostics.tool_finished(call.name, result.outcome)
            context.append_tool_result(call, result)
        if self._recovery.record_failure():
            raise DecisionInterrupted(
                Reason(
                    code=ReasonCode.INVALID_ARGUMENTS,
                    message="Multiple tool calls were rejected.",
                ),
                True,
            )

    def _abort_calls(
        self, context: Context, calls: tuple[ToolCall, ...], position: int, stopped: ToolCall
    ) -> None:
        result = QueryToolResult(
            Rejected(
                reason=Reason(
                    code=ReasonCode.BATCH_ABORTED,
                    message=f"Not executed: stopped after call {position} ({stopped.name}).",
                ),
                suggested_actions=(
                    Suggestion(
                        message="Decide again from the results above; "
                        "resubmit this call only if it is still needed."
                    ),
                ),
            ),
            None,
        )
        for call in calls:
            if self._diagnostics is not None:
                self._diagnostics.tool_finished(call.name, result.outcome)
            context.append_tool_result(call, result)

    def _close_query(
        self,
        context: Context,
        task: DecisionTask,
        bound: native.Location,
        call: ToolCall,
        result: QueryToolResult,
    ) -> native.Observation | None:
        if self._diagnostics is not None:
            self._diagnostics.tool_finished(call.name, result.outcome)
        context.append_tool_result(call, result, defer_observation=isinstance(task, ActTask))
        pause = self._recovery.record_outcome(result)
        observation = result.observation
        if observation is not None and (
            observation.location.game_instance_id != bound.game_instance_id
            or observation.location.run_id != bound.run_id
            or observation.state.phase is native.Phase.TERMINAL
        ):
            raise GameLifetimeEnded(observation.location)
        if pause:
            assert isinstance(result.outcome, (Rejected, Failed, WriteUnknown))
            raise DecisionInterrupted(result.outcome.reason, True)
        if observation is None:
            return None
        if isinstance(task, ActTask) and observation.location != native.Location.model_validate(
            task.expected_route.model_dump()
        ):
            raise DecisionInterrupted(
                Reason(
                    code=ReasonCode.ROLE_CHANGED,
                    message="The action route changed; choose the current role again.",
                ),
                False,
            )
        return observation

    async def accept(self, outcome: ActionToolResult) -> None:
        if self._pending_call is None or self._context is None:
            raise RuntimeError("No model action is awaiting feedback.")
        if self._diagnostics is not None:
            self._diagnostics.tool_finished(self._pending_call.name, outcome.outcome)
        self._context.append_tool_result(self._pending_call, outcome, defer_observation=True)
        self._pending_call = None
        if not self._tools.minimal or (
            isinstance(outcome.outcome, ActionCompleted) and outcome.observation is not None
        ):
            self._context.finish_group()
