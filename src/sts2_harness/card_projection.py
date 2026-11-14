"""Shared card facts and next-upgrade changes for state and knowledge views."""

from pydantic import JsonValue, TypeAdapter

from sts2_harness.client import models as native

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def card_rules(
    card: native.CardEntry | native.CardDefinitionPreview, text: str | None
) -> dict[str, JsonValue]:
    raw = _JSON_OBJECT.validate_json(card.to_json())
    identity = card.definition_id + (f"+{card.upgrades}" if card.upgrades else "")
    return {
        "id": identity,
        "name": card.name,
        "type": card.card_type,
        "cost": raw["cost"],
        "text": text,
        "keywords": raw["keywords"],
    }


def upgrade_changes(
    card: native.CardEntry | native.CardDefinitionPreview,
    text: str | None,
    upgrade: native.UpgradePreview,
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {"status": upgrade.status}
    if not isinstance(upgrade, native.UpgradeAvailable):
        return result
    base = {**_JSON_OBJECT.validate_json(card.to_json()), "text": text}
    ignored = {"definition_id", "upgrades", "rarity", "card_pool", "is_upgradable", "is_removable"}
    result.update(
        {
            "type" if name == "card_type" else name: value
            for name, value in _JSON_OBJECT.validate_json(upgrade.card.to_json()).items()
            if name not in ignored and base.get(name) != value
        }
    )
    return result
