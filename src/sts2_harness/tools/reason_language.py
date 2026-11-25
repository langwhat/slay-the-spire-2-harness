"""Resolve viewer-facing action reason language guidance from native game_language."""

from pydantic import JsonValue

REASON_LANGUAGE_PLACEHOLDER = "{reason_language_guidance}"

# Map native STS2 language codes and common locale aliases to (display_name, use_chinese_style).
_LANGUAGES: dict[str, tuple[str, bool]] = {
    "zhs": ("Simplified Chinese", True),
    "zh-cn": ("Simplified Chinese", True),
    "zh-hans": ("Simplified Chinese", True),
    "zh-sg": ("Simplified Chinese", True),
    "zht": ("Traditional Chinese", True),
    "zh-tw": ("Traditional Chinese", True),
    "zh-hk": ("Traditional Chinese", True),
    "zh-mo": ("Traditional Chinese", True),
    "zh-hant": ("Traditional Chinese", True),
    "zh": ("Chinese", True),
    "eng": ("English", False),
    "en": ("English", False),
    "en-us": ("English", False),
    "en-gb": ("English", False),
    "deu": ("German", False),
    "de": ("German", False),
    "esp": ("Spanish", False),
    "spa": ("Spanish", False),
    "es": ("Spanish", False),
    "fra": ("French", False),
    "fr": ("French", False),
    "ind": ("Indonesian", False),
    "id": ("Indonesian", False),
    "ita": ("Italian", False),
    "it": ("Italian", False),
    "jpn": ("Japanese", False),
    "ja": ("Japanese", False),
    "kor": ("Korean", False),
    "ko": ("Korean", False),
    "pol": ("Polish", False),
    "pl": ("Polish", False),
    "ptb": ("Brazilian Portuguese", False),
    "pt-br": ("Brazilian Portuguese", False),
    "pt": ("Portuguese", False),
    "rus": ("Russian", False),
    "ru": ("Russian", False),
    "tha": ("Thai", False),
    "th": ("Thai", False),
    "tur": ("Turkish", False),
    "tr": ("Turkish", False),
}


def reason_language_guidance(game_language: str | None) -> str:
    """Build viewer-facing reason wording rules for the live game's displayed language."""
    normalized = game_language.strip().lower().replace("_", "-") if game_language else ""
    language_name, chinese_style = _LANGUAGES.get(
        normalized, ("the game's displayed language", False)
    )
    if chinese_style:
        opening = (
            f"Write this viewer-facing `reason` in {language_name} as a short, conversational "
            "line, as if speaking while clicking."
        )
    else:
        opening = f"Write this viewer-facing `reason` in {language_name}."
    return (
        f"{opening} "
        "Name the decisive draw, purpose, or tradeoff using the game's localized card and object "
        "names; never include `CARD.*` or other native definition IDs, temporary refs, or "
        "parenthesized IDs. "
        "A quick call is enough for routine choices; an unexpected draw can sound surprised. "
        "Include a number only if it changes this choice. "
        "Keep step lists, leftover resources, and hypothetical plans out of this field. "
        "Use your own on-stream identity. "
        "This applies only to viewer-facing `reason` wording; keep tool arguments, strategy, "
        "notes, and gameplay decisions independent."
    )


def _resolve_value(value: JsonValue, guidance: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(REASON_LANGUAGE_PLACEHOLDER, guidance)
    if isinstance(value, list):
        return [_resolve_value(item, guidance) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, guidance) for key, item in value.items()}
    return value


def resolve_schema(schema: dict[str, JsonValue], game_language: str | None) -> dict[str, JsonValue]:
    """Return a copy of a tool input schema with the reason language placeholder resolved."""
    guidance = reason_language_guidance(game_language)
    resolved = _resolve_value(schema, guidance)
    assert isinstance(resolved, dict)
    return resolved
