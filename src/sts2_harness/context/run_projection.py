"""Native facts become complete sections and once-delivered stable definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import MISSING, JsonValue, TypeAdapter

from sts2_harness.client import models as native
from sts2_harness.context.map_projection import project_map
from sts2_harness.context.models import FinishedState, RunFinishedState, VisibleObservation
from sts2_harness.context.run_models import RunObservation
from sts2_harness.route_plan import RoutePlan

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _json(value: native.WireModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_json(value.to_json())


def _key(obj: native.ObjectEntry) -> str | None:
    if obj.definition_id is MISSING:
        return None
    if isinstance(obj, native.CardEntry) and obj.upgrades:
        return f"{obj.definition_id}+{obj.upgrades}"
    return obj.definition_id


def definition_references(value: JsonValue) -> set[str]:
    references: set[str] = set()
    if isinstance(value, list):
        for child in value:
            references.update(definition_references(child))
        return references
    if not isinstance(value, dict):
        return references
    identity = value.get("id")
    if isinstance(identity, str) and "name" in value:
        references.add(identity)
    for name, child in value.items():
        if name != "defs":
            references.update(definition_references(child))
    return references


def _card_definition(obj: native.CardEntry, text: str | None) -> dict[str, JsonValue]:
    raw = _json(obj)
    definition: dict[str, JsonValue] = {
        "id": _key(obj),
        "name": obj.name,
        "type": obj.card_type,
        "cost": _json(obj.cost),
        "text": text,
    }
    for key in ("card_pool", "rarity", "keywords", "is_upgradable", "is_removable"):
        if raw.get(key) not in (None, []):
            definition[key] = raw[key]
    return definition


@dataclass
class RunDelivery:
    role: native.DecisionOwner = native.DecisionOwner.RUN
    route_plan: RoutePlan | None = None
    definitions: dict[str, dict[str, JsonValue]] = field(
        default_factory=lambda: dict[str, dict[str, JsonValue]]()
    )
    delivered_defs: set[str] = field(default_factory=lambda: set[str]())
    sections: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())

    deck_stats: dict[str, JsonValue] | None = None
    deck_use: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    candidate_links: dict[str, tuple[dict[str, JsonValue], ...]] = field(
        default_factory=lambda: dict[str, tuple[dict[str, JsonValue], ...]]()
    )

    def snapshot(self) -> dict[str, JsonValue]:
        needed = definition_references(self.sections)
        definitions: list[JsonValue] = [
            value for key, value in self.definitions.items() if key in needed
        ]
        return {"baseline": True, "defs": definitions, **self.sections}

    def project(
        self, observation: VisibleObservation, source: native.Observation
    ) -> RunObservation:
        state = observation.state
        referenced: set[str] = set()

        def identity(ref: str) -> dict[str, JsonValue]:
            row: dict[str, JsonValue] = {"ref": ref}
            obj = observation.objects.get(ref)
            if obj is not None:
                row["name"] = obj.name
                if (key := _key(obj)) is not None:
                    row["id"] = key
                    referenced.add(key)
                    definition = (
                        _card_definition(obj, observation.descriptions.get(ref))
                        if isinstance(obj, native.CardEntry)
                        else {
                            "id": key,
                            "name": obj.name,
                            "type": obj.kind,
                            "text": observation.descriptions.get(ref),
                        }
                    )
                    self.definitions.setdefault(key, definition)
                return row
            if ref == "player":
                key = state.player.definition_id
                referenced.add(key)
                self.definitions.setdefault(key, {"id": key, "name": state.player.name})
                return {**row, "name": state.player.name, "id": key}
            if isinstance(state, native.CombatState):
                actors = (
                    *state.enemies,
                    *state.inactive_enemies,
                    *(state.allies if state.allies is not MISSING else ()),
                )
                for actor in actors:
                    if actor.ref == ref:
                        row["name"] = actor.name
                        if actor.definition_id is not MISSING:
                            key = actor.definition_id
                            row["id"] = key
                            referenced.add(key)
                            self.definitions.setdefault(key, {"id": key, "name": actor.name})
                        return row
            return row

        def object_row(
            ref: str, *, actionable: bool = True, text: bool = False
        ) -> dict[str, JsonValue]:
            obj = observation.objects.get(ref)
            row = identity(ref)
            if not actionable:
                row.pop("ref", None)
            if obj is None:
                return row
            key = _key(obj)
            if key is not None:
                row["id"] = key
                referenced.add(key)
            row["name"] = obj.name
            raw = _json(obj)
            if not isinstance(obj, native.CardEntry):
                row.update(
                    {k: v for k, v in raw.items() if k not in ("definition_id", "kind", "members")}
                )
                if isinstance(obj, native.BundleEntry):
                    row["members"] = [object_row(member, text=text) for member in obj.members]
                if ref in observation.descriptions:
                    row["text"] = observation.descriptions[ref]
                if key is not None and key not in self.definitions:
                    self.definitions[key] = {
                        "id": key,
                        "name": obj.name,
                        "type": obj.kind,
                        "text": observation.descriptions.get(ref),
                    }
                return row
            definition = _card_definition(obj, observation.descriptions.get(ref))
            if key is not None:
                self.definitions.setdefault(key, definition)
                known = self.definitions[key]
            else:
                known = definition
            if text:
                row.update(definition)
                if obj.definition_id in self.deck_use and not actionable:
                    row["use"] = {"observed_plays": self.deck_use[obj.definition_id]}
                if actionable and obj.definition_id in self.candidate_links:
                    row["links"] = list(self.candidate_links[obj.definition_id])
            else:
                for field_name, value in definition.items():
                    if field_name not in ("id", "name") and known.get(field_name) != value:
                        row[field_name] = value
            if obj.upgrades is None:
                row["upgrades"] = None
            # Current cost and playability can change without changing the definition.
            if actionable:
                row["cost"] = _json(obj.cost)
            if obj.playable is not MISSING and actionable:
                row["playable"] = obj.playable
            for field_name in (
                "local_keywords",
                "cost_without_global_effects",
                "should_retain_this_turn",
                "exhaust_on_next_play",
                "has_turn_end_in_hand_effect",
                "enchantment",
                "affliction",
            ):
                value = raw.get(field_name)
                if field_name == "local_keywords" and value == raw.get("keywords"):
                    continue
                if value is not None and value is not False and value != []:
                    row[field_name] = value
            modifier_text = observation.modifier_descriptions.get(ref)
            if modifier_text is not None:
                row["modifier_text"] = _json(modifier_text)
            if actionable and obj.previews is not MISSING:
                row["previews"] = [
                    {"target": identity(preview.target), "text": preview.text}
                    for preview in obj.previews
                ]
            return row

        def counts(refs: tuple[str, ...], *, text: bool = False) -> list[JsonValue]:
            groups: dict[str, dict[str, JsonValue]] = {}
            for ref in refs:
                row = object_row(ref, actionable=False, text=text)
                key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                if key not in groups:
                    groups[key] = {**row, "n": 0}
                count = groups[key]["n"]
                assert isinstance(count, int)
                groups[key]["n"] = count + 1
            return [groups[key] for key in sorted(groups)]

        sections: dict[str, JsonValue] = {
            "location": _json(source.location),
            "progress": _json(state.progress),
            "player": _json(state.player),
            "deck": counts(state.deck, text=self.role is native.DecisionOwner.RUN),
            "relics": [object_row(ref, actionable=False) for ref in state.relics],
            "potions": [object_row(ref) for ref in state.potions],
        }
        if self.role is native.DecisionOwner.RUN and self.deck_stats is not None:
            sections["deck_stats"] = self.deck_stats
        if self.role is native.DecisionOwner.RUN and source.map is not MISSING:
            topology, dynamic, odds = project_map(source.map, state.progress, self.route_plan)
            sections.update(
                map_topology=_json(topology),
                dynamic_map=_json(dynamic),
                unknown_odds={ref: _json(value) for ref, value in odds.items()},
            )
        if self.role is native.DecisionOwner.COMBAT and isinstance(state, native.CombatState):
            sections.update(
                turn=state.turn,
                hand=[object_row(ref) for ref in state.piles.hand],
                enemies=[_json(enemy) for enemy in state.enemies],
                inactive_enemies=[_json(enemy) for enemy in state.inactive_enemies],
            )
            for name in ("draw", "discard", "exhaust", "other"):
                sections[name] = counts(getattr(state.piles, name))
            if state.allies is not MISSING:
                sections["allies"] = [_json(ally) for ally in state.allies]
            if state.orbs is not MISSING:
                sections["orbs"] = _json(state.orbs)
        if isinstance(state, (native.TerminalState, FinishedState, RunFinishedState)):
            sections["result"] = state.result
            if state.last_observed_turn is not MISSING:
                sections["last_observed_turn"] = state.last_observed_turn
            if (
                isinstance(state, (native.TerminalState, FinishedState))
                and state.enemies is not MISSING
            ):
                sections["enemies"] = [_json(enemy) for enemy in state.enemies]
        if state.phase is native.Phase.COMBAT and source.encounter is not MISSING:
            sections["encounter"] = _json(source.encounter)
        same_node = self.sections.get("location") == _json(source.location)
        if same_node and state.phase is native.Phase.COMBAT:
            for name in ("encounter", "orbs"):
                if name not in sections and name in self.sections:
                    sections[name] = self.sections[name]
        choice: dict[str, JsonValue] = {
            "phase": state.phase,
            "screen": state.screen,
            "control": state.control,
            "actions": [],
            "selection": None,
        }
        if source.location.owner is self.role and isinstance(
            state, (native.RunState, native.CombatState)
        ):
            actions: list[JsonValue] = []
            for action in state.actions:
                if (
                    state.screen is native.RunScreen.MAP
                    and action.destination is not MISSING
                    and not action.enabled
                    and action.reason is MISSING
                ):
                    continue
                row = _json(action)
                if action.subject is not MISSING:
                    obj = observation.objects.get(action.subject)
                    row["subject"] = (
                        identity(action.subject)
                        if isinstance(obj, native.CardEntry)
                        else object_row(action.subject)
                    )
                if action.targets is not MISSING:
                    row["targets"] = [identity(ref) for ref in action.targets]
                actions.append(row)
            choice["actions"] = actions
            if state.selection is not None:
                selection = _json(state.selection)
                selection["candidates"] = [
                    object_row(ref, text=self.role is native.DecisionOwner.RUN)
                    for ref in state.selection.candidates
                ]
                selection["selected"] = list(state.selection.selected)
                choice["selection"] = selection
            if isinstance(state, native.RunState):
                offers: list[JsonValue] = []
                for offer in state.offers:
                    row = _json(offer)
                    row["candidates"] = [object_row(ref, text=True) for ref in offer.contents]
                    row.pop("contents")
                    offer_entry = observation.objects.get(offer.ref)
                    if offer_entry is not None:
                        row["name"] = offer_entry.name
                    offers.append(row)
                choice["offers"] = offers
                choice["event"] = (
                    {**_json(state.event), **object_row(state.event.ref)}
                    if state.event is not None
                    else None
                )
        sections["choice"] = choice
        sections["generated"] = [
            {
                "source": object_row(ref, actionable=False),
                "previews": [_json(card) for card in cards],
            }
            for ref, cards in observation.generated.items()
            if cards
        ]
        mechanisms: list[JsonValue] = []
        for mechanism in observation.mechanisms:
            body: dict[str, JsonValue] = _json(mechanism)
            key = mechanism.definition_id
            if key is not None:
                referenced.add(key)
                definition: dict[str, JsonValue] = {
                    "id": key,
                    "name": mechanism.name,
                    "type": mechanism.kind,
                    "text": mechanism.text,
                }
                self.definitions.setdefault(key, definition)
                body = {
                    "id": key,
                    "name": mechanism.name,
                    **(
                        {"text": mechanism.text}
                        if self.definitions[key].get("text") != mechanism.text
                        else {}
                    ),
                }
                if mechanism.owner is not MISSING:
                    body["owner"] = mechanism.owner
            mechanisms.append(body)
        sections["mechanisms"] = mechanisms
        baseline = not self.sections
        changed = {
            name: content
            for name, content in sections.items()
            if baseline or self.sections.get(name, MISSING) != content
        }
        changed.update({name: None for name in self.sections if name not in sections})
        additions: list[JsonValue] = [
            value
            for key, value in self.definitions.items()
            if key in referenced and key not in self.delivered_defs
        ]
        if additions:
            changed["defs"] = additions
        if baseline:
            changed["baseline"] = True
        self.delivered_defs.update(referenced)
        self.sections = sections
        return RunObservation.model_validate_json(json.dumps(changed, ensure_ascii=False))
