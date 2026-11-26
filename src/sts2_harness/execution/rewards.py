"""Resolve reward choices against the native state at each input boundary."""

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.decisions import (
    ActionKind,
    ActionStep,
    CardRewardPlan,
    RerollCardRewardStep,
    SelectStep,
)
from sts2_harness.deck import adding_selection, card_rewards, same_card_options
from sts2_harness.results import ActionStopped, Reason, ReasonCode, RewardBatchStopped, Suggestion


def reward_stopped(completed: int) -> ActionStopped:
    return ActionStopped(
        completed_steps=completed,
        next_step=completed + 1,
        reason=Reason(
            code=ReasonCode.REFERENCE_EXPIRED,
            message="The card reward groups or their available choices changed.",
        ),
        suggested_actions=(Suggestion(message="Review the current groups and submit a new plan."),),
    )


def reward_batch_stopped(completed_groups: int) -> RewardBatchStopped:
    return RewardBatchStopped(
        completed_groups=completed_groups,
        next_group=completed_groups + 1,
        reason=Reason(
            code=ReasonCode.REFERENCE_EXPIRED,
            message="The card reward groups or their available choices changed.",
        ),
        suggested_actions=(Suggestion(message="Review the current groups and submit a new plan."),),
    )


def reward_steps(
    plan: CardRewardPlan, index: int, observation: native.Observation
) -> tuple[ActionStep | SelectStep | RerollCardRewardStep, ...] | None:
    state = observation.state
    if (
        observation.location != plan.before.location
        or not isinstance(state, native.RunState)
        or state.control is not native.StateControl.READY
        or observation.input_readiness
        not in (native.InputReadiness.READY, native.InputReadiness.SELECTION)
    ):
        return None
    choice = plan.choices[index]
    if choice.offer is None:
        if choice.reroll:
            return None
        selection = adding_selection(observation)
        if selection != adding_selection(plan.before) or selection is None:
            return None
        return (
            (SelectStep(choice.refs),)
            if same_card_options(plan.before, observation, selection.candidates)
            else None
        )
    ignored = plan.resolved | frozenset(
        previous.offer.ref
        for previous in plan.choices[:index]
        if previous.offer is not None and not previous.reroll
    )
    actual, active = card_rewards(observation)
    actual_by_ref = {offer.ref: offer for offer in actual if offer.ref not in ignored}
    remaining = tuple(item.offer for item in plan.choices[index:] if item.offer is not None)
    if choice.reroll:
        if choice.offer.ref not in actual_by_ref:
            return None
        published = actual_by_ref[choice.offer.ref]
        if (
            set(published.contents) != set(choice.offer.contents)
            or published.requires_selection != choice.offer.requires_selection
            or published.can_reroll is not True
            or not same_card_options(plan.before, observation, choice.offer.contents)
        ):
            return None
        if state.selection is not None:
            if active is None or active.ref != choice.offer.ref:
                return None
            return (RerollCardRewardStep(),)
        if choice.offer.requires_selection is not True:
            return None
        action_refs = published.actions
        opening = next(
            (action for action in state.actions if action.enabled and action.ref in action_refs),
            None,
        )
        if opening is None:
            return None
        return (ActionStep(ActionKind.INTERACT, opening.ref), RerollCardRewardStep())
    if set(actual_by_ref) != {offer.ref for offer in remaining}:
        return None
    for offer in remaining:
        published = actual_by_ref[offer.ref]
        if (
            set(published.contents) != set(offer.contents)
            or published.requires_selection != offer.requires_selection
            or published.can_reroll != offer.can_reroll
            or not same_card_options(plan.before, observation, offer.contents)
        ):
            return None
    if state.selection is not None:
        if (
            active is None
            or active.ref != choice.offer.ref
            or not state.selection.min <= len(choice.refs) <= state.selection.max
        ):
            return None
        return (SelectStep(choice.refs),)
    if choice.offer.requires_selection is MISSING:
        return None
    if not choice.offer.requires_selection and not choice.refs:
        return ()
    action_refs = actual_by_ref[choice.offer.ref].actions
    opening = next(
        (action for action in state.actions if action.enabled and action.ref in action_refs), None
    )
    if opening is None:
        return None
    first = ActionStep(ActionKind.INTERACT, opening.ref)
    return (first, SelectStep(choice.refs)) if choice.offer.requires_selection else (first,)
