import json

from pydantic import MISSING
from test_mod_client import RUN

from sts2_harness.client.models import DecisionOwner, Observation
from sts2_harness.context import Context, RunActInput


def test_run_task_delivers_four_block_observation() -> None:
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), Observation.model_validate_json(RUN))
    payload = json.loads(context.model_input()[-1].text)
    observation = payload["observation"]
    assert observation["scene"]["node_id"] == "node"
    assert observation["decision"]["selection"] is None
    assert observation["current"]["deck"] == []
    assert "state" not in observation


def test_run_query_returns_full_data_with_fresh_observation() -> None:
    from sts2_harness.client.models import DetailData
    from sts2_harness.context import QueryToolResult, ReadCompleted
    from sts2_harness.llm.messages import ToolCall

    observation = Observation.model_validate_json(RUN)
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), observation)
    data = DetailData(
        objects={}, descriptions={}, modifier_descriptions={}, generated={}, mechanisms=()
    )
    for _ in range(2):
        context.append_tool_result(
            ToolCall("q", "run_inspect", "{}"), QueryToolResult(ReadCompleted(), observation, data)
        )
        result = json.loads(context.model_input()[-1].text)
        assert result["data"]["objects"] == {}
        assert result["observation"]["decision"]["selection"] is None
        assert "state" not in result


def test_profiles_reuse_exact_content_and_refresh_references_across_nodes() -> None:
    from test_mod_client import COMBAT

    from sts2_harness.client.models import CardEntry, RunState
    from sts2_harness.context import QueryToolResult, ReadCompleted
    from sts2_harness.context.models import RunResultMessage
    from sts2_harness.llm.messages import ToolCall

    run = Observation.model_validate_json(RUN)
    card = Observation.model_validate_json(COMBAT).objects["c1"]
    assert isinstance(card, CardEntry) and isinstance(run.state, RunState)
    first = run.model_copy(
        update={
            "state": run.state.model_copy(update={"deck": ("d1", "d2")}),
            "objects": {"d1": card, "d2": card},
            "descriptions": {"d1": "Complete text.", "d2": "Complete text."},
        }
    )
    changed_card = card.model_copy(update={"upgrades": 2})
    changed = first.model_copy(update={"objects": {"d1": changed_card, "d2": card}})
    context = Context(DecisionOwner.RUN)
    call = ToolCall("q", "run_inspect", "{}")
    for observed in (first, changed, first):
        context.append_tool_result(call, QueryToolResult(ReadCompleted(), observed))
    a, b, again = context.model_input()
    assert isinstance(a, RunResultMessage) and isinstance(b, RunResultMessage)
    assert isinstance(again, RunResultMessage)
    first_view, changed_view, last_view = (item.content.observation for item in (a, b, again))
    assert first_view is not None and changed_view is not None and last_view is not None
    assert first_view.scene is not MISSING and changed_view.scene is not MISSING
    assert last_view.scene is not MISSING
    bindings = first_view.scene.model_dump(mode="json")["bindings"]
    changed_bindings = changed_view.scene.model_dump(mode="json")["bindings"]
    restored_bindings = last_view.scene.model_dump(mode="json")["bindings"]
    assert isinstance(bindings, list) and isinstance(changed_bindings, list)
    assert isinstance(restored_bindings, list)
    assert isinstance(bindings[1], dict)
    assert bindings[0] == bindings[1] | {"ref": "d1"}
    assert changed_bindings[0] != bindings[0]
    assert restored_bindings == [bindings[0]]
    assert last_view.catalog_additions is MISSING
    next_node = first.model_copy(
        update={"location": first.location.model_copy(update={"node_id": "next"})}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    next_message = context.model_input()[-1]
    assert isinstance(next_message, RunResultMessage)
    projection = next_message.content.observation
    assert (
        projection is not None
        and projection.scene is not MISSING
        and projection.current is not MISSING
    )
    assert projection.current.model_dump(mode="json")["deck"] == [
        {"ref": "d1", "name": card.name, "definition_id": card.definition_id},
        {"ref": "d2", "name": card.name, "definition_id": card.definition_id},
    ]
    descriptions = projection.current.card_descriptions
    assert descriptions is not MISSING and len(descriptions) == 2


def test_language_only_change_is_delivered_with_distinct_catalog_profiles() -> None:
    from test_mod_client import COMBAT

    from sts2_harness.context.models import RunActContent, TaskMessage

    run = Observation.model_validate_json(RUN)
    card = Observation.model_validate_json(COMBAT).objects["c1"]
    run = run.model_copy(update={"objects": {"c1": card}})
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), run)
    context.append_task(RunActInput(), run.model_copy(update={"game_language": "eng"}))
    assert len(context.model_input()) == 2
    first, second = context.model_input()
    assert isinstance(first, TaskMessage) and isinstance(second, TaskMessage)
    assert isinstance(first.content, RunActContent) and isinstance(second.content, RunActContent)
    assert first.content.observation.catalog_additions is not MISSING
    assert second.content.observation.catalog_additions is not MISSING
    assert second.content.observation.scene is not MISSING


def test_every_location_identity_change_refreshes_complete_bindings_and_deck() -> None:
    from test_mod_client import COMBAT

    from sts2_harness.client.models import CardEntry, RunState
    from sts2_harness.context import QueryToolResult, ReadCompleted
    from sts2_harness.context.models import RunResultMessage
    from sts2_harness.llm.messages import ToolCall

    run = Observation.model_validate_json(RUN)
    card = Observation.model_validate_json(COMBAT).objects["c1"]
    assert isinstance(card, CardEntry) and isinstance(run.state, RunState)
    run = run.model_copy(
        update={
            "state": run.state.model_copy(update={"deck": ("d1",)}),
            "objects": {"d1": card},
            "descriptions": {"d1": "Known text."},
        }
    )
    context = Context(DecisionOwner.RUN)
    call = ToolCall("q", "run_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), run))
    for field, value in (
        ("game_instance_id", "other-game"),
        ("run_id", "other-run"),
        ("node_id", "other-node"),
    ):
        run = run.model_copy(update={"location": run.location.model_copy(update={field: value})})
        context.append_tool_result(call, QueryToolResult(ReadCompleted(), run))
        message = context.model_input()[-1]
        assert isinstance(message, RunResultMessage)
        projection = message.content.observation
        assert projection is not None and projection.scene is not MISSING
        assert projection.current is not MISSING
        assert projection.scene.model_dump(mode="json")[field] == value
        assert projection.current.model_dump(mode="json")["deck"] == [
            {"ref": "d1", "name": card.name, "definition_id": card.definition_id}
        ]
        assert projection.current.model_dump(mode="json")["card_descriptions"] == [
            {
                "ref": "d1",
                "name": card.name,
                "definition_id": card.definition_id,
                "text": "Known text.",
            }
        ]
        assert projection.catalog_additions is MISSING


def test_complete_sections_replace_and_selection_explicitly_closes() -> None:
    from test_mod_client import COMBAT

    from sts2_harness.client.models import CardEntry, RunState, Selection
    from sts2_harness.context import QueryToolResult, ReadCompleted
    from sts2_harness.context.models import RunResultMessage
    from sts2_harness.llm.messages import ToolCall

    original = Observation.model_validate_json(RUN)
    card = Observation.model_validate_json(COMBAT).objects["c1"]
    assert isinstance(original.state, RunState) and isinstance(card, CardEntry)
    unknown = card.model_copy(
        update={
            "is_upgradable": None,
            "should_retain_this_turn": False,
            "affliction": None,
        }
    )
    selection = Selection(
        prompt="Choose", source=None, min=0, max=1, candidates=("d1",), selected=()
    )
    opened = original.model_copy(
        update={
            "state": original.state.model_copy(update={"deck": ("d1",), "selection": selection}),
            "objects": {"d1": unknown},
            "descriptions": {"d1": None},
        }
    )
    closed = opened.model_copy(
        update={
            "state": opened.state.model_copy(update={"selection": None, "deck": ()}),
            "objects": {},
            "descriptions": {},
        }
    )
    context = Context(DecisionOwner.RUN)
    call = ToolCall("q", "run_inspect", "{}")
    for item in (opened, closed):
        context.append_tool_result(call, QueryToolResult(ReadCompleted(), item))
    first, second = context.model_input()
    assert isinstance(first, RunResultMessage) and isinstance(second, RunResultMessage)
    a, b = first.content.observation, second.content.observation
    assert a is not None and a.current is not MISSING
    cards = a.current.model_dump(mode="json")["cards"]
    assert isinstance(cards, list) and isinstance(cards[0], dict)
    assert cards[0]["is_upgradable"] is None
    assert "should_retain_this_turn" not in cards[0] and "affliction" not in cards[0]
    descriptions = a.current.model_dump(mode="json")["card_descriptions"]
    assert isinstance(descriptions, list) and isinstance(descriptions[0], dict)
    assert descriptions[0]["text"] is None
    assert a.decision.selection is not None and a.decision.selection.selected == ()
    assert b is not None and b.current is not MISSING
    assert b.decision.selection is None
    assert b.current.deck == () and b.current.cards is MISSING
    assert b.current.removed is not MISSING
    assert b.current.removed.cards == ("d1",)


def test_run_noncard_section_excludes_combat_actors() -> None:
    from test_mod_client import COMBAT

    from sts2_harness.client.models import AllyEntry, CombatState, EnemyEntry, ObjectKind
    from sts2_harness.context.models import RunActContent, TaskMessage

    run = Observation.model_validate_json(RUN)
    combat = Observation.model_validate_json(COMBAT)
    assert isinstance(combat.state, CombatState)
    enemy = combat.state.enemies[0]
    entry = EnemyEntry(
        kind=ObjectKind.ENEMY,
        name=enemy.name,
        definition_id=enemy.definition_id,
        hp=enemy.hp,
        max_hp=enemy.max_hp,
        block=enemy.block,
        powers=enemy.powers,
        intents=enemy.intents,
        hp_display=enemy.hp_display,
    )
    ally = AllyEntry(
        kind=ObjectKind.ALLY, name="Ally", hp=1, max_hp=1, block=0, powers=(), alive=True
    )
    context = Context(DecisionOwner.RUN)
    context.append_task(
        RunActInput(), run.model_copy(update={"objects": {"e1": entry, "a1": ally}})
    )
    message = context.model_input()[-1]
    assert isinstance(message, TaskMessage) and isinstance(message.content, RunActContent)
    current = message.content.observation.current
    assert current is not MISSING and current.noncard_objects == ()
    scene = message.content.observation.scene
    assert scene is not MISSING and scene.bindings == ()
    assert message.content.observation.catalog_additions is MISSING
