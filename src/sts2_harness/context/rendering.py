"""Compact model view of known projection sections, without changing stored facts."""

import json

from pydantic import JsonValue

from sts2_harness.context.entries import Collection

_TABLES = frozenset(
    {
        *Collection,
        "hand",
        "draw",
        "discard",
        "exhaust",
        "other",
        "deck",
        "relics",
        "potions",
        "nodes",
        "edges",
        "actions",
        "offers",
        "candidates",
        "selected",
        "previews",
        "targets",
        "potential_destinations",
        "enemies",
        "inactive_enemies",
        "allies",
    }
)
_GROUPS = frozenset(
    {
        Collection.CARDS,
        Collection.CARD_DESCRIPTIONS,
        Collection.MODIFIER_DESCRIPTIONS,
        Collection.OBJECT_DESCRIPTIONS,
    }
)


_SECTION_ORDER: dict[str, tuple[str, ...]] = {
    "observation": ("catalog_additions", "current", "scene", "decision"),
    "current": (
        "map_topology",
        "unknown_odds",
        "dynamic_map",
        "encounter",
        "progress",
        "inventory",
        "deck",
        "noncard_objects",
        "upgrades",
        "modifier_descriptions",
        "object_descriptions",
        "card_descriptions",
        "generated",
        "mechanisms",
    ),
}


def _order(value: JsonValue, section: str = "") -> JsonValue:
    if isinstance(value, list):
        return [_order(item) for item in value]
    if not isinstance(value, dict):
        return value
    # Stable state sections precede live state; all other object keys stay deterministic.
    preferred = _SECTION_ORDER.get(section, ())
    keys = [key for key in preferred if key in value]
    keys.extend(key for key in sorted(value) if key not in preferred)
    return {key: _order(value[key], key) for key in keys}


def _json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _group(entries: list[JsonValue]) -> list[JsonValue]:
    groups: dict[str, tuple[dict[str, JsonValue], list[JsonValue]]] = {}
    for entry in entries:
        assert isinstance(entry, dict) and isinstance(entry["ref"], str)
        content = {key: value for key, value in entry.items() if key != "ref"}
        key = _json(content)
        if key not in groups:
            groups[key] = content, []
        groups[key][1].append(entry["ref"])
    return [
        {"refs": refs, **content} if len(refs) > 1 else {"ref": refs[0], **content}
        for content, refs in groups.values()
    ]


def _compact(value: JsonValue, path: tuple[str, ...] = ()) -> JsonValue:
    if isinstance(value, dict):
        return {key: _compact(child, (*path, key)) for key, child in value.items()}
    if not isinstance(value, list):
        return value
    entries = [_compact(child, path) for child in value]
    section = path[-1] if path else ""
    if len(path) >= 2 and path[-2] == "current" and section in _GROUPS and entries:
        if all(isinstance(entry, dict) and isinstance(entry.get("ref"), str) for entry in entries):
            grouped = _group(entries)
            if len(_json(grouped)) < len(_json(entries)):
                entries = grouped
    if section not in _TABLES or len(entries) < 3:
        return entries
    first = entries[0]
    if not isinstance(first, dict) or len(first) < 2:
        return entries
    columns = list(first)
    if any(not isinstance(entry, dict) or list(entry) != columns for entry in entries):
        return entries
    rows: list[JsonValue] = []
    for entry in entries:
        assert isinstance(entry, dict)
        rows.append([entry[column] for column in columns])
    table: dict[str, JsonValue] = {"columns": list(columns), "rows": rows}
    return table if len(_json(table)) < len(_json(entries)) else entries


def render(value: JsonValue) -> str:
    """Render a semantic delivery once; existing conversation text is never rewritten."""
    return _json(_compact(_order(value)))
