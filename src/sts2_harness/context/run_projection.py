"""Role-local catalogs and current non-map observations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import MISSING, JsonValue, TypeAdapter

from sts2_harness.client import models as native
from sts2_harness.context.entries import Collection, difference, ordered
from sts2_harness.context.map_projection import project_map
from sts2_harness.context.models import (
    FinishedState,
    RunFinishedState,
    VisibleObservation,
)
from sts2_harness.context.run_models import RunObservation
from sts2_harness.route_plan import RoutePlan

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _json(value: native.WireModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_json(value.to_json())


def _card_defaults(raw: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = dict(raw)
    if result.get("local_keywords") == result["keywords"]:
        result.pop("local_keywords", None)
    for key in ("should_retain_this_turn", "exhaust_on_next_play", "has_turn_end_in_hand_effect"):
        if result.get(key) is False:
            result.pop(key)
    for key in ("enchantment", "affliction"):
        if key in result and result[key] is None:
            result.pop(key)
    return result


def _identity(observation: VisibleObservation, ref: str) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"ref": ref}
    obj = observation.objects.get(ref)
    if obj is not None:
        result["name"] = obj.name
        if obj.definition_id is not MISSING:
            result["definition_id"] = obj.definition_id
    elif (
        isinstance(observation.state, native.CombatState)
        and (
            actor := next(
                (
                    a
                    for a in (
                        *observation.state.enemies,
                        *observation.state.inactive_enemies,
                        *(
                            observation.state.allies
                            if observation.state.allies is not MISSING
                            else ()
                        ),
                    )
                    if a.ref == ref
                ),
                None,
            )
        )
        is not None
    ):
        result["name"] = actor.name
        if actor.definition_id is not MISSING:
            result["definition_id"] = actor.definition_id
    elif ref == "player":
        result["name"] = observation.state.player.name
        result["definition_id"] = observation.state.player.definition_id
    return result


def _refs(observation: VisibleObservation, refs: tuple[str, ...]) -> list[JsonValue]:
    return [_identity(observation, ref) for ref in refs]


@dataclass
class RunDelivery:
    catalog: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    scene: dict[str, JsonValue] | None = None
    current: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())
    phase: native.Phase | None = None
    next_profile: int = 1
    role: native.DecisionOwner = native.DecisionOwner.RUN
    route_plan: RoutePlan | None = None
    actions: list[JsonValue] | None = None
    decision: dict[str, JsonValue] | None = None

    def project(
        self, observation: VisibleObservation, source: native.Observation
    ) -> RunObservation:
        state = observation.state
        location = source.location
        additions: dict[str, JsonValue] = {}

        def intern(kind: str, content: dict[str, JsonValue]) -> str:
            profile: dict[str, JsonValue] = {
                "kind": kind,
                "language": source.game_language,
                "schema": f"{kind}/1",
                "content": content,
            }
            key = json.dumps(profile, sort_keys=True, ensure_ascii=False)
            if key not in self.catalog:
                ident = f"p{self.next_profile}"
                self.next_profile += 1
                self.catalog[key] = ident
                additions[ident] = profile
            return self.catalog[key]

        bindings: list[JsonValue] = []
        cards: list[JsonValue] = []
        noncards: list[JsonValue] = []
        descriptions: dict[str, dict[str, JsonValue]] = {}
        object_descriptions: list[JsonValue] = []
        for ref, obj in observation.objects.items():
            raw = _json(obj)
            identity = _identity(observation, ref)
            content = {
                k: raw[k]
                for k in ("name", "definition_id", "upgrades", "card_type", "card_pool", "rarity")
                if k in raw
            }
            for key in ("enchantment", "affliction"):
                modifier = raw.get(key)
                if isinstance(modifier, dict):
                    content[key] = {k: modifier[k] for k in ("definition_id", "name")}
            profile = intern(obj.kind, content)
            bindings.append({**identity, "kind": obj.kind, "profile": profile})
            if isinstance(obj, native.CardEntry):
                keys = (
                    "keywords",
                    "local_keywords",
                    "is_upgradable",
                    "is_removable",
                    "cost",
                    "cost_without_global_effects",
                    "playable",
                    "should_retain_this_turn",
                    "exhaust_on_next_play",
                    "has_turn_end_in_hand_effect",
                    "enchantment",
                    "affliction",
                )
                live = {k: raw[k] for k in keys if k in raw}
                if (
                    self.role is native.DecisionOwner.COMBAT
                    and isinstance(state, native.CombatState)
                    and ref in state.piles.hand
                ):
                    live.pop("playable", None)
                live = _card_defaults({**live, "keywords": raw["keywords"]})
                cards.append({**identity, **live})
                if ref in observation.descriptions:
                    descriptions[ref] = {**identity, "text": observation.descriptions[ref]}
            else:
                if ref in observation.descriptions:
                    object_descriptions.append({**identity, "text": observation.descriptions[ref]})
                if isinstance(obj, (native.EnemyEntry, native.AllyEntry)):
                    continue
                if isinstance(obj, native.BundleEntry):
                    raw["members"] = _refs(observation, obj.members)
                noncards.append({"ref": ref, **raw})
        scene: dict[str, JsonValue] = {
            "game_instance_id": location.game_instance_id,
            "run_id": location.run_id,
            "node_id": location.node_id,
            "bindings": ordered(Collection.BINDINGS, bindings),
        }
        node_changed = self.scene is None or any(
            scene[k] != self.scene[k] for k in ("game_instance_id", "run_id", "node_id")
        )
        reset = node_changed
        mechanisms: list[JsonValue] = []
        for mechanism in observation.mechanisms:
            owner = (
                observation.objects.get(mechanism.owner) if mechanism.owner is not MISSING else None
            )
            full_object_rule = (
                mechanism.kind is native.MechanismKind.RELIC
                and isinstance(owner, native.RelicEntry)
            ) or (
                mechanism.kind is native.MechanismKind.POTION
                and isinstance(owner, native.PotionEntry)
            )
            if (
                full_object_rule
                and owner is not None
                and mechanism.definition_id == owner.definition_id
                and mechanism.name == owner.name
                and mechanism.text is not None
                and mechanism.owner is not MISSING
                and observation.descriptions.get(mechanism.owner) == mechanism.text
            ):
                continue
            body = _json(mechanism)
            body.pop("owner", None)
            profile = intern("mechanism", body)
            mechanisms.append(
                {
                    "owner": _identity(observation, mechanism.owner)
                    if mechanism.owner is not MISSING
                    else None,
                    "profile": profile,
                    "definition_id": mechanism.definition_id,
                    "name": mechanism.name,
                }
            )
        current: dict[str, JsonValue] = {
            "progress": _json(state.progress),
            "player": _json(state.player),
            "inventory": {
                "relics": _refs(observation, state.relics),
                "potions": _refs(observation, state.potions),
            },
            "deck": _refs(observation, state.deck),
            "cards": cards,
            "card_descriptions": list(descriptions.values()),
            "noncard_objects": noncards,
            "object_descriptions": object_descriptions,
            "modifier_descriptions": [
                {**_identity(observation, ref), **modifier_texts}
                for ref, value in observation.modifier_descriptions.items()
                if (
                    modifier_texts := {
                        key: text
                        for key, text in _json(value).items()
                        if not (
                            isinstance(observation.objects.get(ref), native.CardEntry)
                            and getattr(observation.objects[ref], key) is None
                            and text is None
                        )
                    }
                )
            ],
            "mechanisms": mechanisms,
            "upgrades": [
                {
                    "source": _identity(observation, ref),
                    **(
                        {**_json(value), "card": _card_defaults(_json(value.card))}
                        if isinstance(value, native.UpgradeAvailable)
                        else _json(value)
                    ),
                }
                for ref, value in observation.upgrades.items()
            ],
            "generated": [
                {
                    "source": _identity(observation, ref),
                    "previews": [
                        {**_json(value), "card": _card_defaults(_json(value.card))}
                        for value in values
                    ],
                }
                for ref, values in sorted(observation.generated.items())
                if values
            ],
        }
        if self.role is native.DecisionOwner.RUN and source.map is not MISSING:
            topology, dynamic, unknown_odds = project_map(
                source.map, state.progress, self.route_plan
            )
            current["map_topology"] = _json(topology)
            current["dynamic_map"] = _json(dynamic)
            current["unknown_odds"] = {ref: _json(odds) for ref, odds in unknown_odds.items()}
        if self.role is native.DecisionOwner.COMBAT and isinstance(state, native.CombatState):
            current["turn"] = state.turn
            hand: list[JsonValue] = []
            for ref in state.piles.hand:
                entry = _identity(observation, ref)
                obj = observation.objects.get(ref)
                if isinstance(obj, native.CardEntry):
                    if obj.playable is not MISSING:
                        entry["playable"] = obj.playable
                    if location.owner is self.role and obj.previews is not MISSING:
                        entry["previews"] = [
                            {"target": _identity(observation, preview.target), "text": preview.text}
                            for preview in obj.previews
                        ]
                hand.append(entry)
            current["hand"] = hand
            for key in ("draw", "discard", "exhaust", "other"):
                current[key] = _refs(observation, getattr(state.piles, key))
            actors: dict[str, JsonValue] = {
                "enemies": [_json(actor) for actor in state.enemies],
                "inactive_enemies": [_json(actor) for actor in state.inactive_enemies],
            }
            if state.allies is not MISSING:
                actors["allies"] = [_json(actor) for actor in state.allies]
            current["actors"] = actors
            if state.orbs is not MISSING:
                current["orbs"] = _json(state.orbs)
            elif not node_changed and self.phase is native.Phase.COMBAT and "orbs" in self.current:
                current["orbs"] = self.current["orbs"]
        if isinstance(state, (native.TerminalState, FinishedState, RunFinishedState)):
            current["result"] = state.result
            if state.last_observed_turn is not MISSING:
                current["last_observed_turn"] = state.last_observed_turn
        if source.encounter is not MISSING and state.phase is native.Phase.COMBAT:
            current["encounter"] = _json(source.encounter)
        elif (
            not node_changed
            and self.phase is native.Phase.COMBAT
            and state.phase is native.Phase.COMBAT
            and "encounter" in self.current
        ):
            current["encounter"] = self.current["encounter"]
        for collection in Collection:
            if collection is not Collection.BINDINGS:
                current[collection] = ordered(collection, current[collection])
        changed = {k: v for k, v in current.items() if reset or self.current.get(k, MISSING) != v}
        previous_progress = self.current.get("progress")
        same_act = (
            self.scene is not None
            and all(scene[k] == self.scene[k] for k in ("game_instance_id", "run_id"))
            and isinstance(previous_progress, dict)
            and previous_progress.get("act") == state.progress.act
        )
        for section in ("map_topology", "dynamic_map", "unknown_odds"):
            if section not in current:
                continue
            if same_act and current[section] == self.current.get(section):
                changed.pop(section, None)
            else:
                changed[section] = current[section]
        changed.update({k: None for k in self.current if k not in current})
        if not reset:
            removals: dict[str, JsonValue] = {}
            for collection in Collection:
                if collection is Collection.BINDINGS:
                    continue
                updates, removed = difference(
                    collection, self.current[collection], current[collection]
                )
                changed.pop(collection, None)
                if updates:
                    changed[collection] = updates
                if removed:
                    removals[collection] = removed
            if removals:
                changed["removed"] = removals
        actions: list[JsonValue] = []
        decision: dict[str, JsonValue] = {
            "phase": state.phase,
            "screen": state.screen,
            "control": state.control,
            "actions": actions,
            "selection": None,
        }
        if location.owner is self.role and isinstance(state, (native.RunState, native.CombatState)):
            for action in state.actions:
                if (
                    state.screen is native.RunScreen.MAP
                    and action.destination is not MISSING
                    and not action.enabled
                    and action.reason is MISSING
                ):
                    continue
                value = _json(action)
                if action.subject is not MISSING:
                    subject = _identity(observation, action.subject)
                    if "name" in subject:
                        value["subject"] = subject
                if action.targets is not MISSING:
                    value["targets"] = _refs(observation, action.targets)
                actions.append(value)
            if state.selection is not None:
                selection = _json(state.selection)
                selection["candidates"] = _refs(observation, state.selection.candidates)
                selection["selected"] = _refs(observation, state.selection.selected)
                decision["selection"] = selection
            if isinstance(state, native.RunState):
                offers: list[JsonValue] = []
                for offer in state.offers:
                    value = _json(offer)
                    value["ref"] = _identity(observation, offer.ref)
                    value["contents"] = _refs(observation, offer.contents)
                    offers.append(value)
                decision["offers"] = offers
                decision["event"] = _json(state.event) if state.event is not None else None
        complete_decision = dict(decision)
        if self.role is native.DecisionOwner.RUN and not reset and actions == self.actions:
            decision.pop("actions")
        wire: dict[str, JsonValue] = {"decision": decision}
        if additions:
            wire["catalog_additions"] = additions
        if reset or scene != self.scene:
            scene_update = dict(scene)
            if reset:
                scene_update["reset"] = True
            else:
                assert self.scene is not None
                updates, removed = difference(
                    Collection.BINDINGS, self.scene["bindings"], scene["bindings"]
                )
                scene_update["bindings"] = updates
                if removed:
                    scene_update["removed"] = removed
            wire["scene"] = scene_update
        if changed:
            wire["current"] = changed
        result = RunObservation.model_validate_json(json.dumps(wire, ensure_ascii=False))
        self.scene = scene
        self.current = current
        self.phase = state.phase
        self.actions = actions
        self.decision = complete_decision
        return result
