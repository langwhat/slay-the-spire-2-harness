"""One focused card reward; native execution and shared notes retain their owners."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace

from pydantic import MISSING, ValidationError

from sts2_harness.agent import Agent, MaterialReader
from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import AutomaticProgress
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    Decision,
    DecisionTask,
    LocalProgress,
    SelectStep,
)
from sts2_harness.deck import (
    CardChoiceCompletion,
    CardChoiceKind,
    deck_context,
    deck_observation,
    deck_tools,
)
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.knowledge import CandidateLinks, DocumentView
from sts2_harness.llm.messages import ModelClient, ToolCall
from sts2_harness.prompts import PromptReader
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    QueryToolResult,
    ReadCompleted,
    ReasonCode,
)
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog, ToolName, unique_fields
from sts2_harness.tools.inputs import RunDeckChoice, SkipCard, TakeCard
from sts2_harness.user_input_values import UserInputSource


@dataclass(frozen=True, slots=True)
class PendingCardChoice:
    call: ToolCall
    query: TakeCard | SkipCard
    before: native.Observation


@dataclass(frozen=True, slots=True)
class CardResolution:
    progress: AutomaticProgress
    result: ActionToolResult
    declined_offer: str | None = None


class DeckAgent(RunAgent):
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
        candidate_links: CandidateLinks | None = None,
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
            candidate_links=candidate_links,
            user_input=user_input,
            diagnostics=diagnostics,
        )
        self._offer_ref = offer_ref
        self._stream = stream
        self._completion = completion
        self._choice: PendingCardChoice | None = None
        self.resolution: CardResolution | None = None
        self.feedback: CardResolution | None = None
        tools.definitions = deck_tools(tools.definitions)

    def _reward_view(self, observation: native.Observation) -> native.Observation:
        state = observation.state
        if not isinstance(state, native.RunState) or self._offer_ref is None:
            return observation
        offer = next((offer for offer in state.offers if offer.ref == self._offer_ref), None)
        return deck_observation(observation, offer)

    def offered(self, observation: native.Observation) -> bool:
        state = observation.state
        return (
            isinstance(state, native.RunState)
            and state.screen is native.RunScreen.REWARDS
            and any(offer.ref == self._offer_ref for offer in state.offers)
        )

    def _initial_context(
        self,
        documents: tuple[DocumentView, ...],
        observation: native.Observation,
    ) -> Context:
        return deck_context(self._prompts, documents, observation, minimal=self._tools.minimal)

    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        observation = self._reward_view(observation)
        context = await self._prepare(observation)
        context.begin_group(observation, self._notes.read(), None)
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
    ) -> ActionProposal | QueryToolResult | LocalProgress:
        if call.name == ToolName.RUN_DECK_CHOICE:
            return await self._deck_choice(call, task)
        return await self._tools.dispatch(call)

    def _record(
        self,
        pending: PendingCardChoice,
        result: ActionToolResult | QueryToolResult,
        *,
        local: bool = False,
    ) -> None:
        if self._completion is not None:
            self._completion(
                CardChoiceCompletion(
                    stream=self._stream,
                    call_id=pending.call.id,
                    offer_ref=pending.query.offer,
                    choice=CardChoiceKind.TAKE
                    if isinstance(pending.query, TakeCard)
                    else CardChoiceKind.SKIP,
                    card_ref=pending.query.card if isinstance(pending.query, TakeCard) else None,
                    before=pending.before,
                    after=result.observation,
                    outcome=result.outcome,
                    local_decline=local,
                )
            )

    def _resolved(self, pending: PendingCardChoice, result: ActionToolResult) -> None:
        query = pending.query
        label = "Skip"
        if isinstance(query, TakeCard):
            card = pending.before.objects[query.card]
            assert isinstance(card, native.CardEntry)
            label = card.definition_id
        self.resolution = CardResolution(
            AutomaticProgress(label=label, reason=query.reason),
            result,
            query.offer if isinstance(query, SkipCard) else None,
        )
        if self._tools.deck_stats is not None:
            self._tools.deck_stats.record_choice(pending.before, taken=isinstance(query, TakeCard))

    async def accept(self, outcome: ActionToolResult) -> None:
        pending = self._choice
        assert pending is not None
        self._choice = None
        self._record(pending, outcome)
        query = pending.query
        label = "Skip"
        if isinstance(query, TakeCard):
            card = pending.before.objects[query.card]
            assert isinstance(card, native.CardEntry)
            label = card.definition_id
        self.feedback = CardResolution(AutomaticProgress(label=label, reason=query.reason), outcome)
        if isinstance(outcome.outcome, ActionCompleted):
            self._resolved(pending, outcome)
        await super().accept(outcome)

    async def _deck_choice(
        self,
        call: ToolCall,
        task: DecisionTask,
    ) -> ActionProposal | QueryToolResult | LocalProgress:
        raw = await self._tools.current()
        if raw is None:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED, "The current reward is unavailable.", None
            )
        current = self._reward_view(raw)
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
            query = RunDeckChoice.model_validate_json(call.arguments, strict=True).root
        except (ValueError, ValidationError):
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS, "The deck choice does not match its schema.", current
            )
        state = current.state
        if (
            isinstance(raw.state, native.RunState)
            and raw.state.selection is not None
            and isinstance(state, native.RunState)
            and state.selection is None
        ):
            return self._route_rejected(
                ReasonCode.PENDING_CHOICE, "Another selection is open.", current
            )
        offer = (
            next((o for o in state.offers if o.ref == query.offer), None)
            if isinstance(state, native.RunState) and state.screen is native.RunScreen.REWARDS
            else None
        )
        if offer is None or offer.kind is not native.OfferKind.CARD:
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS, "The offer is not a current card reward.", current
            )
        assert isinstance(state, native.RunState)
        if state.control is not native.StateControl.READY or current.input_readiness not in (
            native.InputReadiness.READY,
            native.InputReadiness.SELECTION,
        ):
            return self._route_rejected(
                ReasonCode.BUSY, "The current card choice is not ready.", current
            )
        if offer.requires_selection is MISSING:
            return self._route_rejected(
                ReasonCode.QUERY_FAILED,
                "The Mod must publish this card reward's selection mode.",
                current,
            )
        if isinstance(query, TakeCard) and query.card not in offer.contents:
            return self._route_rejected(
                ReasonCode.INVALID_ARGUMENTS, "The taken card must belong to this offer.", current
            )
        pending = PendingCardChoice(call, query, raw)
        chosen = (query.card,) if isinstance(query, TakeCard) else ()
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
            proposal = ActionProposal((SelectStep(chosen),), query.reason, allow_map_travel=False)
        elif not offer.requires_selection and not chosen:
            result = QueryToolResult(ReadCompleted(), current)
            self._record(pending, QueryToolResult(ReadCompleted(), raw), local=True)
            self._resolved(pending, ActionToolResult(ActionCompleted(completed_steps=0), raw))
            return LocalProgress(result)
        else:
            opening = next((a for a in state.actions if a.ref in offer.actions and a.enabled), None)
            if opening is None:
                return self._route_rejected(
                    ReasonCode.ILLEGAL_ACTION, "The card reward cannot be opened now.", current
                )
            steps = (ActionStep(ActionKind.INTERACT, opening.ref),)
            proposal = ActionProposal(
                steps if not offer.requires_selection else (*steps, SelectStep(chosen)),
                query.reason,
                allow_map_travel=False,
            )
        self._choice = pending
        return proposal
