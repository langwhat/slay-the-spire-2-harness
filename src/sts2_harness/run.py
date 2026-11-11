"""One act's model role uses notes retained by the run session."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import MISSING, JsonValue, TypeAdapter, ValidationError

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.auto import choose_auto, shop_controls
from sts2_harness.client import ClientUnavailable
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
    SelectStep,
    TaskComplete,
)
from sts2_harness.diagnostics import AutoFinished, Diagnostics
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.game import GameReadError
from sts2_harness.knowledge import CandidateLinks, DocumentView, GuidanceContent
from sts2_harness.llm.messages import (
    ModelClient,
    TextMessage,
    TextRole,
    ToolCall,
)
from sts2_harness.material_text import MaterialParts, MaterialUnavailable
from sts2_harness.note_values import Engine
from sts2_harness.notes import WantIdentity
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
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
from sts2_harness.route_plan import Edge, MovementStatus, RoutePlan, reconcile_plan, replace_plan
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import (
    MinimalDeckChoice,
    PivotCard,
    RunDeckChoice,
    RunNext,
    RunPlan,
    SkipCard,
    TakeCard,
    WantCard,
)
from sts2_harness.user_input_values import UserInputSource

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class ActStart:
    observation: native.Observation


@dataclass(frozen=True, slots=True)
class PendingDeckChoice:
    before: native.Observation
    taken: bool
    want: WantIdentity | None
    pivot: Engine | None
    reward: bool


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
        user_input: UserInputSource | None = None,
        candidate_links: CandidateLinks | None = None,
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
        )
        self._client = client
        self._notes = tools.notes
        self._prompts = prompts if prompts is not None else PackagedPrompts()
        self._notes_delivered = False
        self._context_location: native.Location | None = None
        self._context_settled = True
        self._pending_auto: tuple[AutomaticProgress, native.Observation] | None = None
        self._force_strategy = False
        self._shop_strategy_node: native.Location | None = None
        self._plan: RoutePlan | None = None
        self._plan_identity: tuple[str, str | None, int | None] | None = None
        self._candidate_links = candidate_links
        self._declined_node: tuple[str, str | None, str | None] | None = None
        self._declined_offers: set[str] = set()
        self._pending_deck_choice: PendingDeckChoice | None = None
        self._card_actions: set[tuple[str, str | None, str | None, str]] = set()

    def _reward_view(self, observation: native.Observation) -> native.Observation:
        location = observation.location
        node = (location.game_instance_id, location.run_id, location.node_id)
        if node != self._declined_node:
            self._declined_node = node
            self._declined_offers.clear()
        state = observation.state
        self._notes.observe(observation)
        if isinstance(state, native.RunState) and state.screen in (
            native.RunScreen.REWARDS,
            native.RunScreen.SHOP,
        ):
            self._card_actions.update(
                (*node, action)
                for offer in state.offers
                if offer.kind is native.OfferKind.CARD
                for action in offer.actions
            )
        if not self._declined_offers or not isinstance(state, native.RunState):
            return observation
        excluded = {
            action
            for offer in state.offers
            if offer.ref in self._declined_offers
            for action in offer.actions
        }
        return observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": tuple(
                            o for o in state.offers if o.ref not in self._declined_offers
                        ),
                        "actions": tuple(a for a in state.actions if a.ref not in excluded),
                    }
                ),
            }
        )

    async def _enrich(self, context: Context, observation: native.Observation) -> None:
        self._sync_notes(context, observation)
        if self._tools.deck_stats is not None and isinstance(observation.state, native.RunState):
            stats, use = self._tools.deck_stats.view(observation)
            context.set_deck_stats(stats, use)
        if self._candidate_links is None or self._tools.minimal:
            return
        state = observation.state
        if not isinstance(state, native.RunState):
            context.set_candidate_links({})
            return
        owned_ids = tuple(
            sorted(
                {
                    entry.definition_id
                    for ref in (*state.deck, *state.relics)
                    if (entry := observation.objects.get(ref)) is not None
                    and entry.definition_id is not MISSING
                }
            )
        )
        candidate_ids = tuple(
            sorted(
                {
                    entry.definition_id
                    for offer in state.offers
                    for ref in offer.contents
                    if isinstance(entry := observation.objects.get(ref), native.CardEntry)
                }
            )
        )
        if not candidate_ids:
            context.set_candidate_links({})
            return
        try:
            links = await self._candidate_links.links(candidate_ids, owned_ids)
        except MaterialUnavailable as error:
            raise DecisionInterrupted(
                Reason(code=ReasonCode.CONTENT_UNAVAILABLE, message=str(error)),
                self._recovery.record_failure(),
            ) from error
        context.set_candidate_links(
            {
                key: tuple(_JSON_OBJECT.validate_json(link.to_json()) for link in values)
                for key, values in links.items()
            }
        )

    def _sync_notes(self, context: Context, observation: native.Observation) -> None:
        if self._tools.minimal:
            return
        self._notes.observe(observation)
        context.set_notes(self._notes.read())
        if isinstance(observation.state, native.RunState):
            context.set_usable_wants(
                {
                    offer.ref: self._notes.usable_wants(observation, offer.ref)
                    for offer in observation.state.offers
                    if offer.kind is native.OfferKind.CARD
                }
            )

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
                    presentation=PlanPresentation(status=PlanPresentationStatus.NOT_ATTEMPTED),
                ),
            )
        return QueryToolResult(outcome, observation)

    async def _dispatch(
        self, call: ToolCall, task: DecisionTask, observation: native.Observation
    ) -> ActionProposal | QueryToolResult:
        if call.name == ToolName.RUN_DECK_CHOICE:
            return await self._deck_choice(call, task)
        if call.name == ToolName.RUN_ACT:
            result = self._tools.dispatch_action(call, task)
            if isinstance(result, ActionProposal):
                current = await self._tools.current()
                if current is None:
                    return self._route_rejected(
                        ReasonCode.QUERY_FAILED, "The current actions are unavailable.", None
                    )
                current = self._reward_view(current)
                state = current.state
                if isinstance(state, native.RunState):
                    card_offers = tuple(
                        o
                        for o in state.offers
                        if o.kind is native.OfferKind.CARD
                        and state.screen in (native.RunScreen.REWARDS, native.RunScreen.SHOP)
                    )
                    card_actions = {ref for offer in card_offers for ref in offer.actions}
                    candidates = {ref for offer in card_offers for ref in offer.contents}
                    node = (
                        current.location.game_instance_id,
                        current.location.run_id,
                        current.location.node_id,
                    )
                    if any(
                        isinstance(step, ActionStep)
                        and (step.ref in card_actions or (*node, step.ref) in self._card_actions)
                        or isinstance(step, SelectStep)
                        and state.selection is not None
                        and bool(candidates.intersection(state.selection.candidates))
                        for step in result.steps
                    ):
                        return self._route_rejected(
                            ReasonCode.INVALID_ARGUMENTS,
                            "Resolve card rewards and buy shop cards with run_deck_choice.",
                            observation,
                        )
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
            location = current.location
            assert location.run_id is not None and location.node_id is not None
            route = native.RouteKey(
                game_instance_id=location.game_instance_id,
                run_id=location.run_id,
                node_id=location.node_id,
                owner=native.DecisionOwner.RUN,
            )
            edges = tuple(
                native.PresentationEdge.model_validate(
                    {"from": edge.source, "to": edge.destination}
                )
                for edge in self._plan.edges
            )
            try:
                presented = await self._tools.present(route, edges)
            except (ClientUnavailable, GameReadError):
                presented = None
                display = PlanPresentation(status=PlanPresentationStatus.UNKNOWN)
                restoration = PlanRestoration(status=PlanRestorationStatus.UNKNOWN)
                current = None
            else:
                receipt = presented.receipt if presented is not None else None
                if presented is None:
                    display = PlanPresentation(status=PlanPresentationStatus.NOT_AVAILABLE)
                    restoration = PlanRestoration()
                elif isinstance(receipt, (native.PresentationRejected, native.InvalidRequestReply)):
                    display = PlanPresentation(status=PlanPresentationStatus.NOT_ATTEMPTED)
                    restoration = PlanRestoration()
                    current = await self._tools.current()
                elif isinstance(receipt, native.PresentationCompleted):
                    display = PlanPresentation(status=PlanPresentationStatus(receipt.drawing.value))
                    restoration = PlanRestoration(
                        status=PlanRestorationStatus(receipt.restoration.value)
                    )
                    current = (
                        await self._tools.current()
                        if receipt.gate is native.PresentationGate.OPEN
                        else None
                    )
                else:
                    display = PlanPresentation(status=PlanPresentationStatus.UNKNOWN)
                    restoration = PlanRestoration(status=PlanRestorationStatus.UNKNOWN)
                    current = None
            return QueryToolResult(
                ReadCompleted(),
                current,
                PlanData(
                    plan=PlanAccepted(next=self._plan.next_destination),
                    presentation=display,
                    restoration=restoration,
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

    async def _deck_choice(
        self, call: ToolCall, task: DecisionTask
    ) -> ActionProposal | QueryToolResult:
        current = await self._tools.current()
        if current is None:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED, "The current reward is unavailable.", None
            )
        current = self._reward_view(current)
        if not isinstance(task, ActTask):
            return self._route_rejected(
                ReasonCode.WRONG_ROLE, "This task cannot take card rewards.", current
            )
        if current.location != native.Location.model_validate(task.expected_route.model_dump()):
            return self._route_rejected(
                ReasonCode.ROLE_CHANGED, "The action route changed.", current
            )
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = (
                MinimalDeckChoice.model_validate_json(call.arguments, strict=True).root
                if self._tools.minimal
                else RunDeckChoice.model_validate_json(call.arguments, strict=True).root
            )
        except (ValueError, ValidationError):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS, "The deck choice does not match its schema.", current
            )
        state = current.state
        offer = (
            next((o for o in state.offers if o.ref == query.offer), None)
            if isinstance(state, native.RunState)
            and state.screen in (native.RunScreen.REWARDS, native.RunScreen.SHOP)
            else None
        )
        if offer is None or offer.kind is not native.OfferKind.CARD:
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "The offer is not a current card reward or shop card.",
                current,
            )
        assert isinstance(state, native.RunState)
        if state.control is not native.StateControl.READY or current.input_readiness not in (
            native.InputReadiness.READY,
            native.InputReadiness.SELECTION,
        ):
            return self._route_rejected(
                ReasonCode.BUSY, "The current card choice is not ready.", current
            )
        shop = state.screen is native.RunScreen.SHOP
        if shop and not isinstance(query, TakeCard):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "Shop cards use run_deck_choice only when buying.",
                current,
            )
        if not shop and offer.requires_selection is MISSING:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED,
                "The Mod must publish this card reward's selection mode.",
                current,
            )
        if isinstance(query, TakeCard):
            if query.card not in offer.contents:
                return self._route_rejected(
                    ReasonCode.INVALID_ARGUMENTS,
                    "The taken card must belong to this offer.",
                    current,
                )
            if isinstance(query, WantCard) and query.want not in self._notes.usable_wants(
                current, offer.ref
            ):
                return self._route_rejected(
                    ReasonCode.INVALID_ARGUMENTS,
                    "That want was recorded after this offer appeared or is no longer present.",
                    current,
                )
            if isinstance(query, PivotCard):
                candidate = current.objects.get(query.card)
                owned = {
                    obj.definition_id
                    for ref in state.deck
                    if isinstance(obj := current.objects.get(ref), native.CardEntry)
                }
                if not isinstance(candidate, native.CardEntry):
                    return self._route_rejected(
                        ReasonCode.QUERY_FAILED,
                        "The taken card's native base ID is unavailable.",
                        current,
                    )
                if candidate.definition_id not in query.pivot.cards:
                    return self._route_rejected(
                        ReasonCode.INVALID_ARGUMENTS,
                        "The pivot does not play the taken card's base ID.",
                        current,
                    )
                if not set(query.pivot.cards) <= owned | {candidate.definition_id}:
                    return self._route_rejected(
                        ReasonCode.INVALID_ARGUMENTS,
                        "The pivot contains other card IDs that are not in the owned deck.",
                        current,
                    )
        chosen = (query.card,) if isinstance(query, TakeCard) else ()
        if not shop and not offer.requires_selection and not chosen:
            self._declined_offers.add(offer.ref)
            remaining = self._reward_view(current)
            automatic = choose_auto(remaining, shop_strategy=False)
            if automatic is not None:
                return self._propose_deck_choice(
                    replace(automatic[0], reason=query.reason), query, current
                )
            if self._tools.deck_stats is not None:
                self._tools.deck_stats.record_choice(current, taken=False)
            return QueryToolResult(ReadCompleted(), remaining)
        selection = state.selection
        if selection is not None:
            if not set(offer.contents) <= set(selection.candidates):
                return self._route_rejected(
                    ReasonCode.PENDING_CHOICE, "Another selection is open.", current
                )
            if not selection.min <= len(chosen) <= selection.max:
                return self._route_rejected(
                    ReasonCode.INVALID_ARGUMENTS,
                    "This reward does not permit that choice.",
                    current,
                )
            return self._propose_deck_choice(
                ActionProposal((SelectStep(chosen),), query.reason, allow_map_travel=False),
                query,
                current,
            )
        opening = next(
            (action for action in state.actions if action.ref in offer.actions and action.enabled),
            None,
        )
        if opening is None:
            return self._route_rejected(
                ReasonCode.ILLEGAL_ACTION, "The card reward cannot be opened now.", current
            )
        if shop or not offer.requires_selection:
            return self._propose_deck_choice(
                ActionProposal(
                    (ActionStep(ActionKind.INTERACT, opening.ref),),
                    query.reason,
                    allow_map_travel=False,
                ),
                query,
                current,
            )
        return self._propose_deck_choice(
            ActionProposal(
                (ActionStep(ActionKind.INTERACT, opening.ref), SelectStep(chosen)),
                query.reason,
                allow_map_travel=False,
            ),
            query,
            current,
        )

    def _propose_deck_choice(
        self, proposal: ActionProposal, query: TakeCard | SkipCard, before: native.Observation
    ) -> ActionProposal:
        self._pending_deck_choice = PendingDeckChoice(
            before=before,
            taken=isinstance(query, TakeCard),
            want=self._notes.want(query.want) if isinstance(query, WantCard) else None,
            pivot=query.pivot if isinstance(query, PivotCard) else None,
            reward=isinstance(before.state, native.RunState)
            and before.state.screen is native.RunScreen.REWARDS,
        )
        return proposal

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
            self._sync_notes(context, current)
        return super()._close_query(context, task, bound, call, result)

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if self._pending_auto is not None or self._pending_call is not None:
            raise RuntimeError("Accept the previous action result before another decision.")
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
                observation, shop_strategy=self._shop_strategy_node == observation.location
            )
            if automatic is not None:
                self._context.begin_group(observation, self._notes.read(), self._plan_value())
                proposal, progress = automatic
                self._pending_auto = progress, observation
                return ActionDecision(proposal)
        location = observation.location
        if (
            isinstance(task, ActTask)
            and not self._context.has_active_group
            and self._context_settled
            and observation.map is not MISSING
            and self._context_location is not None
            and (
                location.game_instance_id,
                location.run_id,
                location.node_id,
            )
            != (
                self._context_location.game_instance_id,
                self._context_location.run_id,
                self._context_location.node_id,
            )
        ):
            try:
                documents = (
                    await self._materials(observation) if self._materials is not None else ()
                )
            except MaterialUnavailable as error:
                raise DecisionInterrupted(
                    Reason(code=ReasonCode.CONTENT_UNAVAILABLE, message=str(error)),
                    self._recovery.record_failure(),
                ) from error
            context = self._initial_context(documents)
            context.set_route_plan(self._plan)
            context.begin_group(observation, self._notes.read(), self._plan_value())
            self._context = context
            self._context_location = location
            self._notes_delivered = False
        if isinstance(observation.state, native.RunState) and shop_controls(observation.state):
            self._shop_strategy_node = observation.location
        self._context.begin_group(observation, self._notes.read(), self._plan_value())
        decision = await super().decide(task, observation)
        if isinstance(decision, TaskComplete):
            self._context.finish_group()
            self._force_strategy = False
        return decision

    async def accept(self, outcome: ActionToolResult) -> None:
        pending = self._pending_deck_choice
        self._pending_deck_choice = None
        if pending is not None and isinstance(outcome.outcome, ActionCompleted):
            if pending.want is not None:
                self._notes.consume(pending.want)
            if pending.pivot is not None:
                self._notes.pivot(pending.pivot)
            if pending.reward and self._tools.deck_stats is not None:
                self._tools.deck_stats.record_choice(pending.before, taken=pending.taken)
        if outcome.observation is not None:
            outcome = replace(outcome, observation=self._reward_view(outcome.observation))
        # A closed group can still contain uncertain execution feedback.
        self._context_settled = (
            isinstance(outcome.outcome, ActionCompleted) and outcome.observation is not None
        )
        movement = (
            self._sync_plan(outcome.observation)
            if outcome.observation is not None
            else MovementStatus.UNOBSERVED
        )
        if self._pending_auto is None:
            if self._context is not None and outcome.observation is not None:
                self._sync_notes(self._context, outcome.observation)
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
            context = self._initial_context(documents)
        except MaterialUnavailable as error:
            raise DecisionInterrupted(
                Reason(code=ReasonCode.CONTENT_UNAVAILABLE, message=str(error)), True
            ) from error
        client = self._client()
        self._llm = client
        self._context = context
        self._context_location = start.observation.location
        self._notes_delivered = False

    async def _prepare(self, observation: native.Observation) -> Context:
        if self._context is None:
            await self.enter_act(ActStart(observation))
            assert self._context is not None
            context = self._context
        else:
            context = await super()._prepare(observation)
        await self._enrich(context, observation)
        return context

    def _initial_context(self, documents: tuple[DocumentView, ...]) -> Context:
        blocks = (
            self._prompts.read("run-tasks"),
            self._prompts.read("run-report-reader"),
            self._prompts.read("run-task-rules"),
            "\n",
            self._prompts.read("state-format"),
            "\n",
            self._prompts.read("run-observations"),
        )
        if not self._tools.minimal:
            blocks += ("\n", self._prompts.read("run-deck-construction"))
        if self._user_input is not None:
            blocks += ("\n", self._prompts.read("viewer-input"))
        system = (TextMessage(TextRole.SYSTEM, MaterialParts(blocks)),)
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
