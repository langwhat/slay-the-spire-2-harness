"""One act's model role uses notes retained by the run session."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import MISSING, JsonValue, TypeAdapter, ValidationError

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.auto import choose_auto, event_reward_surface, shop_controls
from sts2_harness.client import ClientUnavailable
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.map_projection import (
    MapUnavailable,
    planned_stretches,
    replace_stretches,
    split_stretches,
)
from sts2_harness.context.models import AutomaticProgress, RunActInput, RunHandoffInput
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    CardRewardPlan,
    Decision,
    DecisionSource,
    DecisionTask,
    HandoffTask,
    TaskComplete,
)
from sts2_harness.deck import next_card_reward
from sts2_harness.diagnostics import AutoFinished, Diagnostics, MechanismTracker
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.game import GameReadError
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import (
    ModelClient,
    TextMessage,
    TextRole,
    ToolCall,
)
from sts2_harness.material_text import MaterialParts, MaterialUnavailable
from sts2_harness.prompts import PackagedPrompts, PromptReader, deck_act_prompt
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    ExecutionResult,
    PlanAccepted,
    PlanData,
    PlanPresentation,
    PlanPresentationStatus,
    PlanRejected,
    PlanRestoration,
    PlanRestorationStatus,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
from sts2_harness.route_plan import MovementStatus, RoutePlan, reconcile_plan
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import (
    RunNext,
    RunPlan,
)
from sts2_harness.user_input_values import UserInputSource

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def run_observation(observation: native.Observation) -> native.Observation:
    state = observation.state
    if not isinstance(state, native.RunState) or state.screen is not native.RunScreen.REWARDS:
        return observation
    excluded_offers = {offer.ref for offer in state.offers if offer.kind is native.OfferKind.CARD}
    if not excluded_offers:
        return observation
    excluded = {
        action for offer in state.offers if offer.ref in excluded_offers for action in offer.actions
    }
    offers = tuple(o for o in state.offers if o.ref not in excluded_offers)
    actions = tuple(a for a in state.actions if a.ref not in excluded)
    selection = state.selection
    if selection is not None and any(
        offer.ref in excluded_offers and set(offer.contents) <= set(selection.candidates)
        for offer in state.offers
    ):
        selection = None
    hidden = set(excluded_offers) | excluded
    hidden.update(
        ref for offer in state.offers if offer.ref in excluded_offers for ref in offer.contents
    )
    hidden.update(
        action.subject
        for action in state.actions
        if action.ref in excluded and isinstance(action.subject, str)
    )
    retained = set((*state.deck, *state.relics, *state.potions))
    retained.update(ref for offer in offers for ref in (offer.ref, *offer.contents))
    retained.update(action.subject for action in actions if isinstance(action.subject, str))
    if selection is not None:
        retained.update(selection.candidates)
    hidden.difference_update(retained)
    hidden_ids = {
        entry.definition_id
        for ref, entry in observation.objects.items()
        if ref in hidden and isinstance(entry.definition_id, str)
    }
    objects = {ref: entry for ref, entry in observation.objects.items() if ref not in hidden}
    visible_ids = {
        entry.definition_id for entry in objects.values() if isinstance(entry.definition_id, str)
    }
    return observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "offers": offers,
                    "actions": actions,
                    "selection": selection,
                }
            ),
            "objects": objects,
            "descriptions": {
                ref: text for ref, text in observation.descriptions.items() if ref not in hidden
            },
            "modifier_descriptions": {
                ref: text
                for ref, text in observation.modifier_descriptions.items()
                if ref not in hidden
            },
            "upgrades": {
                ref: preview for ref, preview in observation.upgrades.items() if ref not in hidden
            },
            "generated": {
                ref: cards for ref, cards in observation.generated.items() if ref not in hidden
            },
            "mechanisms": tuple(
                rule
                for rule in observation.mechanisms
                if rule.owner not in hidden
                and (rule.definition_id not in hidden_ids or rule.definition_id in visible_ids)
            ),
        }
    )


def enrich_run_context(
    context: Context, tools: ToolCatalog, observation: native.Observation
) -> None:
    if not tools.minimal and tools.notes is not None:
        context.set_notes(tools.notes.read())
    if tools.fights is not None:
        context.set_fights(tools.fights.recent)
        context.set_elites_fought(tools.fights.act_elites)
    if tools.deck_stats is not None and isinstance(observation.state, native.RunState):
        stats, use = tools.deck_stats.view(observation)
        context.set_deck_stats(stats, use)


@dataclass(frozen=True, slots=True)
class ActStart:
    observation: native.Observation


class RunRole(DecisionSource, Protocol):
    @property
    def route_plan(self) -> RoutePlan | None: ...

    async def enter_act(self, start: ActStart) -> None: ...

    def card_progress(self, progress: AutomaticProgress, result: ActionToolResult) -> None: ...


class RunAgent(Agent):
    @property
    def route_plan(self) -> RoutePlan | None:
        return self._plan

    def __init__(
        self,
        client: Callable[[], ModelClient],
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        prompts: PromptReader | None = None,
        materials: MaterialReader | None = None,
        diagnostics: Diagnostics | None = None,
        user_input: UserInputSource | None = None,
        card_reports: list[tuple[AutomaticProgress, ActionToolResult]] | None = None,
        mechanism_tracker: MechanismTracker | None = None,
    ) -> None:
        if tools.notes is None:
            raise ValueError("Run requires the run's notes owner.")
        super().__init__(
            None,
            tools,
            recovery,
            materials=materials,
            diagnostics=diagnostics,
            user_input=user_input,
            mechanism_tracker=mechanism_tracker,
        )
        self._client = client
        self._notes = tools.notes
        self._prompts = prompts if prompts is not None else PackagedPrompts()
        self._notes_delivered = False
        self._pending_auto: tuple[AutomaticProgress, native.Observation] | None = None
        self._force_strategy = False
        self._shop_strategy_node: native.Location | None = None
        self._plan: RoutePlan | None = None
        self._plan_identity: tuple[str, str | None, int | None] | None = None
        self._card_reports = card_reports if card_reports is not None else []

    def _reward_view(self, observation: native.Observation) -> native.Observation:
        return run_observation(observation)

    def card_progress(self, progress: AutomaticProgress, result: ActionToolResult) -> None:
        self._card_reports.append((progress, result))

    async def _enrich(self, context: Context, observation: native.Observation) -> None:
        enrich_run_context(context, self._tools, observation)

    def _sync_notes(self, context: Context) -> None:
        if self._tools.minimal:
            return
        context.set_notes(self._notes.read())

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

    def _plan_value(self, observation: native.Observation) -> JsonValue:
        if self._plan is None or observation.map is MISSING:
            return None
        try:
            stretches = planned_stretches(
                split_stretches(observation.map), observation.state.progress.position, self._plan
            )
        except MapUnavailable:
            return None
        return list(stretches) if stretches is not None else None

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
                    message="Check the tool schema and current state references, "
                    "then choose a valid next step."
                ),
            ),
        )
        if plan:
            return QueryToolResult(
                outcome,
                observation,
                PlanData(
                    plan=PlanRejected(),
                    presentation=PlanPresentation(status=PlanPresentationStatus.NOT_ATTEMPTED),
                ),
            )
        return QueryToolResult(outcome, observation)

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | CardRewardPlan | QueryToolResult:
        if call.name == ToolName.RUN_ACT:
            result = await self._tools.dispatch_action(call, task)
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
                if self._tools.minimal and next_call.notes is not MISSING:
                    raise ValueError("Strategy notes are unavailable in minimal mode.")
                proposed = None
        except (ValueError, ValidationError):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "The route tool input does not match its schema.",
                current,
                plan=call.name == ToolName.RUN_PLAN,
            )
        if next_call is not None:
            if not isinstance(task, ActTask):
                return self._route_rejected(
                    ReasonCode.WRONG_ROLE, "This task cannot travel.", current
                )
            if next_call.notes is not MISSING:
                written = await self._tools.write_notes(next_call.notes, current)
                if not isinstance(written.outcome, ReadCompleted):
                    return written
            if next_call.stretches is not MISSING:
                proposed = RunPlan(reason=next_call.reason, stretches=next_call.stretches)
        if proposed is not None:
            planned = await self._replace_plan(proposed, current)
            if next_call is None or not isinstance(planned.outcome, ReadCompleted):
                return planned
            current = planned.observation
            if current is None:
                return self._route_rejected(
                    ReasonCode.QUERY_FAILED, "Current map observation is unavailable.", None
                )
            assert isinstance(task, ActTask)
            if current.location != native.Location.model_validate(task.expected_route.model_dump()):
                return self._route_rejected(
                    ReasonCode.ROLE_CHANGED, "The action route changed.", current
                )
        if not isinstance(task, ActTask):
            return self._route_rejected(ReasonCode.WRONG_ROLE, "This task cannot travel.", current)
        if self._plan is None:
            return self._route_rejected(ReasonCode.NO_PLAN, "No route plan is active.", current)
        if current.state.progress.position is None and self._plan.position is not None:
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

    async def _replace_plan(
        self, proposed: RunPlan, current: native.Observation
    ) -> QueryToolResult:
        if current.location.owner is not native.DecisionOwner.RUN or current.map is MISSING:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED,
                "Current map facts are unavailable.",
                current,
                plan=True,
            )
        try:
            update = replace_stretches(
                self._plan,
                current.map,
                current.state.progress.position,
                proposed.reason,
                proposed.stretches,
            )
        except MapUnavailable:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED,
                "Current map facts are incomplete.",
                current,
                plan=True,
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
        await self._tools.display_reason(proposed.reason)
        location = current.location
        assert location.run_id is not None and location.node_id is not None
        route = native.RouteKey(
            game_instance_id=location.game_instance_id,
            run_id=location.run_id,
            node_id=location.node_id,
            owner=native.DecisionOwner.RUN,
        )
        edges = tuple(
            native.PresentationEdge.model_validate({"from": edge.source, "to": edge.destination})
            for edge in self._plan.edges
            if edge.source is not None
        )
        restored: native.Observation | None = current
        try:
            presented = await self._tools.present(route, edges)
        except (ClientUnavailable, GameReadError):
            presented = None
            display = PlanPresentation(status=PlanPresentationStatus.UNKNOWN)
            restoration = PlanRestoration(status=PlanRestorationStatus.UNKNOWN)
            restored = None
        else:
            receipt = presented.receipt if presented is not None else None
            if presented is None:
                display = PlanPresentation(status=PlanPresentationStatus.NOT_AVAILABLE)
                restoration = PlanRestoration()
            elif isinstance(receipt, (native.PresentationRejected, native.InvalidRequestReply)):
                display = PlanPresentation(status=PlanPresentationStatus.NOT_ATTEMPTED)
                restoration = PlanRestoration()
                restored = await self._tools.current()
            elif isinstance(receipt, native.PresentationCompleted):
                display = PlanPresentation(status=PlanPresentationStatus(receipt.drawing.value))
                restoration = PlanRestoration(
                    status=PlanRestorationStatus(receipt.restoration.value)
                )
                restored = (
                    await self._tools.current()
                    if receipt.gate is native.PresentationGate.OPEN
                    else None
                )
            else:
                display = PlanPresentation(status=PlanPresentationStatus.UNKNOWN)
                restoration = PlanRestoration(status=PlanRestorationStatus.UNKNOWN)
                restored = None
        return QueryToolResult(
            ReadCompleted(),
            restored,
            PlanData(
                plan=PlanAccepted(next=proposed.stretches[0]),
                presentation=display,
                restoration=restoration,
            ),
        )

    def _close_query(
        self,
        context: Context,
        task: DecisionTask,
        bound: native.Location,
        call: ToolCall,
        result: QueryToolResult,
    ) -> native.Observation | None:
        if result.observation is not None:
            current = self._reward_view(result.observation)
            result = replace(result, observation=current)
            self._sync_plan(current)
            self._sync_notes(context)
        return super()._close_query(context, task, bound, call, result)

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if self._pending_auto is not None or self._pending_call is not None:
            raise RuntimeError("Accept the previous action result before another decision.")
        native_state = observation.state
        if (
            isinstance(task, ActTask)
            and isinstance(native_state, native.RunState)
            and event_reward_surface(native_state)
            and next_card_reward(observation, task.resolved_card_rewards) is not None
        ):
            raise DecisionInterrupted(
                Reason(code=ReasonCode.ROLE_CHANGED, message="An event card reward is ready."),
                False,
            )
        observation = self._reward_view(observation)
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
                observation,
                shop_strategy=self._shop_strategy_node == observation.location,
                allow_event_potion=native_state.selection is None,
            )
            if automatic is not None:
                self._context.begin_group(
                    observation, self._notes.read(), self._plan_value(observation)
                )
                proposal, progress = automatic
                self._pending_auto = progress, observation
                return ActionDecision(proposal)
        if isinstance(observation.state, native.RunState) and shop_controls(observation.state):
            self._shop_strategy_node = observation.location
        self._context.begin_group(observation, self._notes.read(), self._plan_value(observation))
        decision = await super().decide(task, observation)
        if isinstance(decision, TaskComplete):
            self._context.finish_group()
            self._force_strategy = False
        return decision

    async def accept(self, outcome: ExecutionResult) -> None:
        if not isinstance(outcome, ActionToolResult):
            raise TypeError("RunAgent accepts action results only.")
        if outcome.observation is not None:
            outcome = replace(outcome, observation=self._reward_view(outcome.observation))
        movement = (
            self._sync_plan(outcome.observation)
            if outcome.observation is not None
            else MovementStatus.UNOBSERVED
        )
        if self._pending_auto is None:
            if self._context is not None and outcome.observation is not None:
                self._sync_notes(self._context)
            await super().accept(outcome)
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
        after = outcome.observation
        # Localized text changes alone cannot establish gameplay progress.
        self._force_strategy = (
            not isinstance(outcome.outcome, ActionCompleted)
            or after is None
            or after.game_language != before.game_language
            or after.model_copy(update={"stamp": before.stamp}) == before
        )
        if not self._tools.minimal or not self._force_strategy:
            self._context.finish_group()
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
            context = self._initial_context(documents, start.observation)
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
            context = self._context
        else:
            context = await super()._prepare(observation)
        await self._enrich(context, observation)
        for index, (progress, result) in enumerate(self._card_reports, start=1):
            # Run needs each outcome and only the state at the handoff.
            current = self._reward_view(observation) if index == len(self._card_reports) else None
            context.append_automatic(progress, replace(result, observation=current))
        self._card_reports.clear()
        return context

    def _initial_context(
        self, documents: tuple[DocumentView, ...], observation: native.Observation
    ) -> Context:
        blocks = (
            self._prompts.read("run-tasks"),
            self._prompts.read("run-report-reader"),
            self._prompts.read("run-task-rules"),
            "\n",
            self._prompts.read("state-format"),
            "\n",
            self._prompts.read("run-observations"),
            "\n",
            self._prompts.read("deck"),
            "\n",
            self._prompts.read(deck_act_prompt(observation.state.progress)),
        )
        if not self._tools.minimal:
            blocks += ("\n", self._prompts.read("run-notes"))
        if self._user_input is not None:
            blocks += ("\n", self._prompts.read("viewer-input"))
        system = (
            TextMessage(TextRole.SYSTEM, MaterialParts(blocks)),
            TextMessage(
                TextRole.SYSTEM,
                json.dumps(
                    {"run_rules": observation.run_rules.model_dump(mode="json")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        if documents:
            system += (TextMessage(TextRole.SYSTEM, GuidanceContent(guidance=documents)),)
        return Context(native.DecisionOwner.RUN, system=system, minimal=self._tools.minimal)

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        notes = (
            self._notes.read() if not self._notes_delivered and not self._tools.minimal else MISSING
        )
        if isinstance(task, HandoffTask):
            context.append_task(RunHandoffInput(task.handoff, notes))
        elif isinstance(task, ActTask):
            context.append_task(RunActInput(notes, task.handoff, task.previous_battle), observation)
        else:
            raise TypeError("Run accepts action and handoff tasks only.")
        self._notes_delivered = True
