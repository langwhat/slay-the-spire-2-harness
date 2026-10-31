"""One act's model role uses notes retained by the run session."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import MISSING, JsonValue, TypeAdapter, ValidationError

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.auto import choose_auto, shop_controls
from sts2_harness.capacity import CompactionDecision, CompactionValidation
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.map_projection import Plan, PlanEdge
from sts2_harness.context.models import AutomaticProgress, RunActInput, RunHandoffInput
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    Decision,
    DecisionSource,
    DecisionTask,
    HandoffTask,
    TaskComplete,
)
from sts2_harness.diagnostics import AutoFinished, Diagnostics
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import (
    FailureKind,
    ModelClient,
    ModelReply,
    ModelUnavailable,
    PreparedRequest,
    RequestPreparation,
    TextMessage,
    TextRole,
    ToolCall,
)
from sts2_harness.material_text import MaterialParts, MaterialUnavailable
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    PlanAccepted,
    PlanData,
    PlanRejected,
    PresentationUnavailable,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
from sts2_harness.route_plan import Edge, MovementStatus, RoutePlan, reconcile_plan, replace_plan
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import RunNext, RunPlan

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class ActStart:
    observation: native.Observation


class RunRole(DecisionSource, Protocol):
    async def enter_act(self, start: ActStart) -> None: ...


class RunAgent(Agent):
    def __init__(
        self,
        client: Callable[[], ModelClient],
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        prompts: PromptReader | None = None,
        materials: MaterialReader | None = None,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        if tools.notes is None:
            raise ValueError("Run requires the run's notes owner.")
        super().__init__(None, tools, recovery, materials=materials, diagnostics=diagnostics)
        self._client = client
        self._notes = tools.notes
        self._prompts = prompts if prompts is not None else PackagedPrompts()
        self._notes_delivered = False
        self._pending_auto: tuple[AutomaticProgress, native.Observation] | None = None
        self._force_strategy = False
        self._shop_strategy_node: native.Location | None = None
        self._plan: RoutePlan | None = None
        self._plan_identity: tuple[str, str | None, int | None] | None = None

    def _sync_plan(self, observation: native.Observation) -> MovementStatus:
        identity = (
            observation.location.game_instance_id,
            observation.location.run_id,
            observation.state.progress.act,
        )
        if self._plan_identity is not None:
            previous_game, previous_run, previous_act = self._plan_identity
            game, run, act = identity
            if (game, run) != (previous_game, previous_run) or (
                act is not None and previous_act is not None and act != previous_act
            ):
                self._plan = None
            if act is None and (game, run) == (previous_game, previous_run):
                identity = (game, run, previous_act)
        self._plan_identity = identity
        movement = reconcile_plan(self._plan, observation.state.progress.position)
        self._plan = movement.plan
        if self._context is not None:
            self._context.set_route_plan(self._plan)
        return movement.status

    def _plan_value(self) -> JsonValue:
        if self._plan is None:
            return None
        plan = Plan(
            reason=self._plan.reason,
            edges=tuple(
                PlanEdge(source=edge.source, destination=edge.destination)
                for edge in self._plan.edges
            ),
        )
        return _JSON_OBJECT.validate_json(plan.to_json())

    def _route_rejected(
        self,
        code: ReasonCode,
        message: str,
        observation: native.Observation | None,
        *,
        plan: bool = False,
    ) -> QueryToolResult:
        outcome = Rejected(
            reason=Reason(code=code, message=message),
            suggested_actions=(
                Suggestion(
                    message="Inspect the current map and decision, then choose a valid next step."
                ),
            ),
        )
        if plan:
            return QueryToolResult(
                outcome,
                observation,
                PlanData(
                    plan=PlanRejected(),
                    presentation=PresentationUnavailable(status="not_attempted"),
                ),
            )
        return QueryToolResult(outcome, observation)

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | QueryToolResult:
        if call.name == ToolName.RUN_ACT:
            result = self._tools.dispatch_action(call, task)
            return (
                replace(result, allow_map_travel=False)
                if isinstance(result, ActionProposal)
                else result
            )
        if call.name not in (ToolName.RUN_PLAN, ToolName.RUN_NEXT):
            return await super()._dispatch(call, task, observation)
        current = await self._tools.current()
        if current is None:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED,
                "Current map observation is unavailable.",
                None,
                plan=call.name == ToolName.RUN_PLAN,
            )
        self._sync_plan(current)
        if isinstance(task, ActTask) and current.location != native.Location.model_validate(
            task.expected_route.model_dump()
        ):
            return self._route_rejected(
                ReasonCode.ROLE_CHANGED,
                "The action route changed.",
                current,
                plan=call.name == ToolName.RUN_PLAN,
            )
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            if call.name == ToolName.RUN_PLAN:
                proposed = RunPlan.model_validate_json(call.arguments, strict=True)
                next_call = None
            else:
                next_call = RunNext.model_validate_json(call.arguments, strict=True)
                proposed = None
        except (ValueError, ValidationError):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "The route tool input does not match its schema.",
                current,
                plan=call.name == ToolName.RUN_PLAN,
            )
        if call.name == ToolName.RUN_PLAN:
            assert proposed is not None
            if current.location.owner is not native.DecisionOwner.RUN or current.map is MISSING:
                return self._route_rejected(
                    ReasonCode.QUERY_FAILED,
                    "Current map facts are unavailable.",
                    current,
                    plan=True,
                )
            update = replace_plan(
                self._plan,
                current.state.progress.position,
                frozenset(node.position for node in current.map.nodes),
                proposed.reason,
                tuple(Edge(edge.source, edge.destination) for edge in proposed.edges),
            )
            if update.rejection is not None:
                return self._route_rejected(
                    ReasonCode(update.rejection.value),
                    f"The route plan was rejected: {update.rejection.value}.",
                    current,
                    plan=True,
                )
            self._plan = update.plan
            assert self._plan is not None and self._plan.next_destination is not None
            assert self._context is not None
            self._context.set_route_plan(self._plan)
            return QueryToolResult(
                ReadCompleted(),
                current,
                PlanData(
                    plan=PlanAccepted(next=self._plan.next_destination),
                    presentation=PresentationUnavailable(status="not_available"),
                ),
            )
        if not isinstance(task, ActTask):
            return self._route_rejected(ReasonCode.WRONG_ROLE, "This task cannot travel.", current)
        if self._plan is None:
            return self._route_rejected(ReasonCode.NO_PLAN, "No route plan is active.", current)
        if current.state.progress.position is None:
            return self._route_rejected(
                ReasonCode.UNKNOWN_POSITION, "The current map position is unknown.", current
            )
        destination = self._plan.next_destination
        if destination is None:
            return self._route_rejected(
                ReasonCode.AMBIGUOUS_NEXT,
                "The route does not select one next destination.",
                current,
            )
        state = current.state
        if (
            not isinstance(state, native.RunState)
            or state.screen is not native.RunScreen.MAP
            or state.selection is not None
            or state.offers
        ):
            return self._route_rejected(
                ReasonCode.PENDING_CHOICE, "The current screen or choice blocks travel.", current
            )
        if (
            current.input_readiness is not native.InputReadiness.READY
            or current.map is MISSING
            or current.map.legal_destinations is None
            or destination not in current.map.legal_destinations
        ):
            return self._route_rejected(
                ReasonCode.TRAVEL_NOT_AVAILABLE,
                "The planned destination is not currently reachable.",
                current,
            )
        action = next(
            (
                action
                for action in state.actions
                if action.enabled and action.destination == destination
            ),
            None,
        )
        if action is None:
            return self._route_rejected(
                ReasonCode.TRAVEL_NOT_AVAILABLE,
                "Native travel is not currently available.",
                current,
            )
        assert next_call is not None
        return ActionProposal((ActionStep(ActionKind.INTERACT, action.ref),), next_call.reason)

    def _close_query(
        self,
        context: Context,
        task: DecisionTask,
        bound: native.Location,
        call: ToolCall,
        result: QueryToolResult,
    ) -> native.Observation | None:
        if result.observation is not None:
            self._sync_plan(result.observation)
        return super()._close_query(context, task, bound, call, result)

    async def _request(self, context: Context) -> ModelReply:
        assert self._llm is not None
        prepared, evidence = await self.compact(context)
        return await self._llm.complete_prepared(prepared, preparation=evidence)

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
        if self._pending_auto is not None or self._pending_call is not None:
            raise RuntimeError("Accept the previous action result before another decision.")
        if self._context is None:
            await self.enter_act(ActStart(observation))
        assert self._context is not None
        self._sync_plan(observation)
        if (
            isinstance(task, ActTask)
            and not self._force_strategy
            and not self._context.has_active_group
        ):
            automatic = choose_auto(
                observation, shop_strategy=self._shop_strategy_node == observation.location
            )
            if automatic is not None:
                self._context.begin_group(observation, self._notes.read(), self._plan_value())
                proposal, progress = automatic
                self._pending_auto = progress, observation
                return ActionDecision(proposal)
        if isinstance(observation.state, native.RunState) and shop_controls(observation.state):
            self._shop_strategy_node = observation.location
        self._context.begin_group(observation, self._notes.read(), self._plan_value())
        decision = await super().decide(task, observation)
        if isinstance(decision, TaskComplete):
            self._context.finish_group()
            self._force_strategy = False
        return decision

    async def accept(self, outcome: ActionToolResult) -> None:
        movement = (
            self._sync_plan(outcome.observation)
            if outcome.observation is not None
            else MovementStatus.UNOBSERVED
        )
        if self._pending_auto is None:
            await super().accept(outcome)
            assert self._context is not None
            self._context.finish_group()
            self._force_strategy = False
            return
        progress, before = self._pending_auto
        if self._diagnostics is not None:
            self._diagnostics.emit(
                AutoFinished(
                    label=progress.label,
                    outcome=outcome.outcome.status,
                    reason=None
                    if isinstance(outcome.outcome, ActionCompleted)
                    else outcome.outcome.reason.code,
                    role=native.DecisionOwner.RUN,
                )
            )
        assert self._context is not None
        self._context.append_automatic(
            progress, outcome, invalidated=movement is MovementStatus.INVALIDATED
        )
        self._context.finish_group()
        after = outcome.observation
        # Localized text changes alone cannot establish gameplay progress.
        self._force_strategy = (
            not isinstance(outcome.outcome, ActionCompleted)
            or after is None
            or after.game_language != before.game_language
            or after.model_copy(update={"stamp": before.stamp}) == before
        )
        self._pending_auto = None

    async def enter_act(self, start: ActStart) -> None:
        if self._pending_call is not None or self._pending_auto is not None:
            raise RuntimeError("Accept the previous action result before entering an act.")
        if self._context is not None:
            raise RuntimeError("A Run Agent cannot enter more than one act.")
        try:
            documents = (
                await self._materials(start.observation) if self._materials is not None else ()
            )
            context = self._initial_context(documents)
        except MaterialUnavailable as error:
            raise DecisionInterrupted(
                Reason(code=ReasonCode.CONTENT_UNAVAILABLE, message=str(error)), True
            ) from error
        client = self._client()
        self._llm = client
        self._context = context
        self._notes_delivered = False

    async def _prepare(self, observation: native.Observation) -> Context:
        if self._context is None:
            await self.enter_act(ActStart(observation))
            assert self._context is not None
            return self._context
        return await super()._prepare(observation)

    def _initial_context(self, documents: tuple[DocumentView, ...]) -> Context:
        blocks = (
            self._prompts.read("run-tasks"),
            self._prompts.read("run-report-reader"),
            self._prompts.read("run-task-rules"),
            "\n",
            self._prompts.read("run-projected-reader"),
            "\n",
            self._prompts.read("run-observations"),
        )
        system = (TextMessage(TextRole.SYSTEM, MaterialParts(blocks)),)
        if documents:
            system += (TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=documents)),)
        return Context(native.DecisionOwner.RUN, system=system)

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        notes = self._notes.read() if not self._notes_delivered else MISSING
        if isinstance(task, HandoffTask):
            context.append_task(RunHandoffInput(task.handoff, notes))
        elif isinstance(task, ActTask):
            context.append_task(RunActInput(notes, task.handoff), observation)
        else:
            raise TypeError("Run accepts action and handoff tasks only.")
        self._notes_delivered = True
