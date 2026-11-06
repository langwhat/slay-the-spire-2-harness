import json

from pydantic import JsonValue

from sts2_harness.context.rendering import render


def expand(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [expand(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"columns", "rows"}:
        columns, rows = value["columns"], value["rows"]
        assert isinstance(columns, list) and isinstance(rows, list)
        result: list[JsonValue] = []
        for row in rows:
            assert isinstance(row, list)
            result.append({str(key): expand(cell) for key, cell in zip(columns, row, strict=True)})
        return result
    return {key: expand(item) for key, item in value.items()}


def test_known_ordered_rows_keep_every_value_and_order() -> None:
    rows: list[JsonValue] = [
        {"ref": f"card:{i}", "name": 'Quote "\n null', "definition_id": "CARD.WOUND"}
        for i in (3, 1, 2, 0)
    ]
    source: dict[str, JsonValue] = {"observation": {"current": {"draw": rows}}}
    encoded = render(source)
    assert '"columns"' in encoded
    assert expand(json.loads(encoded)) == source
    assert len(encoded) < len(json.dumps(source, ensure_ascii=False, separators=(",", ":")))


def test_unknown_lists_and_missing_fields_are_not_tabulated() -> None:
    data: list[JsonValue] = [{"a": None, "b": False, "c": 0}] * 4
    source: dict[str, JsonValue] = {
        "data": data,
        "observation": {"current": {"draw": [{"ref": "a"}, {"ref": "b", "name": None}]}},
    }
    assert json.loads(render(source)) == source


def test_equal_unordered_entries_share_content_but_keep_distinct_refs() -> None:
    source: dict[str, JsonValue] = {
        "observation": {
            "current": {
                "card_descriptions": [
                    {"ref": "card:1", "text": "A complete long rule with a condition." * 4},
                    {"ref": "card:2", "text": "A complete long rule with a condition." * 4},
                    {"ref": "card:3", "text": None},
                ]
            }
        },
    }
    result = json.loads(render(source))["observation"]["current"]["card_descriptions"]
    assert result == [
        {"refs": ["card:1", "card:2"], "text": "A complete long rule with a condition." * 4},
        {"ref": "card:3", "text": None},
    ]


def test_appending_new_state_preserves_the_exact_previous_model_prefix() -> None:
    from test_mod_client import RUN

    from sts2_harness.client import models as native
    from sts2_harness.context import Context
    from sts2_harness.context.models import RunActInput

    observation = native.Observation.model_validate_json(RUN)
    context = Context(native.DecisionOwner.RUN)
    context.append_task(RunActInput(), observation)
    prefix = tuple(message.text for message in context.model_input())
    changed = observation.model_copy(update={
        "state": observation.state.model_copy(update={
            "player": observation.state.player.model_copy(update={"gold": 97})
        })
    })
    context.append_task(RunActInput(), changed)
    assert len(context.model_input()) == 2
    assert tuple(message.text for message in context.model_input()[:-1]) == prefix
