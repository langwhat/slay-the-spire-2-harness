"""Focused card-reward input shared by replay and live decision owners."""

import json
from enum import StrEnum

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.map_projection import upcoming_act_bosses, upcoming_act_elites
from sts2_harness.context.view import ACT_MECHANISM_OWNER_PREFIX
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import TextMessage, TextRole
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.prompts import PromptReader, deck_act_prompt
from sts2_harness.results import Outcome
from sts2_harness.route_plan import RoutePlan


class CardChoiceKind(StrEnum):
    TAKE = "take"
    SKIP = "skip"
    REROLL = "reroll"


class CardChoiceCompletion(native.WireModel):
    stream: str
    call_id: str
    batch_index: int | None = None
    offer_ref: str | None
    choice: CardChoiceKind
    card_ref: str | tuple[str, ...] | None
    before: native.Observation
    after: native.Observation | None
    outcome: Outcome
    local_decline: bool = False


def deck_context(
    prompts: PromptReader,
    documents: tuple[DocumentView, ...],
    observation: native.Observation,
    *,
    minimal: bool,
    viewer_input: bool = False,
) -> Context:
    blocks: tuple[str | MaterialText, ...] = (
        prompts.read("deck-tasks"),
        "\n",
        prompts.read("run-report-reader"),
        "\n",
        prompts.read("state-format"),
        "\n",
        prompts.read("deck"),
        "\n",
        prompts.read(deck_act_prompt(observation.state.progress)),
        "\n",
        prompts.read("deck-choice"),
    )
    if viewer_input:
        blocks += ("\n", prompts.read("viewer-input"))
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
    return Context(native.DecisionOwner.RUN, system=system, minimal=minimal, focused_deck=True)


def card_rewards(
    observation: native.Observation,
) -> tuple[tuple[native.Offer, ...], native.Offer | None]:
    state = observation.state
    if not isinstance(state, native.RunState) or state.screen is not native.RunScreen.REWARDS:
        return (), None
    offers = tuple(offer for offer in state.offers if offer.kind is native.OfferKind.CARD)
    if state.selection is None:
        return offers, None
    if state.selection.offer_ref is not MISSING:
        logical = tuple(offer for offer in offers if offer.parent_offer_ref is MISSING)
        active = next(
            (offer for offer in logical if offer.ref == state.selection.offer_ref),
            None,
        )
        return logical, active
    candidates = set(state.selection.candidates)
    matching = tuple(offer for offer in offers if set(offer.contents) == candidates)
    if len(matching) == 2:
        # The open overlay is published first, followed by its underlying reward.
        overlay, active = matching
        return tuple(offer for offer in offers if offer.ref != overlay.ref), active
    return offers, matching[0] if len(matching) == 1 else None


def next_card_reward(
    observation: native.Observation, resolved: set[str] | frozenset[str]
) -> native.Offer | None:
    offers, active = card_rewards(observation)
    return next(
        (
            offer
            for offer in offers
            if offer.ref not in resolved
            and (observation.state.selection is None or offer == active)
        ),
        None,
    )


def same_card_options(
    before: native.Observation, current: native.Observation, refs: tuple[str, ...]
) -> bool:
    return all(
        before.objects.get(ref) == current.objects.get(ref)
        and before.descriptions.get(ref) == current.descriptions.get(ref)
        and before.upgrades.get(ref) == current.upgrades.get(ref)
        and before.modifier_descriptions.get(ref) == current.modifier_descriptions.get(ref)
        for ref in refs
    )


def deck_observation(
    observation: native.Observation,
    offers: native.Offer | tuple[native.Offer, ...] | None,
    *,
    route_plan: RoutePlan | None = None,
) -> native.Observation:
    state = observation.state
    assert isinstance(state, native.RunState)
    if isinstance(offers, native.Offer):
        offers = (offers,)
    offers = offers or ()
    action_refs = {ref for offer in offers for ref in offer.actions}
    actions = tuple(action for action in state.actions if action.ref in action_refs)
    selection = state.selection if offers else adding_selection(observation)
    if selection is not None and offers:
        candidates = tuple(
            ref for ref in selection.candidates if any(ref in offer.contents for offer in offers)
        )
        offer_matches = (
            any(selection.offer_ref in (offer.ref, offer.parent_offer_ref) for offer in offers)
            if selection.offer_ref is not MISSING
            else any(set(offer.contents) <= set(selection.candidates) for offer in offers)
        )
        if not offer_matches or selection.min > len(candidates):
            selection = None
        else:
            selection = selection.model_copy(
                update={
                    "candidates": candidates,
                    "selected": tuple(ref for ref in selection.selected if ref in candidates),
                    "max": min(selection.max, len(candidates)),
                }
            )
    refs = set((*state.deck, *state.relics, *state.potions))
    for offer in offers:
        refs.update((offer.ref, *offer.contents))
    if selection is not None:
        refs.update(selection.candidates)
    refs.update(action.subject for action in actions if isinstance(action.subject, str))
    identities = {
        entry.definition_id
        for ref, entry in observation.objects.items()
        if ref in refs and isinstance(entry.definition_id, str)
    }
    identities.update(
        boss.definition_id
        for boss in upcoming_act_bosses(observation.map, state.progress)
        if isinstance(boss.definition_id, str)
    )
    identities.update(
        elite.definition_id
        for elite in upcoming_act_elites(observation.map, state.progress, route_plan)
        if isinstance(elite.definition_id, str)
    )
    return observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "offers": offers,
                    "actions": actions,
                    "selection": selection,
                    "event": None,
                }
            ),
            "objects": {ref: entry for ref, entry in observation.objects.items() if ref in refs},
            "descriptions": {
                ref: text for ref, text in observation.descriptions.items() if ref in refs
            },
            "modifier_descriptions": {
                ref: text for ref, text in observation.modifier_descriptions.items() if ref in refs
            },
            "upgrades": {
                ref: preview for ref, preview in observation.upgrades.items() if ref in refs
            },
            "generated": {
                ref: cards for ref, cards in observation.generated.items() if ref in refs
            },
            "mechanisms": tuple(
                rule
                for rule in observation.mechanisms
                if rule.owner in refs
                or rule.definition_id in identities
                or (
                    isinstance(rule.owner, str)
                    and rule.owner.startswith(ACT_MECHANISM_OWNER_PREFIX)
                    and rule.owner.removeprefix(ACT_MECHANISM_OWNER_PREFIX) in identities
                )
            ),
        }
    )


def adding_selection(observation: native.Observation) -> native.Selection | None:
    state = observation.state
    if (
        not isinstance(state, native.RunState)
        or state.screen is not native.RunScreen.SELECTION
        or state.selection is None
        or not state.selection.candidates
    ):
        return None
    candidates = state.selection.candidates
    if any(
        ref in state.deck or not isinstance(observation.objects.get(ref), native.CardEntry)
        for ref in candidates
    ):
        return None
    return state.selection
