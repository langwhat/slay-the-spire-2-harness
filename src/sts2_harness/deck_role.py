"""Joint card reward decisions, with per-group native completion evidence."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import MISSING, ValidationError

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.client import ClientUnavailable, RewardCloseResult, RewardOpenResult
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import AutomaticProgress, RunActInput
from sts2_harness.decisions import (
    ActionProposal,
    ActTask,
    CardRewardChoice,
    CardRewardPlan,
    Decision,
    DecisionSource,
    DecisionTask,
)
from sts2_harness.deck import (
    CardChoiceCompletion,
    CardChoiceKind,
    adding_selection,
    card_rewards,
    deck_context,
    deck_observation,
    same_card_options,
)
from sts2_harness.diagnostics import Diagnostics, MechanismTracker
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.messages import ModelClient, ModelReply, ToolCall
from sts2_harness.prompts import PackagedPrompts, PromptReader
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    CardRewardBatchResult,
    CardRewardResult,
    ExecutionResult,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
from sts2_harness.route_plan import RoutePlan, reconcile_plan
from sts2_harness.run import enrich_run_context
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import RunDeckChoice, SkipCard, TakeCard
from sts2_harness.user_input_values import UserInputSource


@dataclass(frozen=True, slots=True)
class PendingCardChoice:
    call: ToolCall
    choices: tuple[TakeCard | SkipCard, ...]
    resolved: frozenset[str]


@dataclass(frozen=True, slots=True)
class CardResolution:
    progress: AutomaticProgress
    result: ActionToolResult


class DeckRole(DecisionSource, Protocol):
    def take_resolutions(self) -> tuple[CardResolution, ...]: ...

    def offered(
        self, observation: native.Observation, resolved: frozenset[str] = frozenset()
    ) -> bool: ...

    def set_route_plan(self, plan: RoutePlan | None) -> None: ...


def deck_offers(
    observation: native.Observation,
    resolved: frozenset[str] = frozenset(),
    *,
    offer_ref: str | None = None,
) -> tuple[native.Offer, ...]:
    offers, _ = card_rewards(observation)
    return tuple(
        offer
        for offer in offers
        if offer.ref not in resolved and (offer_ref is None or offer.ref == offer_ref)
    )


def deck_offered(
    observation: native.Observation,
    resolved: frozenset[str] = frozenset(),
    *,
    offer_ref: str | None = None,
) -> bool:
    if adding_selection(observation) is not None:
        return True
    if not isinstance(observation.state, native.RunState):
        return False
    offers = deck_offers(observation, resolved, offer_ref=offer_ref)
    _, active = card_rewards(observation)
    return bool(offers) and (observation.state.selection is None or active in offers)


def _deck_rejected(
    code: ReasonCode, message: str, observation: native.Observation | None
) -> QueryToolResult:
    return QueryToolResult(
        Rejected(
            reason=Reason(code=code, message=message),
            suggested_actions=(
                Suggestion(
                    message="Check the tool schema and current state references, "
                    "then choose a valid next step."
                ),
            ),
        ),
        observation,
    )


_EMPTY_RESOLVED: frozenset[str] = frozenset()


class CardRewardPreview(Protocol):
    @property
    def card_reward_preview_enabled(self) -> bool: ...
    def start_open_card_reward(self, route: native.RouteKey, offer: str) -> None: ...
    async def cancel_open_card_reward(self) -> None: ...
    async def open_card_reward(self, route: native.RouteKey, offer: str) -> RewardOpenResult: ...
    async def close_card_reward(self, route: native.RouteKey, offer: str) -> RewardCloseResult: ...


def _preview_candidate(
    observation: native.Observation,
    resolved: frozenset[str] = _EMPTY_RESOLVED,
    *,
    offer_ref: str | None = None,
) -> tuple[native.RouteKey, str] | None:
    state = observation.state
    if (
        not isinstance(state, native.RunState)
        or state.screen is not native.RunScreen.REWARDS
        or state.selection is not None
        or observation.input_readiness is not native.InputReadiness.READY
        or observation.location.owner is not native.DecisionOwner.RUN
        or observation.location.run_id is None
        or observation.location.node_id is None
    ):
        return None
    enabled_actions = {action.ref for action in state.actions if action.enabled}
    for offer in deck_offers(observation, resolved, offer_ref=offer_ref):
        if offer.requires_selection is True and any(
            action_ref in enabled_actions for action_ref in offer.actions
        ):
            route = native.RouteKey(
                game_instance_id=observation.location.game_instance_id,
                run_id=observation.location.run_id,
                node_id=observation.location.node_id,
                owner=native.DecisionOwner.RUN,
            )
            return route, offer.ref
    return None


def validate_deck_choice(
    call: ToolCall,
    task: DecisionTask,
    delivered: native.Observation,
    raw: native.Observation | None,
    *,
    offer_ref: str | None = None,
    filter_view: bool = True,
    route_plan: RoutePlan | None = None,
    previewed_offer_ref: str | None = None,
) -> tuple[CardRewardPlan, tuple[TakeCard | SkipCard, ...]] | QueryToolResult:
    resolved = task.resolved_card_rewards if isinstance(task, ActTask) else _EMPTY_RESOLVED
    if raw is None:
        return _deck_rejected(ReasonCode.QUERY_FAILED, "The current rewards are unavailable.", None)
    current = (
        deck_observation(
            raw,
            deck_offers(raw, resolved, offer_ref=offer_ref),
            route_plan=reconcile_plan(route_plan, raw.state.progress.position).plan,
        )
        if filter_view and isinstance(raw.state, native.RunState)
        else raw
    )
    if not isinstance(task, ActTask):
        return _deck_rejected(ReasonCode.WRONG_ROLE, "This task cannot take card rewards.", current)
    if raw.location != native.Location.model_validate(task.expected_route.model_dump()):
        return _deck_rejected(ReasonCode.ROLE_CHANGED, "The action route changed.", current)
    try:
        json.loads(call.arguments, object_pairs_hook=unique_fields)
        query = RunDeckChoice.model_validate_json(call.arguments, strict=True)
    except (ValueError, ValidationError):
        return _deck_rejected(
            ReasonCode.INVALID_ARGUMENTS,
            "The deck choices do not match the tool schema.",
            current,
        )
    state = raw.state
    if (
        not isinstance(state, native.RunState)
        or state.control is not native.StateControl.READY
        or raw.input_readiness not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
    ):
        return _deck_rejected(ReasonCode.BUSY, "The current card choices are not ready.", current)
    delivered_state = delivered.state
    current_state = current.state
    assert isinstance(delivered_state, native.RunState)
    assert isinstance(current_state, native.RunState)
    delivered_candidates = (
        delivered_state.selection.candidates if delivered_state.selection is not None else ()
    )
    refs = tuple(
        dict.fromkeys(
            (
                *delivered_candidates,
                *(ref for offer in delivered_state.offers for ref in offer.contents),
            )
        )
    )
    offers = {offer.ref: offer for offer in deck_offers(raw, resolved, offer_ref=offer_ref)}
    _, active = card_rewards(raw)
    selection = adding_selection(raw)
    preview_opened = (
        previewed_offer_ref is not None
        and selection is None
        and delivered_state.selection is None
        and current_state.selection is not None
        and active is not None
        and active.ref == previewed_offer_ref
        and active.ref in offers
        and set(current_state.selection.candidates) == set(active.contents)
    )
    if (
        delivered_state.offers != current_state.offers
        or (delivered_state.selection != current_state.selection and not preview_opened)
        or not same_card_options(delivered, current, refs)
    ):
        return _deck_rejected(
            ReasonCode.REFERENCE_EXPIRED, "Review the updated card reward groups.", current
        )
    if selection is None and (
        set(item.offer for item in query.choices) != set(offers)
        or len(query.choices) != len(offers)
    ):
        return _deck_rejected(
            ReasonCode.INVALID_ARGUMENTS,
            "Submit one choice for each remaining card reward.",
            current,
        )
    if selection is not None and (len(query.choices) != 1 or query.choices[0].offer is not MISSING):
        return _deck_rejected(
            ReasonCode.INVALID_ARGUMENTS,
            "Submit one choice without an offer for this selection.",
            current,
        )
    if (
        selection is None
        and state.selection is not None
        and (active is None or query.choices[0].offer != active.ref)
    ):
        return _deck_rejected(
            ReasonCode.PENDING_CHOICE, "Decide the open card reward first.", current
        )
    plan: list[CardRewardChoice] = []
    for item in query.choices:
        offer = None if item.offer is MISSING else offers[item.offer]
        chosen = item.cards if isinstance(item, TakeCard) else ()
        if offer is not None and offer.requires_selection is MISSING:
            return _deck_rejected(
                ReasonCode.QUERY_FAILED,
                "The Mod must publish the reward selection mode.",
                current,
            )
        limits = selection if offer is None else state.selection if offer == active else None
        candidates = limits.candidates if limits is not None else offer.contents if offer else ()
        if (
            len(set(chosen)) != len(chosen)
            or not set(chosen) <= set(candidates)
            or (limits is not None and not limits.min <= len(chosen) <= limits.max)
            or (limits is None and len(chosen) > 1)
        ):
            return _deck_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "Use the card refs and limits of each current reward group.",
                current,
            )
        plan.append(CardRewardChoice(offer, chosen, item.reason))
    return (
        CardRewardPlan(choices=tuple(plan), before=raw, resolved=resolved),
        query.choices,
    )


class DeckAgent(Agent):
    def __init__(
        self,
        client: Callable[[], ModelClient],
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        offer_ref: str | None = None,
        stream: str = "deck:replay",
        prompts: PromptReader | None = None,
        materials: MaterialReader | None = None,
        completion: Callable[[CardChoiceCompletion], None] | None = None,
        reward_preview: CardRewardPreview | None = None,
        user_input: UserInputSource | None = None,
        diagnostics: Diagnostics | None = None,
        mechanism_tracker: MechanismTracker | None = None,
    ) -> None:
        if tools.notes is None:
            raise ValueError("Deck requires the run's notes owner.")
        if not self._accepts_tools(tools):
            raise ValueError("DeckAgent requires a focused Deck tool catalog")
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
        self._plan: RoutePlan | None = None
        self._offer_ref = offer_ref
        self._stream = stream
        self._completion = completion
        self._reward_preview = reward_preview
        self._preview_target: tuple[native.RouteKey, str] | None = None
        self._preview_pending: tuple[native.RouteKey, str] | None = None
        self._previewed_offer_ref: str | None = None
        self._choice: PendingCardChoice | None = None
        self._resolutions: list[CardResolution] = []
        self._notes_delivered = False

    def _accepts_tools(self, tools: ToolCatalog) -> bool:
        return tuple(tool.name for tool in tools.definitions) == (ToolName.RUN_DECK_CHOICE,)

    def _reward_view(
        self, observation: native.Observation, resolved: frozenset[str] = frozenset()
    ) -> native.Observation:
        if not isinstance(observation.state, native.RunState):
            return observation
        active_plan = reconcile_plan(self._plan, observation.state.progress.position).plan
        return deck_observation(
            observation,
            deck_offers(observation, resolved, offer_ref=self._offer_ref),
            route_plan=active_plan,
        )

    def set_route_plan(self, plan: RoutePlan | None) -> None:
        self._plan = plan

    async def _prepare(self, observation: native.Observation) -> Context:
        context = await super()._prepare(observation)
        if self._llm is None:
            self._llm = self._client()
        enrich_run_context(context, self._tools, observation)
        context.set_route_plan(reconcile_plan(self._plan, observation.state.progress.position).plan)
        return context

    def offered(
        self, observation: native.Observation, resolved: frozenset[str] = frozenset()
    ) -> bool:
        return deck_offered(observation, resolved, offer_ref=self._offer_ref)

    def take_resolutions(self) -> tuple[CardResolution, ...]:
        reports = tuple(self._resolutions)
        self._resolutions.clear()
        return reports

    def _initial_context(
        self,
        documents: tuple[DocumentView, ...],
        observation: native.Observation,
    ) -> Context:
        return deck_context(
            self._prompts,
            documents,
            observation,
            minimal=self._tools.minimal,
            viewer_input=self._user_input is not None,
        )

    def _append_task(
        self, context: Context, task: DecisionTask, observation: native.Observation
    ) -> None:
        if not isinstance(task, ActTask):
            raise TypeError("Deck accepts action tasks only.")
        context.begin_group(observation, self._notes.read(), None)
        notes = (
            self._notes.read() if not self._notes_delivered and not self._tools.minimal else MISSING
        )
        previous = task.handoff or task.previous_battle
        context.append_task(RunActInput(notes, None, previous), observation)
        self._notes_delivered = True

    def _close_query(
        self,
        context: Context,
        task: DecisionTask,
        bound: native.Location,
        call: ToolCall,
        result: QueryToolResult,
    ) -> native.Observation | None:
        if result.observation is not None:
            resolved = task.resolved_card_rewards if isinstance(task, ActTask) else _EMPTY_RESOLVED
            current = self._reward_view(result.observation, resolved)
            result = replace(result, observation=current)
            if not self._tools.minimal:
                context.set_notes(self._notes.read())
            context.set_route_plan(reconcile_plan(self._plan, current.state.progress.position).plan)
        return super()._close_query(context, task, bound, call, result)

    async def _settle_preview(self) -> None:
        pending = self._preview_pending
        if pending is None or self._reward_preview is None:
            return
        self._preview_pending = None
        route, offer_ref = pending
        try:
            result = await self._reward_preview.open_card_reward(route, offer_ref)
        except ClientUnavailable:
            # An unconfirmed open may still have opened the native screen; let raw state decide.
            self._previewed_offer_ref = offer_ref
            return
        if isinstance(result, native.RewardOpenReply) and (
            result.status is native.RewardOpenStatus.OPENED
        ):
            self._previewed_offer_ref = offer_ref

    async def _request(self, context: Context) -> ModelReply:
        if self._preview_target is not None and self._reward_preview is not None:
            route, offer_ref = self._preview_target
            self._preview_target = None
            self._preview_pending = (route, offer_ref)
            self._reward_preview.start_open_card_reward(route, offer_ref)
        return await super()._request(context)

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        resolved = task.resolved_card_rewards if isinstance(task, ActTask) else _EMPTY_RESOLVED
        if not self.offered(observation, resolved):
            raise DecisionInterrupted(
                Reason(code=ReasonCode.ROLE_CHANGED, message="The card reward groups changed."),
                False,
            )
        self._preview_target = (
            _preview_candidate(observation, resolved, offer_ref=self._offer_ref)
            if self._reward_preview is not None and self._reward_preview.card_reward_preview_enabled
            else None
        )
        self._preview_pending = None
        self._previewed_offer_ref = None
        view = self._reward_view(observation, resolved)
        try:
            return await super().decide(task, view)
        finally:
            if self._reward_preview is not None:
                await self._reward_preview.cancel_open_card_reward()
            self._preview_target = None
            self._preview_pending = None
            self._previewed_offer_ref = None

    async def _dispatch(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation,
    ) -> ActionProposal | CardRewardPlan | QueryToolResult:
        if call.name == ToolName.RUN_DECK_CHOICE:
            return await self._deck_choice(call, task, observation)
        return await self._tools.dispatch(call)

    def _record(
        self, call: ToolCall, index: int, query: TakeCard | SkipCard, result: CardRewardResult
    ) -> None:
        offer = None if query.offer is MISSING else query.offer
        taken = isinstance(query, TakeCard)
        if self._completion is not None:
            self._completion(
                CardChoiceCompletion(
                    stream=self._stream,
                    call_id=call.id,
                    batch_index=index,
                    offer_ref=offer,
                    choice=CardChoiceKind.TAKE if taken else CardChoiceKind.SKIP,
                    card_ref=query.card if isinstance(query, TakeCard) else None,
                    before=result.before,
                    after=result.after,
                    outcome=ReadCompleted() if result.local_decline else result.outcome,
                    local_decline=result.local_decline,
                )
            )
        completed = isinstance(result.outcome, ActionCompleted)
        if completed and self._tools.deck_stats is not None:
            self._tools.deck_stats.record_choice(
                result.before,
                taken=taken,
                count=len(query.cards) if isinstance(query, TakeCard) else 1,
            )
        names: list[str] = []
        if isinstance(query, TakeCard):
            for ref in query.cards:
                entry = result.before.objects.get(ref)
                if isinstance(entry, native.CardEntry):
                    names.append(entry.definition_id)
        self._resolutions.append(
            CardResolution(
                AutomaticProgress(label=", ".join(names) if taken else "Skip", reason=query.reason),
                ActionToolResult(result.outcome, result.after),
            )
        )

    async def accept(self, outcome: ExecutionResult) -> None:
        if not isinstance(outcome, CardRewardBatchResult):
            raise TypeError("DeckAgent accepts card reward batch results only.")
        pending = self._choice
        assert pending is not None
        self._choice = None
        for index, result in enumerate(outcome.groups, 1):
            self._record(pending.call, index, pending.choices[index - 1], result)
        resolved = pending.resolved | frozenset(
            group.offer_ref for group in outcome.completed_groups if group.offer_ref is not None
        )
        if outcome.observation is not None:
            filtered = self._reward_view(outcome.observation, resolved)
            outcome = replace(outcome, observation=filtered)
            if self._context is not None:
                if not self._tools.minimal:
                    self._context.set_notes(self._notes.read())
                self._context.set_route_plan(
                    reconcile_plan(self._plan, filtered.state.progress.position).plan
                )
        await super().accept(outcome)

    async def _run_close_preview(self, route: native.RouteKey, offer_ref: str) -> None:
        if self._reward_preview is None:
            return
        try:
            await self._reward_preview.close_card_reward(route, offer_ref)
        except ClientUnavailable:
            return

    def _first_chosen_offer(self, call: ToolCall) -> str | None:
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = RunDeckChoice.model_validate_json(call.arguments, strict=True)
        except (ValueError, ValidationError):
            return None
        first = query.choices[0].offer
        return None if first is MISSING else first

    async def _deck_choice(
        self,
        call: ToolCall,
        task: DecisionTask,
        delivered: native.Observation,
    ) -> CardRewardPlan | QueryToolResult:
        await self._settle_preview()
        raw = await self._tools.current()
        if (
            self._previewed_offer_ref is not None
            and self._reward_preview is not None
            and isinstance(task, ActTask)
            and raw is not None
            and isinstance(delivered.state, native.RunState)
            and delivered.state.selection is None
            and isinstance(raw.state, native.RunState)
            and raw.state.selection is not None
            and raw.location == native.Location.model_validate(task.expected_route.model_dump())
        ):
            _, active = card_rewards(raw)
            first_offer = self._first_chosen_offer(call)
            if (
                active is not None
                and active.ref == self._previewed_offer_ref
                and first_offer is not None
                and first_offer != active.ref
            ):
                await self._run_close_preview(task.expected_route, active.ref)
                raw = await self._tools.current()
        validated = validate_deck_choice(
            call,
            task,
            delivered,
            raw,
            offer_ref=self._offer_ref,
            route_plan=self._plan,
            previewed_offer_ref=self._previewed_offer_ref,
        )
        if isinstance(validated, QueryToolResult):
            return validated
        plan, choices = validated
        self._choice = PendingCardChoice(call, choices, plan.resolved)
        return plan
