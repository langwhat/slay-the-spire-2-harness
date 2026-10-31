"""Projection computes a new delivery ledger without changing retained history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.context.models import (
    ActObservation,
    ForeignCombatState,
    Updates,
    VisibleObservation,
)


def _changed[T](current: Mapping[str, T], delivered: Mapping[str, T]) -> dict[str, T]:
    return {
        key: value
        for key, value in current.items()
        if key not in delivered or delivered[key] != value
    }


def _retain[T](previous: Mapping[str, T], current: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType({**previous, **current})


def _mechanisms(
    current: tuple[native.MechanismView, ...],
    shared: Mapping[tuple[native.MechanismKind, str], tuple[native.MechanismView, ...]],
    owners: Mapping[str, tuple[native.MechanismView, ...]],
) -> tuple[
    tuple[native.MechanismView, ...],
    Mapping[tuple[native.MechanismKind, str], tuple[native.MechanismView, ...]],
    Mapping[str, tuple[native.MechanismView, ...]],
]:
    by_owner: dict[str, tuple[native.MechanismView, ...]] = {}
    by_identity: dict[tuple[native.MechanismKind, str], tuple[native.MechanismView, ...]] = {}
    for mechanism in current:
        if mechanism.owner is not MISSING:
            owner = mechanism.owner
            by_owner[owner] = (*by_owner.get(owner, ()), mechanism)
            continue
        if mechanism.definition_id is None:
            continue
        identity = (mechanism.kind, mechanism.definition_id)
        by_identity[identity] = (*by_identity.get(identity, ()), mechanism)
    changed_owners = _changed(by_owner, owners)
    changed_shared = {
        key: entries for key, entries in by_identity.items() if shared.get(key) != entries
    }
    updates = tuple(
        mechanism
        for mechanism in current
        if (
            mechanism.owner in changed_owners
            if mechanism.owner is not MISSING
            else mechanism.definition_id is None
            or (mechanism.kind, mechanism.definition_id) in changed_shared
        )
    )
    return updates, MappingProxyType({**shared, **by_identity}), MappingProxyType(by_owner)


def _source_refs(observation: VisibleObservation) -> set[str]:
    refs = set(observation.objects)
    refs.add("player")
    state = observation.state
    if isinstance(state, native.CombatState):
        refs.update(actor.ref for actor in (*state.enemies, *state.inactive_enemies))
        if state.allies is not MISSING:
            refs.update(ally.ref for ally in state.allies)
    return refs


@dataclass(frozen=True, slots=True)
class Delivery:
    objects: Mapping[str, native.ObjectEntry]
    descriptions: Mapping[str, str | None]
    modifiers: Mapping[str, native.ModifierDescriptions]
    upgrades: Mapping[str, native.UpgradePreview]
    generated: Mapping[str, tuple[native.GeneratedCard, ...]]
    shared_mechanisms: Mapping[tuple[native.MechanismKind, str], tuple[native.MechanismView, ...]]
    owner_mechanisms: Mapping[str, tuple[native.MechanismView, ...]]
    node: tuple[str, str | None, str | None] | None = None
    map_delivered: bool = False
    encounter: native.NamedDefinition | MISSING = MISSING

    @classmethod
    def empty(cls) -> Delivery:
        return cls(
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
        )

    def project(
        self, observation: VisibleObservation, location: native.Location
    ) -> tuple[ActObservation, Delivery]:
        node = (
            location.game_instance_id,
            location.run_id,
            location.node_id,
        )
        previous = self
        if node != self.node:
            previous = Delivery(
                MappingProxyType({}),
                MappingProxyType({}),
                MappingProxyType({}),
                MappingProxyType({}),
                MappingProxyType({}),
                self.shared_mechanisms,
                MappingProxyType({}),
                node,
                self.map_delivered,
            )
        generated = {
            ref: observation.generated.get(ref, ())
            for ref in sorted(_source_refs(observation) | set(observation.generated))
        }
        generated_updates = {
            ref: cards
            for ref, cards in generated.items()
            if previous.generated.get(ref, ()) != cards
        }
        mechanisms, shared, owners = _mechanisms(
            observation.mechanisms, previous.shared_mechanisms, previous.owner_mechanisms
        )
        state = observation.state
        encounter: native.NamedDefinition | MISSING = MISSING
        if isinstance(state, ForeignCombatState):
            encounter = state.encounter if state.encounter is not MISSING else previous.encounter
            state = state.model_copy(update={"encounter": encounter})
        updates = Updates(
            objects=_changed(observation.objects, previous.objects) or MISSING,
            descriptions=_changed(observation.descriptions, previous.descriptions) or MISSING,
            modifier_descriptions=_changed(observation.modifier_descriptions, previous.modifiers)
            or MISSING,
            upgrades=_changed(observation.upgrades, previous.upgrades) or MISSING,
            generated=generated_updates or MISSING,
            mechanisms=mechanisms or MISSING,
            map=observation.map if not previous.map_delivered else MISSING,
        )
        delivered = Delivery(
            _retain(previous.objects, observation.objects),
            _retain(previous.descriptions, observation.descriptions),
            _retain(previous.modifiers, observation.modifier_descriptions),
            _retain(previous.upgrades, observation.upgrades),
            _retain(previous.generated, generated),
            shared,
            owners,
            node,
            previous.map_delivered or observation.map is not MISSING,
            encounter,
        )
        return ActObservation(state=state, updates=updates), delivered
