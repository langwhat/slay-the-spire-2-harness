"""Role views preserve native facts while hiding the other role's menus."""

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.context.models import (
    FinishedState,
    ForeignCombatState,
    ForeignRunState,
    RunFinishedState,
    ShortState,
    VisibleObservation,
)


def _run_player(player: native.CombatPlayer | native.Player) -> native.Player:
    return native.Player(
        name=player.name,
        definition_id=player.definition_id,
        hp=player.hp,
        max_hp=player.max_hp,
        gold=player.gold,
        potion_slot_count=player.potion_slot_count,
        empty_potion_slot_count=player.empty_potion_slot_count,
        base_orb_slots=player.base_orb_slots if isinstance(player, native.Player) else MISSING,
    )


def _short_state(
    observation: native.Observation, own_menu: bool, role: native.DecisionOwner
) -> ShortState:
    state = observation.state
    if own_menu:
        return state
    if isinstance(state, native.RunState):
        return ForeignRunState(
            phase=state.phase,
            screen=state.screen,
            control=state.control,
            progress=state.progress,
            player=state.player,
            relics=state.relics,
            potions=state.potions,
            deck=state.deck,
            event=state.event,
        )
    if isinstance(state, native.CombatState):
        return ForeignCombatState(
            phase=state.phase,
            screen=state.screen,
            control=state.control,
            progress=state.progress,
            player=_run_player(state.player),
            relics=state.relics,
            potions=state.potions,
            deck=state.deck,
            encounter=observation.encounter,
        )
    if role is native.DecisionOwner.RUN:
        return RunFinishedState(
            phase=state.phase,
            screen=state.screen,
            control=state.control,
            progress=state.progress,
            player=_run_player(state.player),
            relics=state.relics,
            potions=state.potions,
            result=state.result,
            deck=state.deck,
            last_observed_turn=state.last_observed_turn,
        )
    return FinishedState(
        phase=state.phase,
        screen=state.screen,
        control=state.control,
        progress=state.progress,
        player=state.player,
        relics=state.relics,
        potions=state.potions,
        result=state.result,
        deck=state.deck,
        last_observed_turn=state.last_observed_turn,
        enemies=state.enemies,
    )


def _menu_refs(observation: native.Observation) -> set[str]:
    state = observation.state
    retained = set((*state.relics, *state.potions))
    excluded = {action.subject for action in state.actions if action.subject is not MISSING}
    if state.selection is not None:
        excluded.update(state.selection.candidates)
    if isinstance(state, native.RunState):
        retained.update(state.deck)
        for offer in state.offers:
            excluded.add(offer.ref)
            excluded.update(offer.contents)
    if isinstance(state, native.CombatState):
        piles = state.piles
        retained.update((*piles.hand, *piles.draw, *piles.discard, *piles.exhaust, *piles.other))
        retained.update(enemy.ref for enemy in (*state.enemies, *state.inactive_enemies))
        if state.allies is not MISSING:
            retained.update(ally.ref for ally in state.allies)
    for ref, entry in observation.objects.items():
        if isinstance(entry, (native.OfferEntry, native.OptionEntry, native.EventEntry)):
            excluded.add(ref)
        if isinstance(entry, native.BundleEntry):
            excluded.add(ref)
            excluded.update(entry.members)
    return excluded - retained


def _native_ids(
    objects: native.Directory[native.ObjectEntry], state: native.State, *, actors: bool = True
) -> set[str]:
    identities: set[str] = set()
    for entry in objects.values():
        if entry.definition_id is not MISSING:
            identities.add(entry.definition_id)
        if isinstance(entry, native.CardEntry):
            identities.update(entry.keywords)
            if entry.local_keywords is not MISSING:
                identities.update(entry.local_keywords)
            for modifier in (entry.enchantment, entry.affliction):
                if modifier is not None:
                    identities.add(modifier.definition_id)
    if not actors:
        return identities
    if isinstance(state, native.TerminalState):
        if state.player.powers is not MISSING:
            identities.update(power.definition_id for power in state.player.powers)
        if state.enemies is not MISSING:
            for enemy in state.enemies:
                identities.update(power.definition_id for power in enemy.powers)
    if not isinstance(state, native.CombatState):
        return identities
    identities.update(power.definition_id for power in state.player.powers)
    for actor in (*state.enemies, *state.inactive_enemies):
        identities.update(power.definition_id for power in actor.powers)
    if state.allies is not MISSING:
        for ally in state.allies:
            identities.update(power.definition_id for power in ally.powers)
    if state.orbs is not MISSING:
        identities.update(
            orb.definition_id for orb in state.orbs.slots if orb.definition_id is not MISSING
        )
    return identities


def visible_observation(
    observation: native.Observation, role: native.DecisionOwner
) -> VisibleObservation:
    own_menu = observation.location.owner is role
    state = observation.state
    if role is native.DecisionOwner.RUN and isinstance(
        state, (native.CombatState, native.TerminalState)
    ):
        retained = set((*state.deck, *state.relics, *state.potions))
        objects = {
            ref: entry.model_copy(update={"previews": MISSING})
            if isinstance(entry, native.CardEntry)
            else entry
            for ref, entry in observation.objects.items()
            if ref in retained
        }
        identities = _native_ids(objects, state, actors=False)
        mechanisms = tuple(
            entry
            for entry in observation.mechanisms
            if (
                entry.owner in retained
                if entry.owner is not MISSING
                else entry.definition_id in identities
            )
        )
        return VisibleObservation(
            state=_short_state(observation, False, role),
            objects=objects,
            descriptions={
                ref: text for ref, text in observation.descriptions.items() if ref in retained
            },
            modifier_descriptions={
                ref: text
                for ref, text in observation.modifier_descriptions.items()
                if ref in retained
            },
            upgrades={
                ref: preview for ref, preview in observation.upgrades.items() if ref in retained
            },
            generated={
                ref: cards for ref, cards in observation.generated.items() if ref in retained
            },
            mechanisms=mechanisms,
            map=observation.map,
        )
    excluded = set[str]() if own_menu else _menu_refs(observation)
    objects = {
        ref: entry
        for ref, entry in observation.objects.items()
        if ref not in excluded
        and not (
            role is native.DecisionOwner.RUN
            and isinstance(entry, (native.EnemyEntry, native.AllyEntry))
        )
    }
    identities = _native_ids(objects, observation.state)
    mechanisms = tuple(
        entry
        for entry in observation.mechanisms
        if own_menu
        or (
            entry.owner not in excluded
            if entry.owner is not MISSING
            else entry.definition_id in identities
        )
    )
    return VisibleObservation(
        state=_short_state(observation, own_menu, role),
        objects=objects,
        descriptions={
            ref: text for ref, text in observation.descriptions.items() if ref not in excluded
        },
        modifier_descriptions={
            ref: text
            for ref, text in observation.modifier_descriptions.items()
            if ref not in excluded
        },
        upgrades={
            ref: preview for ref, preview in observation.upgrades.items() if ref not in excluded
        },
        generated={
            ref: cards for ref, cards in observation.generated.items() if ref not in excluded
        },
        mechanisms=mechanisms,
        map=observation.map if role is native.DecisionOwner.RUN else MISSING,
    )
