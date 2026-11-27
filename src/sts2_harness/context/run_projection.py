"""Native facts become complete sections and once-delivered stable definitions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import MISSING, JsonValue, TypeAdapter

from sts2_harness.card_projection import DEFAULT_FALSE_CARD_FLAGS, card_rules, upgrade_changes
from sts2_harness.client import models as native
from sts2_harness.context.map_projection import (
    MapUnavailable,
    is_post_boss,
    planned_route,
    project_map,
    reachable_room_summary,
    upcoming_act_bosses,
    upcoming_act_elites,
)
from sts2_harness.context.models import FinishedState, RunFinishedState, VisibleObservation
from sts2_harness.context.run_models import RunObservation
from sts2_harness.fights import Fight, FightEncounter
from sts2_harness.note_values import NotesView
from sts2_harness.route_plan import RoutePlan

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def _json(value: native.WireModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT.validate_json(value.to_json())


def _slim(
    value: JsonValue,
    definitions: dict[str, dict[str, JsonValue]] | None = None,
    inherited: JsonValue = None,
) -> JsonValue:
    if isinstance(value, list):
        return [_slim(child, definitions) for child in value]
    if not isinstance(value, dict):
        return value
    identity = value.get("id")
    if definitions is not None and isinstance(identity, str):
        inherited = definitions.get(identity)
    known = inherited if isinstance(inherited, dict) else {}
    result: dict[str, JsonValue] = {}
    for name, child in value.items():
        if name in ("card_pool", "is_removable", "is_upgradable", "hp_display"):
            continue
        if name == "stars" and child == "none":
            continue
        if name == "display_amount" and child == value.get("amount"):
            continue
        if name == "enabled" and child is True:
            continue
        if name in ("is_used_up", "is_melted") and child is False:
            continue
        if name == "status" and child == native.RelicStatus.NORMAL:
            continue
        if name == "stack_count" and child == 1:
            continue
        previous = {**known, **value} if name == "upgrade" else known.get(name)
        compact = _slim(child, definitions, previous)
        if (
            name != "elites_fought"
            and (compact is None or compact == [] or compact == {})
            and (previous is None or previous == [] or previous == {})
        ):
            continue
        result[name] = compact
    return result


def generated_key(value: dict[str, JsonValue]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def mechanism_key(value: dict[str, JsonValue]) -> tuple[str, str, str | None, str | None]:
    kind, name, text = value.get("kind"), value.get("name"), value.get("text")
    definition_id = value.get("definition_id")
    completeness = value.get("completeness")
    assert isinstance(kind, str) and isinstance(name, str)
    assert text is None or isinstance(text, str)
    assert completeness is None or isinstance(completeness, str)
    identity = definition_id if isinstance(definition_id, str) else name
    return kind, identity, text, completeness


def _duplicates_definition(
    mechanism: native.MechanismView,
    definitions: dict[str, dict[str, JsonValue]],
    referenced: set[str],
) -> bool:
    if (
        mechanism.kind not in (native.MechanismKind.RELIC, native.MechanismKind.POTION)
        or mechanism.definition_id is None
        or mechanism.text is None
        or mechanism.definition_id not in referenced
        or mechanism.completeness is not MISSING
    ):
        return False
    definition = definitions.get(mechanism.definition_id)
    if definition is None:
        return False
    return (
        definition.get("type") == mechanism.kind
        and definition.get("name") == mechanism.name
        and definition.get("text") == mechanism.text
    )


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
        if name not in ("defs", "generated"):
            references.update(definition_references(child))
    return references


@dataclass
class RunDelivery:
    role: native.DecisionOwner = native.DecisionOwner.RUN
    focused_deck: bool = False
    route_plan: RoutePlan | None = None
    definitions: dict[str, dict[str, JsonValue]] = field(
        default_factory=lambda: dict[str, dict[str, JsonValue]]()
    )
    delivered_defs: set[str] = field(default_factory=lambda: set[str]())
    sections: dict[str, JsonValue] = field(default_factory=lambda: dict[str, JsonValue]())

    generated_entries: list[JsonValue] = field(default_factory=lambda: list[JsonValue]())
    delivered_generated: set[str] = field(default_factory=lambda: set[str]())

    mechanisms: list[JsonValue] = field(default_factory=lambda: list[JsonValue]())
    delivered_mechanisms: set[tuple[str, str, str | None, str | None]] = field(
        default_factory=lambda: set[tuple[str, str, str | None, str | None]]()
    )
    inline_card_ids: set[str] = field(default_factory=lambda: set[str]())

    deck_stats: dict[str, JsonValue] | None = None
    fights: tuple[Fight, ...] = ()
    elites_fought: tuple[FightEncounter, ...] | None = None
    deck_use: dict[str, int] = field(default_factory=lambda: dict[str, int]())

    notes: NotesView | None = None

    def snapshot(self) -> dict[str, JsonValue]:
        needed = definition_references(self.sections)
        definitions: list[JsonValue] = [
            value for key, value in self.definitions.items() if key in needed
        ]
        return {
            "baseline": True,
            "defs": [_slim(value) for value in definitions],
            **self.sections,
            **({"generated": self.generated_entries} if self.generated_entries else {}),
            **({"mechanisms": self.mechanisms} if self.mechanisms else {}),
        }

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
                    if isinstance(obj, native.CardEntry) and self.role is native.DecisionOwner.RUN:
                        self.inline_card_ids.add(key)
                        return row
                    referenced.add(key)
                    definition = (
                        card_rules(obj, observation.descriptions.get(ref))
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
            ref: str,
            *,
            actionable: bool = True,
            text: bool = False,
            candidate: bool = False,
            show_cost: bool = False,
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
                current_text = observation.descriptions.get(ref)
                known_text = self.definitions.get(key or "", {}).get("text")
                if current_text != known_text:
                    row["text"] = current_text
                if key is not None and key not in self.definitions:
                    self.definitions[key] = {
                        "id": key,
                        "name": obj.name,
                        "type": obj.kind,
                        "text": observation.descriptions.get(ref),
                    }
                return row
            definition = card_rules(obj, observation.descriptions.get(ref))
            if key is not None and self.role is native.DecisionOwner.COMBAT:
                self.definitions.setdefault(key, definition)
                known = self.definitions[key]
            else:
                known = definition
            if text:
                row.update(definition)
                if candidate and obj.rarity is not None:
                    row["rarity"] = obj.rarity
                if obj.definition_id in self.deck_use and not actionable:
                    row["use"] = {"observed_plays": self.deck_use[obj.definition_id]}
            else:
                for field_name, value in definition.items():
                    if field_name not in ("id", "name") and known.get(field_name) != value:
                        row[field_name] = value
            # Current cost and playability can change without changing the definition.
            if actionable:
                if text or show_cost or known.get("cost") != _json(obj.cost):
                    row["cost"] = _json(obj.cost)
            if obj.playable is not MISSING and actionable:
                row["playable"] = obj.playable
            if obj.needs_target is True and actionable:
                row["needs_target"] = True
            for field_name in (
                "local_keywords",
                "cost_without_global_effects",
                *DEFAULT_FALSE_CARD_FLAGS,
                "enchantment",
                "affliction",
            ):
                value = raw.get(field_name)
                if field_name == "local_keywords" and value == raw.get("keywords"):
                    continue
                if field_name == "cost_without_global_effects" and value == raw.get("cost"):
                    continue
                if value is not None and value is not False and value != []:
                    row[field_name] = value
            modifier_text = observation.modifier_descriptions.get(ref)
            if modifier_text is not None:
                row["modifier_text"] = _json(modifier_text)
            if ref in observation.upgrades and (actionable or text):
                row["upgrade"] = upgrade_changes(
                    obj, observation.descriptions.get(ref), observation.upgrades[ref]
                )
            if actionable and obj.previews is not MISSING:
                row["previews"] = [
                    {"target": identity(preview.target), "text": preview.text}
                    for preview in obj.previews
                    if preview.text != observation.descriptions.get(ref)
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
            "relics": [object_row(ref, actionable=False) for ref in state.relics],
            "potions": [object_row(ref) for ref in state.potions],
        }
        if isinstance(state, native.RunState):
            sections["card_rarity_offset"] = state.card_rarity_offset
        if self.role is native.DecisionOwner.RUN and self.notes is not None:
            sections["notes"] = _json(self.notes)
        if self.role is native.DecisionOwner.RUN:
            sections["deck"] = counts(state.deck, text=True)
        if self.role is native.DecisionOwner.RUN and self.deck_stats is not None:
            sections["deck_stats"] = self.deck_stats
        if self.role is native.DecisionOwner.RUN and self.fights:
            sections["fights"] = [_json(fight) for fight in self.fights]
        post_boss = isinstance(state, native.RunState) and is_post_boss(state.progress, source.map)
        if (
            self.role is native.DecisionOwner.RUN
            and not self.focused_deck
            and source.map is not MISSING
            and not post_boss
        ):
            try:
                projected = _json(project_map(source.map, state.progress, self.route_plan))
                if self.elites_fought is not None:
                    projected["elites_fought"] = [_json(elite) for elite in self.elites_fought]
                sections["map"] = projected
            except MapUnavailable:
                sections["map"] = None
        if self.role is native.DecisionOwner.COMBAT or self.focused_deck:
            if source.map is not MISSING:
                try:
                    route = planned_route(source.map, state.progress, self.route_plan)
                except MapUnavailable:
                    route = MISSING
                if route is not MISSING:
                    progress = sections["progress"]
                    assert isinstance(progress, dict)
                    progress["route"] = list(route)
            if self.focused_deck and isinstance(state, native.RunState):
                progress = sections["progress"]
                assert isinstance(progress, dict)
                upcoming = upcoming_act_bosses(source.map, state.progress)
                if upcoming:
                    progress["boss"] = [_json(boss) for boss in upcoming]
                elites = upcoming_act_elites(source.map, state.progress, self.route_plan)
                if elites:
                    progress["elite_pool"] = [_json(elite) for elite in elites]
                    if self.elites_fought is not None:
                        progress["elites_fought"] = [_json(elite) for elite in self.elites_fought]
                if "route" not in progress and source.map is not MISSING and not post_boss:
                    try:
                        reachable = reachable_room_summary(source.map, state.progress)
                    except MapUnavailable:
                        reachable = MISSING
                    if reachable is not MISSING:
                        progress["reachable_ahead"] = _json(reachable)
        if self.role is native.DecisionOwner.COMBAT:
            player = sections["player"]
            assert isinstance(player, dict)
            for name in ("gold", "potion_slot_count", "empty_potion_slot_count", "base_orb_slots"):
                player.pop(name, None)
        if self.role is native.DecisionOwner.COMBAT and isinstance(state, native.CombatState):
            sections.update(
                turn=state.turn,
                hand=[object_row(ref, text=True) for ref in state.piles.hand],
                enemies=[_json(enemy) for enemy in state.enemies],
                inactive_enemies=[_json(enemy) for enemy in state.inactive_enemies],
            )
            for name in ("draw", "discard", "exhaust", "other"):
                sections[name] = counts(getattr(state.piles, name))
            if state.allies is not MISSING:
                sections["allies"] = [_json(ally) for ally in state.allies]
            if state.orbs is not MISSING:
                sections["orbs"] = _json(state.orbs)
            if state.arrow_slots is not MISSING:
                sections["arrow_slots"] = [_json(slot) for slot in state.arrow_slots]
            if state.melodies is not MISSING:
                sections["melodies"] = list(state.melodies)
            if state.turn_activity is not MISSING:
                sections["turn_activity"] = _json(state.turn_activity)
            active_relics: list[JsonValue] = []
            for ref in state.relics:
                entry = observation.objects.get(ref)
                if (
                    isinstance(entry, native.RelicEntry)
                    and entry.status is native.RelicStatus.ACTIVE
                    and entry.is_melted is not True
                ):
                    row = object_row(ref, actionable=False)
                    row.pop("status", None)
                    active_relics.append(row)
            sections["active_relics"] = active_relics
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
                selection.pop("offer_ref", None)
                selection["candidates"] = [
                    identity(ref)
                    if isinstance(state, native.CombatState) and ref in state.piles.hand
                    else object_row(
                        ref,
                        text=self.role is native.DecisionOwner.RUN,
                        candidate=state.screen in (native.RunScreen.REWARDS, native.RunScreen.SHOP),
                        show_cost=isinstance(state, native.CombatState),
                    )
                    for ref in state.selection.candidates
                ]
                selection["selected"] = list(state.selection.selected)
                choice["selection"] = selection
            if isinstance(state, native.RunState):
                offers: list[JsonValue] = []
                for offer in state.offers:
                    row = _json(offer)
                    row.pop("parent_offer_ref", None)
                    row["candidates"] = [
                        object_row(
                            ref,
                            text=True,
                            candidate=state.screen
                            in (native.RunScreen.REWARDS, native.RunScreen.SHOP),
                        )
                        for ref in offer.contents
                    ]
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

        def canonical_source(ref: str) -> dict[str, JsonValue]:
            obj = observation.objects.get(ref)
            if obj is not None:
                key = _key(obj)
                return {"id": key, "name": obj.name} if key is not None else {"name": obj.name}
            if ref == "player":
                return {"id": state.player.definition_id, "name": state.player.name}
            if isinstance(state, native.CombatState):
                actors = (
                    *state.enemies,
                    *state.inactive_enemies,
                    *(state.allies if state.allies is not MISSING else ()),
                )
                for actor in actors:
                    if actor.ref == ref:
                        return (
                            {"id": actor.definition_id, "name": actor.name}
                            if actor.definition_id is not MISSING
                            else {"name": actor.name}
                        )
            return {"name": ref}

        generated: dict[str, dict[str, JsonValue]] = {}
        for ref, cards in observation.generated.items():
            if not cards:
                continue
            raw_entry: dict[str, JsonValue] = {
                "source": canonical_source(ref),
                "previews": [
                    {k: v for k, v in _json(card).items() if k != "rarity"} for card in cards
                ],
            }
            slimmed = _slim(raw_entry)
            assert isinstance(slimmed, dict)
            generated.setdefault(generated_key(slimmed), slimmed)
        self.generated_entries = list(generated.values())
        generated_additions: list[JsonValue] = [
            body for key, body in generated.items() if key not in self.delivered_generated
        ]
        self.delivered_generated.update(generated)
        covered_power_ids: set[str] = {
            identity
            for kind, identity, text, completeness in self.delivered_mechanisms
            if kind == native.MechanismKind.OTHER
            and identity.startswith("POWER.")
            and text is not None
            and completeness in (None, native.MechanismCompleteness.COMPLETE)
        }
        for mechanism in observation.mechanisms:
            if (
                mechanism.kind is native.MechanismKind.OTHER
                and mechanism.definition_id is not None
                and mechanism.definition_id.startswith("POWER.")
                and mechanism.text is not None
                and mechanism.completeness in (MISSING, native.MechanismCompleteness.COMPLETE)
            ):
                covered_power_ids.add(mechanism.definition_id)
        mechanisms: dict[tuple[str, str, str | None, str | None], dict[str, JsonValue]] = {}
        for mechanism in observation.mechanisms:
            if _duplicates_definition(mechanism, self.definitions, referenced):
                continue
            if (
                mechanism.kind is native.MechanismKind.POWER
                and mechanism.definition_id is not None
                and mechanism.definition_id in covered_power_ids
            ):
                continue
            body: dict[str, JsonValue] = {
                name: value
                for name, value in _json(mechanism).items()
                if name != "owner" and value is not None
            }
            mechanisms.setdefault(mechanism_key(body), body)
        self.mechanisms = list(mechanisms.values())
        mechanism_additions: list[JsonValue] = [
            body for key, body in mechanisms.items() if key not in self.delivered_mechanisms
        ]
        self.delivered_mechanisms.update(mechanisms)
        # Each ordinary section replaces its predecessor, including empty sections.
        sections = {
            name: value if name in ("notes", "fights", "map") else _slim(value, self.definitions)
            for name, value in sections.items()
        }
        baseline = not self.sections
        previous_turn = self.sections.get("turn")
        current_turn = sections.get("turn")
        turn_changed = (
            not baseline
            and self.role is native.DecisionOwner.COMBAT
            and isinstance(state, native.CombatState)
            and current_turn is not None
            and current_turn != previous_turn
        )
        previous_choice = self.sections.get("choice")
        previous_screen = (
            previous_choice.get("screen") if isinstance(previous_choice, dict) else None
        )
        changed = {
            name: content
            for name, content in sections.items()
            if baseline or self.sections.get(name, MISSING) != content
        }
        changed.update({name: None for name in self.sections if name not in sections})
        if (
            not baseline
            and self.role is native.DecisionOwner.RUN
            and isinstance(state, native.RunState)
            and state.screen is native.RunScreen.SHOP
            and previous_screen != native.RunScreen.SHOP
            and "deck_stats" not in changed
            and "deck_stats" in sections
        ):
            changed["deck_stats"] = sections["deck_stats"]
        if (
            turn_changed
            and "active_relics" not in changed
            and isinstance(sections.get("active_relics"), list)
            and sections["active_relics"]
        ):
            changed["active_relics"] = sections["active_relics"]
        additions: list[JsonValue] = [
            value
            for key, value in self.definitions.items()
            if key in referenced and key not in self.delivered_defs
        ]
        if additions:
            changed["defs"] = [_slim(value) for value in additions]
        if generated_additions:
            changed["generated"] = generated_additions
        if mechanism_additions:
            changed["mechanisms"] = [_slim(value) for value in mechanism_additions]
        if baseline:
            changed["baseline"] = True
        self.delivered_defs.update(referenced)
        self.sections = sections
        return RunObservation.model_validate_json(json.dumps(changed, ensure_ascii=False))
