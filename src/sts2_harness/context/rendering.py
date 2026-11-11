"""Render complete state sections with stable keys and ordinary object arrays."""

import json

from pydantic import JsonValue


def render(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
