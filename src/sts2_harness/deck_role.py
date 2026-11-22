"""Joint card reward decisions, with per-group native completion evidence."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import MISSING, ValidationError

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import (
    ActionDecision,
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
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.messages import ModelClient, ToolCall
from sts2_harness.prompts import PromptReader
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    CardRewardResult,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
)
from sts2_harness.route_plan import RoutePlan, reconcile_plan
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import RunDeckChoice, SkipCard, TakeCard
from sts2_harness.user_input_values import UserInputSource


@dataclass(frozen=True, slots=True)
class PendingCardChoice:
    call: ToolCall
    choices: tuple[TakeCard | SkipCard, ...]


@dataclass(frozen=True, slots=True)
class CardResolution:
    progress: AutomaticProgress
    result: ActionToolResult
    resolved_offer: str | None = None


class DeckRole(DecisionSource, Protocol):
    def take_resolutions(self) -> tuple[CardResolution, ...]: ...

    def offered(self, observation: native.Observation) -> bool: ...

    def set_route_plan(self, plan: RoutePlan | None) -> None: ...


class DeckAgent(RunAgent):
    def __init__(
        self,
        client: Callable[[], ModelClient],
        tools: ToolCatalog,
        recovery: ToolRecoveryBudget,
        *,
        offer_ref: str | None = None,
        resolved: frozenset[str] = frozenset(),
        stream: str = "deck:replay",
        prompts: PromptReader | None = None,
        materials: MaterialReader | None = None,
        completion: Callable[[CardChoiceCompletion], None] | None = None,
        user_input: UserInputSource | None = None,
        diagnostics: Diagnostics | None = None,
    ) -> None:
        super().__init__(
            client,
            tools,
            recovery,
            prompts=prompts,
            materials=materials,
            user_input=user_input,
            diagnostics=diagnostics,
        )
        self._offer_ref = offer_ref
        self._stream = stream
        self._completion = completion
        self._choice: PendingCardChoice | None = None
        self._resolutions: list[CardResolution] = []
        self._resolved = set(resolved)
        if not self._accepts_tools(tools):
            raise ValueError("DeckAgent requires a focused Deck tool catalog")

    def _accepts_tools(self, tools: ToolCatalog) -> bool:
        return tuple(tool.name for tool in tools.definitions) == (ToolName.RUN_DECK_CHOICE,)

    def _offers(self, observation: native.Observation) -> tuple[native.Offer, ...]:
        offers, _ = card_rewards(observation)
        return tuple(
            offer
            for offer in offers
            if offer.ref not in self._resolved
            and (self._offer_ref is None or offer.ref == self._offer_ref)
        )

    def _reward_view(self, observation: native.Observation) -> native.Observation:
        if not isinstance(observation.state, native.RunState):
            return observation
        return deck_observation(observation, self._offers(observation))

    def set_route_plan(self, plan: RoutePlan | None) -> None:
        self._plan = plan

    async def _prepare(self, observation: native.Observation) -> Context:
        context = await super()._prepare(observation)
        context.set_route_plan(
            reconcile_plan(self.route_plan, observation.state.progress.position).plan
        )
        return context

    def offered(self, observation: native.Observation) -> bool:
        if adding_selection(observation) is not None:
            return True
        if not isinstance(observation.state, native.RunState):
            return False
        offers = self._offers(observation)
        _, active = card_rewards(observation)
        return bool(offers) and (observation.state.selection is None or active in offers)

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
        context.begin_group(observation, self._notes.read(), None)
        super()._append_task(context, task, observation)

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if not self.offered(observation):
            raise DecisionInterrupted(
                Reason(code=ReasonCode.ROLE_CHANGED, message="The card reward groups changed."),
                False,
            )
        observation = self._reward_view(observation)
        decision = await Agent.decide(self, task, observation)
        return (
            replace(decision, handoff_processed=False)
            if isinstance(decision, ActionDecision)
            else decision
        )

    async def _dispatch(
        self,
        call: ToolCall,
        task: DecisionTask,
        observation: native.Observation,
    ) -> ActionProposal | QueryToolResult:
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
        if completed and offer is not None:
            self._resolved.add(offer)
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
                offer if completed else None,
            )
        )

    async def accept(self, outcome: ActionToolResult) -> None:
        pending = self._choice
        assert pending is not None
        self._choice = None
        for index, result in enumerate(outcome.card_rewards, 1):
            self._record(pending.call, index, pending.choices[index - 1], result)
        await super().accept(outcome)

    async def _deck_choice(
        self,
        call: ToolCall,
        task: DecisionTask,
        delivered: native.Observation,
    ) -> ActionProposal | QueryToolResult:
        raw = await self._tools.current()
        if raw is None:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED, "The current rewards are unavailable.", None
            )
        current = self._reward_view(raw)
        if not isinstance(task, ActTask):
            return self._route_rejected(
                ReasonCode.WRONG_ROLE, "This task cannot take card rewards.", current
            )
        if raw.location != native.Location.model_validate(task.expected_route.model_dump()):
            return self._route_rejected(
                ReasonCode.ROLE_CHANGED, "The action route changed.", current
            )
        try:
            json.loads(call.arguments, object_pairs_hook=unique_fields)
            query = RunDeckChoice.model_validate_json(call.arguments, strict=True)
        except (ValueError, ValidationError):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "The deck choices do not match the tool schema.",
                current,
            )
        state = raw.state
        if (
            not isinstance(state, native.RunState)
            or state.control is not native.StateControl.READY
            or raw.input_readiness
            not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
        ):
            return self._route_rejected(
                ReasonCode.BUSY, "The current card choices are not ready.", current
            )
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
        if (
            delivered_state.offers != current_state.offers
            or delivered_state.selection != current_state.selection
            or not same_card_options(delivered, current, refs)
        ):
            return self._route_rejected(
                ReasonCode.REFERENCE_EXPIRED, "Review the updated card reward groups.", current
            )
        offers = {offer.ref: offer for offer in self._offers(raw)}
        _, active = card_rewards(raw)
        selection = adding_selection(raw)
        if selection is None and (
            set(item.offer for item in query.choices) != set(offers)
            or len(query.choices) != len(offers)
        ):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "Submit one choice for each remaining card reward.",
                current,
            )
        if selection is not None and (
            len(query.choices) != 1 or query.choices[0].offer is not MISSING
        ):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS,
                "Submit one choice without an offer for this selection.",
                current,
            )
        if (
            selection is None
            and state.selection is not None
            and (active is None or query.choices[0].offer != active.ref)
        ):
            return self._route_rejected(
                ReasonCode.PENDING_CHOICE, "Decide the open card reward first.", current
            )
        plan: list[CardRewardChoice] = []
        for item in query.choices:
            offer = None if item.offer is MISSING else offers[item.offer]
            chosen = item.cards if isinstance(item, TakeCard) else ()
            if offer is not None and offer.requires_selection is MISSING:
                return self._route_rejected(
                    ReasonCode.QUERY_FAILED,
                    "The Mod must publish the reward selection mode.",
                    current,
                )
            limits = selection if offer is None else state.selection if offer == active else None
            candidates = (
                limits.candidates if limits is not None else offer.contents if offer else ()
            )
            if (
                len(set(chosen)) != len(chosen)
                or not set(chosen) <= set(candidates)
                or (limits is not None and not limits.min <= len(chosen) <= limits.max)
                or (limits is None and len(chosen) > 1)
            ):
                return self._route_rejected(
                    ReasonCode.INVALID_ARGUMENTS,
                    "Use the card refs and limits of each current reward group.",
                    current,
                )
            plan.append(CardRewardChoice(offer, chosen, item.reason))
        self._choice = PendingCardChoice(call, query.choices)
        return CardRewardPlan(
            (),
            allow_map_travel=False,
            choices=tuple(plan),
            before=raw,
            resolved=frozenset(self._resolved),
        )
