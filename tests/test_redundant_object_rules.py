import json

import pytest
from pydantic import JsonValue
from test_mod_client import RUN

from sts2_harness.client.models import DecisionOwner, Observation
from sts2_harness.context import Context
from sts2_harness.context.models import RunActInput, TaskMessage


@pytest.mark.parametrize("kind", ["relic", "potion"])
def test_full_object_rule_replaces_only_its_exact_owned_mechanism(kind: str) -> None:
    raw = json.loads(RUN)
    obj: dict[str, JsonValue] = {"kind": kind, "definition_id": "OBJECT.ONE", "name": "One"}
    if kind == "potion":
        obj.update(usable=False, needs_target=None, target_type=None, usage=None)
    raw["objects"] = {"one": obj, "two": obj}
    raw["state"]["relics" if kind == "relic" else "potions"] = ["one", "two"]
    raw["descriptions"] = {"one": "Full rule."}
    rule = {"kind": kind, "definition_id": "OBJECT.ONE", "name": "One", "text": "Full rule."}
    raw["mechanisms"] = [
        {**rule, "owner": "one"},
        {**rule, "owner": "two"},
        {**rule, "owner": "one", "kind": "keyword"},
        {**rule, "owner": "one", "text": "Additional condition."},
        {**rule, "owner": "one", "name": "Other name"},
        {**rule, "owner": "one", "definition_id": "OBJECT.OTHER"},
    ]
    context = Context(DecisionOwner.RUN)
    opening = Observation.model_validate_json(json.dumps(raw))
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    frame = json.loads(message.content.to_json())["observation"]
    assert len(frame["current"]["mechanisms"]) == 5
    assert frame["current"]["object_descriptions"] == [
        {"ref": "one", "definition_id": "OBJECT.ONE", "name": "One", "text": "Full rule."}
    ]
    profiles = frame["catalog_additions"]
    shared = next(p for p in profiles.values() if p["content"].get("text") == "Full rule.")
    assert shared["content"]["definition_id"] == "OBJECT.ONE"
    context.finish_group()
    only_duplicate = opening.model_copy(update={"mechanisms": (opening.mechanisms[0],)})
    fresh = Context(DecisionOwner.RUN)
    fresh.append_task(RunActInput(), only_duplicate)
    message = fresh.model_input()[-1]
    assert isinstance(message, TaskMessage)
    isolated = json.loads(message.content.to_json())["observation"]
    assert isolated["current"]["mechanisms"] == []
    assert all(p["kind"] != "mechanism" for p in isolated["catalog_additions"].values())

    # A missing or unknown description restores the independent rule immediately.
    for description in ({}, {"one": None}, {"one": "Changed rule."}):
        changed = opening.model_copy(update={"descriptions": description})
        context.begin_group(changed, "")
        context.append_task(RunActInput(), changed)
        message = context.model_input()[-1]
        assert isinstance(message, TaskMessage)
        current = json.loads(message.content.to_json())["observation"]["current"]
        if description == {}:
            assert len(current["mechanisms"]) == 1
            assert current["mechanisms"][0]["owner"]["ref"] == "one"
        context.install_compaction(context.compaction_candidate())
        context.finish_group()
    context.begin_group(opening, "")
    context.append_task(RunActInput(), opening)
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage)
    restored = json.loads(message.content.to_json())["observation"]["current"]
    assert len(restored["removed"]["mechanisms"]) == 1
    assert restored["removed"]["mechanisms"][0]["owner"]["ref"] == "one"
    context.install_compaction(context.compaction_candidate())
