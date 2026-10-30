"""Verify public correspondence before deriving or consuming native object bindings."""

import re
from enum import StrEnum

from pydantic import JsonValue, TypeAdapter

from sts2_harness.client import models as native
from sts2_harness_eval.checkpoints import (
    CardKey,
    CreatureKey,
    IdentityRead,
    MapPointKey,
    NativeKey,
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
    )
)


def _value(observation: native.Observation) -> JsonValue:
    return _JSON.validate_json(observation.to_json())


def _refs(value: JsonValue, field: str = "") -> set[str]:
    if isinstance(value, str):
        return (
            {value}
            if field in _REF_FIELDS and (field != "owner" or _REF.fullmatch(value))
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
        self._scopes: dict[tuple[_Scope, str], str] = {}

    def pair(self, domain: _Scope, before: str, after: str) -> None:
        key = (domain, before)
        if key in self._scopes and self._scopes[key] != after:
            raise VerificationFailure(self.relation, domain.value)
        if any(
            kind == domain and old != before and new == after
            for (kind, old), new in self._scopes.items()
        ):
            raise VerificationFailure(self.relation, domain.value)
        self._scopes[key] = after

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


class SourceCorrespondence:
    """Keep the injective technical scope mapping for the entire original prefix."""

    def __init__(self) -> None:
        self._scopes = _ScopeCorrespondence(Relation.SOURCE_REPLAY)

    def compare(
        self, source: native.Observation, actual: native.Observation, sequence: int
    ) -> dict[str, str]:
        try:
            return self._compare(source, actual)
        except VerificationFailure as error:
            error.sequence = sequence
            raise

    def _compare(self, source: native.Observation, actual: native.Observation) -> dict[str, str]:
        self._scopes.locations(source, actual)
        before_refs = _refs(_value(source))
        after_refs = _refs(_value(actual))
        refs: dict[str, str] = {}
        for before in before_refs:
            parts = _REF.fullmatch(before)
            if parts is None:
                raise VerificationFailure(
                    Relation.SOURCE_REPLAY, "refs." + before, unsupported=True
                )
            candidates = [
                after
                for after in after_refs
                if (match := _REF.fullmatch(after)) is not None
                and (parts[1], parts[3]) == (match[1], match[3])
            ]
            if len(candidates) != 1:
                raise VerificationFailure(Relation.SOURCE_REPLAY, "refs." + before)
            after = candidates[0]
            after_parts = _REF.fullmatch(after)
            assert after_parts is not None
            self._scopes.pair(_Scope.REF_EPOCH, parts[2], after_parts[2])
            refs[before] = after
        difference = _difference(_translate(_value(_scoped(source, actual)), refs), _value(actual))
        if difference is not None:
            raise VerificationFailure(Relation.SOURCE_REPLAY, difference)
        return refs


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
    reference: Publication, actual: IdentityRead, *, allow_event_map: bool = False
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
        allow_event_map
        and isinstance(state, native.RunState)
        and isinstance(loaded, native.RunState)
        and state.screen is native.RunScreen.MAP
        and loaded.screen is native.RunScreen.MAP
        and state.progress.room_type == "event"
        and loaded.progress.room_type is None
    ):
        allowed = AllowedMapDifference(source_sequence=reference.source_sequence, before="event")
        before = before.model_copy(
            update={
                "state": state.model_copy(
                    update={"progress": state.progress.model_copy(update={"room_type": None})}
                )
            }
        )
    difference = _difference(_translate(_value(before), refs), _value(actual.observation))
    if difference is not None:
        raise VerificationFailure(Relation.REPLAY_LOAD, difference)
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
        self, reference: Publication, actual: IdentityRead, *, allow_event_map: bool = False
    ) -> AllowedMapDifference | None:
        try:
            self._scopes.locations(reference.read.observation, actual.observation)
            return _compare_loaded(reference, actual, allow_event_map=allow_event_map)
        except VerificationFailure as error:
            error.sequence = reference.source_sequence
            raise
