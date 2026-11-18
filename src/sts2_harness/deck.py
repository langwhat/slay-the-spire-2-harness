"""Focused card-reward input shared by replay and live decision owners."""

import json
from enum import StrEnum

from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.knowledge import DocumentView, GuidanceContent
from sts2_harness.llm.messages import TextMessage, TextRole, ToolDefinition
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.prompts import PromptReader
from sts2_harness.results import Outcome
from sts2_harness.tools import ToolName


class CardChoiceKind(StrEnum):
    TAKE = "take"
    SKIP = "skip"


class CardChoiceCompletion(native.WireModel):
    stream: str
    call_id: str
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
) -> Context:
    blocks: tuple[str | MaterialText, ...] = (
        prompts.read("deck-tasks"),
        "\n",
        prompts.read("deck"),
    )
    blocks += ("\n", prompts.read("state-format"))
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


def deck_tools(definitions: tuple[ToolDefinition, ...]) -> tuple[ToolDefinition, ...]:
    return tuple(tool for tool in definitions if tool.name == ToolName.RUN_DECK_CHOICE)


def deck_observation(
    observation: native.Observation, offer: native.Offer | None
) -> native.Observation:
    state = observation.state
    assert isinstance(state, native.RunState)
    actions = tuple(
        action for action in state.actions if offer is not None and action.ref in offer.actions
    )
    selection = state.selection if offer is not None else adding_selection(observation)
    if selection is not None and offer is not None:
        candidates = tuple(ref for ref in selection.candidates if ref in offer.contents)
        if not set(offer.contents).issubset(selection.candidates) or selection.min > len(
            candidates
        ):
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
    if offer is not None:
        refs.update((offer.ref, *offer.contents))
    if selection is not None:
        refs.update(selection.candidates)
    refs.update(action.subject for action in actions if isinstance(action.subject, str))
    identities = {
        entry.definition_id
        for ref, entry in observation.objects.items()
        if ref in refs and isinstance(entry.definition_id, str)
    }
    return observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "offers": (offer,) if offer is not None else (),
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
                if rule.owner in refs or rule.definition_id in identities
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
