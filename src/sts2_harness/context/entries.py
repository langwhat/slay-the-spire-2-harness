"""Complete-entry updates for unordered projection collections."""

import json
from collections import Counter
from enum import StrEnum

from pydantic import JsonValue


class Collection(StrEnum):
    BINDINGS = "bindings"
    CARDS = "cards"
    NONCARD_OBJECTS = "noncard_objects"
    OBJECT_DESCRIPTIONS = "object_descriptions"
    CARD_DESCRIPTIONS = "card_descriptions"
    MODIFIER_DESCRIPTIONS = "modifier_descriptions"
    MECHANISMS = "mechanisms"
    UPGRADES = "upgrades"
    GENERATED = "generated"


def entry_key(collection: Collection, entry: JsonValue) -> str:
    assert isinstance(entry, dict)
    if collection is Collection.MECHANISMS:
        return json.dumps(entry, ensure_ascii=False, sort_keys=True)
    identity = (
        entry["source"] if collection in (Collection.UPGRADES, Collection.GENERATED) else entry
    )
    assert isinstance(identity, dict) and isinstance(identity["ref"], str)
    return identity["ref"]


def ordered(collection: Collection, entries: JsonValue) -> list[JsonValue]:
    assert isinstance(entries, list)
    return sorted(entries, key=lambda entry: entry_key(collection, entry))


def difference(
    collection: Collection, before: JsonValue, after: JsonValue
) -> tuple[list[JsonValue], list[JsonValue]]:
    assert isinstance(before, list) and isinstance(after, list)
    if collection is Collection.MECHANISMS:
        remaining = Counter(entry_key(collection, entry) for entry in before)
        added: list[JsonValue] = []
        for entry in after:
            key = entry_key(collection, entry)
            if remaining[key]:
                remaining[key] -= 1
            else:
                added.append(entry)
        removed: list[JsonValue] = []
        for entry in before:
            key = entry_key(collection, entry)
            if remaining[key]:
                removed.append(entry)
                remaining[key] -= 1
        return added, removed
    old = {entry_key(collection, entry): entry for entry in before}
    new = {entry_key(collection, entry): entry for entry in after}
    return (
        [entry for key, entry in new.items() if old.get(key) != entry],
        [key for key in old if key not in new],
    )


def apply_entries(
    collection: Collection, before: JsonValue, updates: JsonValue, removed: JsonValue
) -> list[JsonValue]:
    assert isinstance(before, list) and isinstance(updates, list) and isinstance(removed, list)
    if collection is Collection.MECHANISMS:
        entries = list(before)
        for entry in removed:
            entries.remove(entry)
        return ordered(collection, entries + updates)
    values = {entry_key(collection, entry): entry for entry in before}
    for key in removed:
        assert isinstance(key, str)
        del values[key]
    values.update((entry_key(collection, entry), entry) for entry in updates)
    return ordered(collection, list(values.values()))
