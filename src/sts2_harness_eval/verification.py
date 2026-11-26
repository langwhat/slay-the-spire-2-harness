"""Verify public correspondence before deriving or consuming native object bindings."""

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import JsonValue, TypeAdapter

from sts2_harness.client import models as native
from sts2_harness.deck import card_rewards
from sts2_harness_eval.checkpoints import (
    CardKey,
    CreatureKey,
    IdentityRead,
    MapPointKey,
    NativeKey,
    PotionSlotKey,
    RelicSlotKey,
    RewardKey,
)


class Relation(StrEnum):
    SOURCE_REPLAY = "R1"
    REPLAY_LOAD = "R2"


class VerificationFailure(Exception):
    def __init__(
        self,
        relation: Relation,
        field_path: str,
        *,
        unsupported: bool = False,
        sequence: int | None = None,
    ) -> None:
        self.relation = relation
        self.field_path = field_path
        self.unsupported = unsupported
        self.sequence = sequence
        super().__init__(field_path)


class RefBinding(native.WireModel):
    source_ref: str
    native_key: NativeKey


class KeyedRefs(native.WireModel):
    source_sequence: int
    refs: tuple[RefBinding, ...]


class Publication(native.WireModel):
    source_sequence: int
    source_stamp: native.Stamp
    read: IdentityRead


# A map frontier loaded from a save starts without the room that preceded it, so the loaded
# observation reports no room type where the recording named one; nothing else may differ.
LOADABLE_ROOM_TYPES = ("monster", "elite", "boss", "event", "restsite", "shop", "treasure")


class AllowedMapDifference(native.WireModel):
    source_sequence: int
    field_path: str = "state.progress.room_type"
    before: str
    after: None = None


class Reference(native.WireModel):
    boundary: Publication
    suffix: tuple[Publication, ...]
    target: Publication
    follow_on_before: tuple[Publication, ...]
    follow_on: tuple[Publication, ...]


_REF = re.compile(r"^([a-z]+):(\d+):(\d+)$")
_JSON = TypeAdapter(JsonValue)
_REF_FIELDS = frozenset(
    (
        "ref",
        "card",
        "target",
        "potion",
        "action",
        "subject",
        "refs",
        "candidates",
        "selected",
        "deck",
        "relics",
        "potions",
        "hand",
        "draw",
        "discard",
        "exhaust",
        "other",
        "contents",
        "actions",
        "targets",
        "owner",
        "impaled_target_ref",
    )
)


def _value(observation: native.Observation) -> JsonValue:
    return _JSON.validate_json(observation.to_json())


# Fields that hold either an object ref or the literal "player".
_PLAYER_OR_REF_FIELDS = frozenset(("owner", "impaled_target_ref"))


def _refs(value: JsonValue, field: str = "") -> set[str]:
    if isinstance(value, str):
        return (
            {value}
            if field in _REF_FIELDS
            and (field not in _PLAYER_OR_REF_FIELDS or _REF.fullmatch(value))
            else set()
        )
    if isinstance(value, list):
        return {ref for item in value for ref in _refs(item, field)}
    if isinstance(value, dict):
        result = (
            {key for key in value if field != "generated" or key != "player"}
            if field
            in ("objects", "descriptions", "modifier_descriptions", "generated", "upgrades")
            else set[str]()
        )
        for key, item in value.items():
            result.update(_refs(item, key))
        return result
    return set()


def _translate(value: JsonValue, refs: dict[str, str], field: str = "") -> JsonValue:
    if isinstance(value, str):
        return refs.get(value, value) if field in _REF_FIELDS else value
    if isinstance(value, list):
        return [_translate(item, refs, field) for item in value]
    if isinstance(value, dict):
        return {refs.get(key, key): _translate(item, refs, key) for key, item in value.items()}
    return value


def _difference(left: JsonValue, right: JsonValue, path: str = "") -> str | None:
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return path + ".keys"
        for key in left:
            difference = _difference(left[key], right[key], f"{path}.{key}".lstrip("."))
            if difference is not None:
                return difference
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return path + ".length"
        for index, (before, after) in enumerate(zip(left, right, strict=True)):
            difference = _difference(before, after, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    return None if type(left) is type(right) and left == right else path


_SORTED_PILES = ("draw", "discard", "exhaust", "other")


def _translated(value: JsonValue, refs: dict[str, str]) -> JsonValue:
    """Translate refs, then restore the ordinal ref order the Mod gives unordered piles.

    The Mod lists every pile except the hand sorted by ref text, so translating refs between
    runs whose ordinals differ can reorder those lists without any change in the game.
    """
    translated = _translate(value, refs)
    if not isinstance(translated, dict):
        return translated
    state = translated.get("state")
    piles = state.get("piles") if isinstance(state, dict) else None
    if not isinstance(state, dict) or not isinstance(piles, dict):
        return translated
    ordered: dict[str, JsonValue] = {
        name: sorted(cards, key=str) if name in _SORTED_PILES and isinstance(cards, list) else cards
        for name, cards in piles.items()
    }
    return {**translated, "state": {**state, "piles": ordered}}


_NUMBERS = re.compile(r"\d+")


# Events whose text shows a number drawn from the game's unseeded (chaotic) generator: the
# Trial event names a random entrant number, so identical seeds and choices replay other text.
_UNSEEDED_NUMBER_EVENTS = frozenset(("EVENT.TRIAL",))


def _comparable(value: JsonValue) -> JsonValue:
    """Mask the numbers in the texts of events that draw them outside the seeded run."""
    if not isinstance(value, dict):
        return value
    descriptions, objects = value.get("descriptions"), value.get("objects")
    if not isinstance(descriptions, dict) or not isinstance(objects, dict):
        return value
    unseeded = {
        ref
        for ref, entry in objects.items()
        if isinstance(entry, dict) and entry.get("definition_id") in _UNSEEDED_NUMBER_EVENTS
    }
    masked: dict[str, JsonValue] = {
        ref: _NUMBERS.sub("#", text) if ref in unseeded and isinstance(text, str) else text
        for ref, text in descriptions.items()
    }
    return {**value, "descriptions": masked}


def _action_surface(value: JsonValue, control: str | None) -> JsonValue:
    """Compare the next control's readiness and the action surface's settled facts."""
    if not isinstance(value, dict):
        return value
    state = value.get("state")
    actions = state.get("actions") if isinstance(state, dict) else None
    if not isinstance(state, dict) or not isinstance(actions, list):
        return value
    # The native treasure Proceed button can still say Skip after the chest is resolved.
    chest_resolved = state.get("screen") == native.RunScreen.TREASURE and any(
        isinstance(action, dict)
        and action.get("operation") == native.ActionOperation.OPEN_CHEST
        and action.get("enabled") is False
        for action in actions
    )
    kept: list[JsonValue] = []
    for action in actions:
        if isinstance(action, dict):
            action = dict(action)
            if action.get("ref") != control:
                action.pop("enabled", None)
            elif chest_resolved and action.get("operation") == native.ActionOperation.PROCEED:
                action.pop("label", None)
        kept.append(action)
    return {**value, "state": {**state, "actions": kept}}


type CounterBounds = Mapping[str, tuple[int, int]]

_RELIC_COUNTER = re.compile(r"^objects\.relic:\d+:\d+\.counter$")


def is_relic_counter(field_path: str) -> bool:
    return _RELIC_COUNTER.fullmatch(field_path) is not None


def counter_bounds(read: IdentityRead) -> dict[str, tuple[int, int]]:
    """Return relic definition id -> (trigger threshold, reset value) that the game reports."""
    bounds: dict[str, tuple[int, int]] = {}
    for relic in read.identities.relics:
        entry = read.observation.objects.get(relic.ref)
        if (
            relic.counter_threshold is not None
            and relic.counter_reset is not None
            and isinstance(entry, native.RelicEntry)
            and entry.definition_id
        ):
            bounds[entry.definition_id] = (relic.counter_threshold, relic.counter_reset)
    return bounds


def _pending_resets(expected: JsonValue, observed: JsonValue, bounds: CounterBounds) -> JsonValue:
    """Treat a relic counter at its trigger threshold as equal to its reset value.

    A relic shows its threshold while its trigger visuals play and its reset value afterwards,
    so one read can catch the visuals that another has finished. Only that exact pair of
    counters is accepted; the effect already granted and every other field stay compared.
    """
    if not bounds or not isinstance(expected, dict) or not isinstance(observed, dict):
        return expected
    before, after = expected.get("objects"), observed.get("objects")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return expected
    settled = dict(before)
    for ref, entry in before.items():
        live = after.get(ref)
        if not isinstance(entry, dict) or not isinstance(live, dict):
            continue
        definition = entry.get("definition_id")
        pair = bounds.get(definition) if isinstance(definition, str) else None
        recorded, current = entry.get("counter"), live.get("counter")
        if (
            pair is not None
            and entry.get("kind") == "relic"
            and isinstance(recorded, int)
            and isinstance(current, int)
            and recorded != current
            and {recorded, current} == set(pair)
        ):
            settled[ref] = {**entry, "counter": current}
    return {**expected, "objects": settled}


def _scoped(source: native.Observation, actual: native.Observation) -> native.Observation:
    return source.model_copy(update={"location": actual.location, "stamp": actual.stamp})


class _Scope(StrEnum):
    INSTANCE = "game_instance_id"
    RUN = "run_id"
    NODE = "node_id"
    REF_EPOCH = "ref_epoch"


class _ScopeCorrespondence:
    def __init__(self, relation: Relation) -> None:
        self.relation = relation
        self.pairs: dict[tuple[_Scope, str], str] = {}

    def pair(self, domain: _Scope, before: str, after: str) -> None:
        key = (domain, before)
        if key in self.pairs and self.pairs[key] != after:
            raise VerificationFailure(self.relation, domain.value)
        if any(
            kind == domain and old != before and new == after
            for (kind, old), new in self.pairs.items()
        ):
            raise VerificationFailure(self.relation, domain.value)
        self.pairs[key] = after

    def locations(self, source: native.Observation, actual: native.Observation) -> None:
        pairs = (
            (_Scope.INSTANCE, source.location.game_instance_id, actual.location.game_instance_id),
            (_Scope.RUN, source.location.run_id, actual.location.run_id),
            (_Scope.NODE, source.location.node_id, actual.location.node_id),
        )
        for domain, before, after in pairs:
            if before is None or after is None:
                raise VerificationFailure(self.relation, "location." + domain.value)
            self.pair(domain, before, after)
        if source.location.owner is not actual.location.owner:
            raise VerificationFailure(self.relation, "location.owner")


def _ordinal_key(ref: str) -> tuple[int, int]:
    parts = _REF.fullmatch(ref)
    assert parts is not None
    return int(parts[2]), int(parts[3])


class SourceCorrespondence:
    """Keep the injective technical scope mapping for the entire original prefix."""

    def __init__(self, *, rank_refs: bool = False) -> None:
        """Pair refs by ordinal, or by order of first appearance within a kind.

        Instant presentation allocates fewer or different transient objects, so live ordinals
        shift while the objects and their order stay the same. Ranked pairing keeps one strict
        bijection for the whole prefix: a ref keeps its first partner, and refs that first
        appear in the same read pair in ordinal order within their kind.
        """
        self._scopes = _ScopeCorrespondence(Relation.SOURCE_REPLAY)
        self._rank_refs = rank_refs
        self._ranked: dict[str, str] = {}
        self._ranked_live: dict[str, str] = {}

    def _pairs(self, before_refs: set[str], after_refs: set[str]) -> dict[str, str]:
        known = {ref: self._ranked[ref] for ref in before_refs if ref in self._ranked}
        for before, after in known.items():
            if after not in after_refs:
                raise VerificationFailure(Relation.SOURCE_REPLAY, "refs." + before)
        claimed = set(known.values())
        for after in after_refs - claimed:
            if after in self._ranked_live:
                raise VerificationFailure(Relation.SOURCE_REPLAY, "refs." + after)

        def by_kind(refs: set[str]) -> dict[str, list[str]]:
            groups: dict[str, list[str]] = {}
            for ref in sorted(refs, key=_ordinal_key):
                groups.setdefault(ref.split(":", 1)[0], []).append(ref)
            return groups

        fresh = by_kind(before_refs - known.keys())
        fresh_live = by_kind(after_refs - claimed)
        pairs = dict(known)
        for kind in fresh.keys() | fresh_live.keys():
            group, other = fresh.get(kind, []), fresh_live.get(kind, [])
            if len(group) != len(other):
                raise VerificationFailure(Relation.SOURCE_REPLAY, "refs." + (group or other)[0])
            pairs.update(zip(group, other, strict=True))
        return pairs

    def compare(
        self,
        source: native.Observation,
        actual: native.Observation,
        sequence: int,
        *,
        next_step: native.NativeStep | None = None,
        counter_bounds: CounterBounds | None = None,
    ) -> dict[str, str]:
        """Compare two settled reads; a refused read leaves the correspondence unchanged.

        Before a recorded operation, only the control that operation uses must be enabled:
        other buttons enable on their own animation timing. The resolved treasure room's
        Proceed control can also show a transient label.
        """
        scopes = dict(self._scopes.pairs)
        try:
            refs = self._compare(source, actual, next_step, counter_bounds or {})
        except VerificationFailure as error:
            self._scopes.pairs = scopes
            error.sequence = sequence
            raise
        if self._rank_refs:
            self._ranked.update(refs)
            self._ranked_live.update({after: before for before, after in refs.items()})
        return refs

    def _paired(self, source: native.Observation, actual: native.Observation) -> dict[str, str]:
        self._scopes.locations(source, actual)
        before_refs = _refs(_value(source))
        after_refs = _refs(_value(actual))
        for ref in before_refs | (after_refs if self._rank_refs else set[str]()):
            if _REF.fullmatch(ref) is None:
                raise VerificationFailure(Relation.SOURCE_REPLAY, "refs." + ref, unsupported=True)
        refs: dict[str, str] = {}
        ranked = self._pairs(before_refs, after_refs) if self._rank_refs else None
        for before in before_refs:
            parts = _REF.fullmatch(before)
            assert parts is not None
            candidates = (
                [ranked[before]]
                if ranked is not None
                else [
                    after
                    for after in after_refs
                    if (match := _REF.fullmatch(after)) is not None
                    and (parts[1], parts[3]) == (match[1], match[3])
                ]
            )
            if len(candidates) != 1:
                raise VerificationFailure(Relation.SOURCE_REPLAY, "refs." + before)
            after = candidates[0]
            after_parts = _REF.fullmatch(after)
            assert after_parts is not None
            self._scopes.pair(_Scope.REF_EPOCH, parts[2], after_parts[2])
            refs[before] = after
        return refs

    def _compare(
        self,
        source: native.Observation,
        actual: native.Observation,
        next_step: native.NativeStep | None,
        bounds: CounterBounds,
    ) -> dict[str, str]:
        refs = self._paired(source, actual)
        observed = _comparable(_value(actual))
        expected = _pending_resets(
            _comparable(_translated(_value(_scoped(source, actual)), refs)), observed, bounds
        )
        used = translated_step(next_step, refs) if next_step is not None else None
        if used is not None:
            control = used.action if isinstance(used, native.Interact) else None
            expected, observed = (
                _action_surface(expected, control),
                _action_surface(observed, control),
            )
        difference = _difference(expected, observed)
        if difference is not None:
            raise VerificationFailure(Relation.SOURCE_REPLAY, difference)
        if used is not None:
            check_control(actual, used, Relation.SOURCE_REPLAY, 0)
        return refs


def check_control(
    observation: native.Observation, step: native.NativeStep, relation: Relation, sequence: int
) -> None:
    """Require the control a translated recorded step uses to be present and enabled."""
    state = observation.state
    objects = observation.objects
    match step:
        case native.Interact(action=ref):
            usable = isinstance(state, native.RunState | native.CombatState) and any(
                action.ref == ref and action.enabled for action in state.actions
            )
        case native.Select(refs=refs):
            selection = getattr(state, "selection", None)
            usable = isinstance(selection, native.Selection) and set(refs) <= set(
                selection.candidates
            )
        case native.RerollCardReward():
            _, active = card_rewards(observation)
            usable = (
                isinstance(state, native.RunState)
                and state.selection is not None
                and active is not None
                and active.can_reroll is True
            )
        case native.PlayCard(card=ref):
            card = objects.get(ref)
            usable = (
                isinstance(state, native.CombatState)
                and ref in state.piles.hand
                and isinstance(card, native.CardEntry)
                and card.playable is not False
            )
        case native.UsePotion(potion=ref):
            potion = objects.get(ref)
            usable = (
                ref in state.potions
                and isinstance(potion, native.PotionEntry)
                and potion.usable is not False
            )
        case native.DiscardPotion(potion=ref):
            usable = ref in state.potions
        case native.EndTurn():
            usable = isinstance(state, native.CombatState)
    if not usable or observation.input_readiness not in (
        native.InputReadiness.READY,
        native.InputReadiness.SELECTION,
    ):
        raise VerificationFailure(relation, "control." + step.kind.value, sequence=sequence)


def native_refs(read: IdentityRead, relation: Relation) -> dict[str, NativeKey]:
    refs: dict[str, NativeKey] = {}
    keys: set[NativeKey] = set()

    def add(ref: str, key: NativeKey) -> None:
        if ref in refs or key in keys:
            raise VerificationFailure(relation, "identities.duplicate", unsupported=True)
        refs[ref] = key
        keys.add(key)

    for card in read.identities.cards:
        if card.combat_card_id is None and card.deck_card is None:
            continue
        add(card.ref, CardKey(combat_id=card.combat_card_id, deck=card.deck_card))
    for creature in read.identities.creatures:
        if creature.combat_id is not None:
            add(creature.ref, CreatureKey(combat_id=creature.combat_id))
    for relic in read.identities.relics:
        add(relic.ref, RelicSlotKey(owner_net_id=relic.owner_net_id, slot=relic.slot))
    # Only list order identifies a potion; correspondence already compares that list strictly.
    for slot, potion in enumerate(read.observation.state.potions):
        add(potion, PotionSlotKey(slot=slot))
    for point in read.identities.map_travel:
        add(point.ref, MapPointKey(act=point.act, row=point.row, col=point.col))
    for reward in read.identities.reward_bindings:
        add(reward.ref, RewardKey(binding=reward.binding))
    return refs


def bind(
    sequence: int, source: native.Observation, read: IdentityRead, refs: dict[str, str]
) -> KeyedRefs:
    identities = native_refs(read, Relation.SOURCE_REPLAY)
    bindings: list[RefBinding] = []
    for ref in sorted(_refs(_value(source))):
        actual = refs.get(ref)
        if actual is None or actual not in identities:
            raise VerificationFailure(Relation.SOURCE_REPLAY, "identities." + ref, unsupported=True)
        bindings.append(RefBinding(source_ref=ref, native_key=identities[actual]))
    return KeyedRefs(source_sequence=sequence, refs=tuple(bindings))


def loaded_refs(
    binding: KeyedRefs,
    actual: IdentityRead,
    step: native.NativeStep | None = None,
    *,
    relation: Relation = Relation.REPLAY_LOAD,
) -> dict[str, str]:
    identities = native_refs(actual, relation)
    reverse = {key: ref for ref, key in identities.items()}
    bindings = {item.source_ref: item.native_key for item in binding.refs}
    required = set(bindings) if step is None else _refs(_JSON.validate_json(step.model_dump_json()))
    refs: dict[str, str] = {}
    for source in sorted(required):
        key = bindings.get(source)
        ref = reverse.get(key) if key is not None else None
        if ref is None:
            raise VerificationFailure(
                relation,
                "identities." + source,
                unsupported=True,
                sequence=binding.source_sequence,
            )
        refs[source] = ref
    return refs


def _compare_loaded(
    reference: Publication,
    actual: IdentityRead,
    *,
    next_step: native.NativeStep | None,
    allow_unloaded_room: bool,
) -> AllowedMapDifference | None:
    expected = reference.read
    identities = native_refs(expected, Relation.REPLAY_LOAD)
    actual_ids = native_refs(actual, Relation.REPLAY_LOAD)
    if set(identities.values()) != set(actual_ids.values()):
        raise VerificationFailure(Relation.REPLAY_LOAD, "identities", unsupported=True)
    reverse = {key: ref for ref, key in actual_ids.items()}
    refs = {ref: reverse[key] for ref, key in identities.items()}
    if _refs(_value(expected.observation)) - refs.keys():
        raise VerificationFailure(Relation.REPLAY_LOAD, "identities.missing", unsupported=True)
    before = _scoped(expected.observation, actual.observation)
    allowed: AllowedMapDifference | None = None
    state, loaded = before.state, actual.observation.state
    if (
        allow_unloaded_room
        and isinstance(state, native.RunState)
        and isinstance(loaded, native.RunState)
        and state.screen is native.RunScreen.MAP
        and loaded.screen is native.RunScreen.MAP
        and state.progress.room_type in LOADABLE_ROOM_TYPES
        and loaded.progress.room_type is None
    ):
        allowed = AllowedMapDifference(
            source_sequence=reference.source_sequence, before=state.progress.room_type
        )
        before = before.model_copy(
            update={
                "state": state.model_copy(
                    update={"progress": state.progress.model_copy(update={"room_type": None})}
                )
            }
        )
    observed = _comparable(_value(actual.observation))
    bounds = counter_bounds(expected) | counter_bounds(actual)
    recorded = _pending_resets(_comparable(_translated(_value(before), refs)), observed, bounds)
    if next_step is not None:
        control = next_step.action if isinstance(next_step, native.Interact) else None
        recorded, observed = _action_surface(recorded, control), _action_surface(observed, control)
    difference = _difference(recorded, observed)
    if difference is not None:
        raise VerificationFailure(Relation.REPLAY_LOAD, difference)
    if next_step is not None:
        check_control(
            actual.observation, next_step, Relation.REPLAY_LOAD, reference.source_sequence
        )
    return allowed


def translated_step(step: native.NativeStep, refs: dict[str, str]) -> native.NativeStep:
    value = _JSON.validate_json(step.model_dump_json())
    missing = _refs(value) - refs.keys()
    if missing:
        raise VerificationFailure(
            Relation.SOURCE_REPLAY, "identities." + min(missing), unsupported=True
        )
    return TypeAdapter(native.NativeStep).validate_json(_JSON.dump_json(_translate(value, refs)))


class LoadedCorrespondence:
    def __init__(self) -> None:
        self._scopes = _ScopeCorrespondence(Relation.REPLAY_LOAD)

    def compare(
        self,
        reference: Publication,
        actual: IdentityRead,
        *,
        next_step: native.NativeStep | None = None,
        allow_unloaded_room: bool = False,
    ) -> AllowedMapDifference | None:
        """Compare results using the actual read's refs for an optional next operation."""
        scopes = dict(self._scopes.pairs)
        try:
            self._scopes.locations(reference.read.observation, actual.observation)
            return _compare_loaded(
                reference, actual, next_step=next_step, allow_unloaded_room=allow_unloaded_room
            )
        except VerificationFailure as error:
            self._scopes.pairs = scopes
            error.sequence = reference.source_sequence
            raise
