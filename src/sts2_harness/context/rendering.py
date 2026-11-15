"""Render complete state sections with stable keys and ordinary object arrays."""

import json

from pydantic import JsonValue


def _ordered(value: JsonValue, *, fights: bool = False) -> JsonValue:
    if isinstance(value, list):
        return [_ordered(item, fights=fights) for item in value]
    if not isinstance(value, dict):
        return value
    keys = value if fights else sorted(value)
    return {key: _ordered(value[key], fights=key == "fights") for key in keys}


def render(value: JsonValue) -> str:
    return json.dumps(_ordered(value), ensure_ascii=False, separators=(",", ":"))
