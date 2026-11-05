import asyncio
from os.path import commonprefix
from types import MappingProxyType

import httpx2 as httpx
from pydantic import MISSING, JsonValue
from test_mod_client import COMBAT, RUN

from sts2_harness.client import ModClient
from sts2_harness.client.models import (
    CardEntry,
    CombatPlayer,
    CombatState,
    DecisionOwner,
    DetailData,
    MapData,
    MechanismKind,
    MechanismView,
    ModifierDescriptions,
    Observation,
    Power,
    ResourceCosts,
    RunState,
    StateCompleted,
    TerminalResult,
    TerminalState,
)
from sts2_harness.context import (
    ActionToolResult,
    CombatActInput,
    CombatReviewInput,
    Context,
    QueryToolResult,
    ReadCompleted,
)
from sts2_harness.context.models import (
    ActContent,
    ReviewContent,
    RunActContent,
    RunActInput,
    RunHandoffInput,
    RunProjectedResult,
    RunResultMessage,
    TaskMessage,
)
from sts2_harness.decisions import BattleLocation, BattleReport, BoundaryValues, CombatHandoff
from sts2_harness.llm.messages import AssistantMessage, ResponsesReply, ToolCall
from sts2_harness.results import ActionCompleted, Failed, Reason, ReasonCode, Suggestion


def test_task_delivers_complete_state_and_latest_details_without_rewriting_history() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    first_input = context.model_input()
    assert len(first_input) == 1
    first = first_input[0]
    assert isinstance(first, TaskMessage)
    assert isinstance(first.content, ActContent)
    projected = first.content.observation
    assert projected.current is not MISSING
    assert projected.current.piles is not MISSING and projected.current.piles is not None
    assert tuple(card.ref for card in projected.current.piles.hand) == ("c2", "c1")
    assert projected.scene is not MISSING
    assert {binding.ref: binding.name for binding in projected.scene.bindings}["c1"] == "牌"
    context.append_task(CombatActInput(), observation)
    assert context.model_input() == first_input


def test_results_deliver_latest_changes_and_full_explicit_data_independently() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("inspect", "combat_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    original = context.model_input()[0]
    assert isinstance(original, RunResultMessage)
    original_projected = original.content.observation
    assert original_projected is not None and original_projected.current is not MISSING
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    changed = observation.model_copy(
        update={
            "objects": {
                **observation.objects,
                "c1": card.model_copy(update={"cost": ResourceCosts(energy=0)}),
            },
            "descriptions": {**observation.descriptions, "c1": "新正文"},
            "modifier_descriptions": {
                "c1": ModifierDescriptions(enchantment=None, affliction=None)
            },
        }
    )
    numeric_only = changed.model_copy(
        update={
            "descriptions": observation.descriptions,
            "modifier_descriptions": observation.modifier_descriptions,
        }
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), numeric_only))
    numeric_message = context.model_input()[-1]
    assert isinstance(numeric_message, RunResultMessage)
    numeric = numeric_message.content.observation
    assert numeric is not None and numeric.current is not MISSING
    assert numeric.current.cards is not MISSING
    assert next(item for item in numeric.current.cards if item.ref == "c1").cost.energy == 0
    assert numeric.current.card_descriptions is MISSING
    assert numeric.current.modifier_descriptions is MISSING
    details = DetailData(
        objects=changed.objects,
        descriptions=changed.descriptions,
        modifier_descriptions=changed.modifier_descriptions,
        generated={},
        mechanisms=(),
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), changed, details))
    changed_message = context.model_input()[-1]
    assert isinstance(changed_message, RunResultMessage)
    assert changed_message.content.data is details
    projected = changed_message.content.observation
    assert projected is not None and projected.current is not MISSING
    assert projected.current.cards is MISSING
    assert projected.current.card_descriptions is not MISSING
    assert [(item.ref, item.text) for item in projected.current.card_descriptions] == [
        ("c1", "新正文")
    ]
    assert projected.current.modifier_descriptions is not MISSING
    assert [
        (item.ref, item.enchantment, item.affliction)
        for item in projected.current.modifier_descriptions
    ] == [("c1", None, MISSING)]
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), changed, details))
    unchanged = context.model_input()[-1]
    assert isinstance(unchanged, RunResultMessage)
    assert unchanged.content.data is details
    assert unchanged.content.observation is not None
    assert unchanged.content.observation.current is MISSING
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    returned = context.model_input()[-1]
    assert isinstance(returned, RunResultMessage)
    assert (
        returned.content.observation is not None
        and returned.content.observation.current is not MISSING
    )
    assert returned.content.observation.current.card_descriptions is not MISSING
    assert [
        (item.ref, item.text) for item in returned.content.observation.current.card_descriptions
    ] == [("c1", None)]
    assert original_projected.current.cards is not MISSING
    assert (
        next(item for item in original_projected.current.cards if item.ref == "c1").cost
        == card.cost
    )
    state = observation.state
    assert isinstance(state, CombatState)
    departing = observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "piles": state.piles.model_copy(update={"hand": ("c2",), "discard": ("c1",)}),
                    "player": state.player.model_copy(update={"powers": ()}),
                }
            )
        }
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), departing))
    moved = context.model_input()[-1]
    assert isinstance(moved, RunResultMessage)
    assert (
        moved.content.observation is not None and moved.content.observation.current is not MISSING
    )
    current = moved.content.observation.current
    assert current.piles is not MISSING and current.piles is not None
    assert tuple(item.ref for item in current.piles.hand) == ("c2",)
    assert tuple(item.ref for item in current.piles.discard) == ("c1",)
    assert isinstance(current.player, CombatPlayer)
    assert current.player.powers == ()
    assert current.cards is MISSING
    assert moved.content.observation.decision.hand_previews is not MISSING
    assert tuple(item.ref for item in moved.content.observation.decision.hand_previews) == ("c2",)
    assert isinstance(original_projected.current.player, CombatPlayer)
    assert original_projected.current.player.powers[0].amount == 0


def test_generated_directories_keep_stable_keys_and_card_order_through_changes() -> None:
    observation = Observation.model_validate_json(COMBAT)
    first_card = observation.generated["c1"][0]
    second_card = first_card.model_copy(
        update={"source": first_card.source.model_copy(update={"definition_id": "CARD.TWO"})}
    )
    generated = {"c2": (second_card, first_card), "c1": (first_card,)}
    observation = observation.model_copy(update={"generated": MappingProxyType(generated)})
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    first = context.model_input()[-1]
    assert isinstance(first, TaskMessage)
    initial = ActContent.model_validate_json(first.text).observation
    assert initial.current is not MISSING and initial.current.generated is not MISSING
    assert [entry.source.ref for entry in initial.current.generated] == ["c1", "c2"]
    assert [entry.source.definition_id for entry in initial.current.generated[1].previews] == [
        "CARD.TWO",
        "CARD.ONE",
    ]
    assert initial.current.piles is not MISSING and initial.current.piles is not None
    assert tuple(item.ref for item in initial.current.piles.hand) == ("c2", "c1")
    changed = {"c2": (first_card,), "c1": (second_card, first_card)}
    call = ToolCall("inspect", "combat_inspect", "{}")
    for directory in (changed, {}):
        observation = observation.model_copy(update={"generated": MappingProxyType(directory)})
        context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
        result = context.model_input()[-1]
        assert isinstance(result, RunResultMessage)
        projected = RunProjectedResult.model_validate_json(result.text).observation
        assert projected is not None and projected.current is not MISSING
        if not directory:
            assert projected.current.generated is MISSING
            assert projected.current.removed is not MISSING
            assert projected.current.removed.generated == ("c1", "c2")
            continue
        assert projected.current.generated is not MISSING
        assert [entry.source.ref for entry in projected.current.generated] == ["c1", "c2"]
        assert [
            tuple(item.source.definition_id for item in entry.previews)
            for entry in projected.current.generated
        ] == [("CARD.TWO", "CARD.ONE"), ("CARD.ONE",)]


def test_generated_sources_clear_and_reset_while_mechanisms_keep_owner_semantics() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("inspect", "combat_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    first = context.model_input()[-1]
    assert isinstance(first, RunResultMessage)
    assert (
        first.content.observation is not None and first.content.observation.current is not MISSING
    )
    assert first.content.observation.current.generated is not MISSING
    assert [
        (item.source.ref, len(item.previews))
        for item in first.content.observation.current.generated
    ] == [("c1", 1), ("c2", 0)]
    same_definition_new_source = observation.model_copy(
        update={
            "generated": MappingProxyType(
                {**observation.generated, "c2": observation.generated["c1"]}
            )
        }
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), same_definition_new_source))
    second = context.model_input()[-1]
    assert isinstance(second, RunResultMessage)
    assert (
        second.content.observation is not None and second.content.observation.current is not MISSING
    )
    assert second.content.observation.current.generated is not MISSING
    assert [
        (item.source.ref, len(item.previews))
        for item in second.content.observation.current.generated
    ] == [("c2", 1)]
    cleared = observation.model_copy(update={"generated": MappingProxyType({})})
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), cleared))
    empty = context.model_input()[-1]
    assert isinstance(empty, RunResultMessage)
    assert (
        empty.content.observation is not None and empty.content.observation.current is not MISSING
    )
    assert empty.content.observation.current.generated is MISSING
    assert empty.content.observation.current.removed is not MISSING
    assert empty.content.observation.current.removed.generated == ("c1", "c2")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    returned = context.model_input()[-1]
    assert isinstance(returned, RunResultMessage)
    assert (
        returned.content.observation is not None
        and returned.content.observation.current is not MISSING
    )
    assert returned.content.observation.current.generated is not MISSING
    assert [
        (item.source.ref, len(item.previews))
        for item in returned.content.observation.current.generated
    ] == [("c1", 1), ("c2", 0)]
    changed_text = observation.mechanisms[0].model_copy(update={"text": "甲变化"})
    same_owner_same_id = changed_text.model_copy(update={"text": "同 ID 的第二条正文"})
    mechanisms = observation.model_copy(
        update={"mechanisms": (changed_text, same_owner_same_id, observation.mechanisms[1])}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), mechanisms))
    current = context.model_input()[-1]
    assert isinstance(current, RunResultMessage)
    assert (
        current.content.observation is not None
        and current.content.observation.current is not MISSING
    )
    assert current.content.observation.current.mechanisms is not MISSING
    assert len(current.content.observation.current.mechanisms) == 2
    assert current.content.observation.current.removed is not MISSING
    assert current.content.observation.current.removed.mechanisms is not MISSING
    assert len(current.content.observation.current.removed.mechanisms) == 1
    assert "甲变化" in current.text and "同 ID 的第二条正文" in current.text
    next_node = observation.model_copy(
        update={"location": observation.location.model_copy(update={"node_id": "next"})}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    reset = context.model_input()[-1]
    assert isinstance(reset, RunResultMessage)
    assert reset.content.observation is not None
    assert reset.content.observation.scene is not MISSING
    assert {item.ref for item in reset.content.observation.scene.bindings} == set(
        observation.objects
    )
    assert reset.content.observation.current is not MISSING
    assert reset.content.observation.current.generated is not MISSING
    assert [
        (item.source.ref, len(item.previews))
        for item in reset.content.observation.current.generated
    ] == [("c1", 1), ("c2", 0)]
    assert reset.content.observation.current.mechanisms is not MISSING
    assert len(reset.content.observation.current.mechanisms) == len(observation.mechanisms)
    empty_mechanisms = next_node.model_copy(update={"mechanisms": ()})
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), empty_mechanisms))
    cleared_mechanisms = context.model_input()[-1]
    assert isinstance(cleared_mechanisms, RunResultMessage)
    assert cleared_mechanisms.content.observation is not None
    assert cleared_mechanisms.content.observation.current is not MISSING
    assert cleared_mechanisms.content.observation.current.mechanisms is MISSING
    assert cleared_mechanisms.content.observation.current.removed is not MISSING
    assert (
        cleared_mechanisms.content.observation.current.removed.mechanisms
        == reset.content.observation.current.mechanisms
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    restored_mechanisms = context.model_input()[-1]
    assert isinstance(restored_mechanisms, RunResultMessage)
    assert (
        restored_mechanisms.content.observation is not None
        and restored_mechanisms.content.observation.current is not MISSING
    )
    assert restored_mechanisms.content.observation.current.mechanisms is not MISSING
    assert len(restored_mechanisms.content.observation.current.mechanisms) == len(
        observation.mechanisms
    )


def test_foreign_role_menus_and_their_details_do_not_leak_or_change_raw_observation() -> None:

    reward = Observation.model_validate_json(
        RUN.replace('"screen":"map"', '"screen":"rewards"')
        .replace(
            '"objects":{}',
            '"objects":{"reward":{"kind":"option","name":"奖励选项"}}',
        )
        .replace('"descriptions":{}', '"descriptions":{"reward":"奖励正文"}')
        .replace('"enabled":false', '"subject":"reward","enabled":false')
    )
    assert isinstance(reward.state, RunState)
    card = Observation.model_validate_json(COMBAT).objects["c1"]
    assert isinstance(card, CardEntry)
    local_keyword = MechanismView(
        kind=MechanismKind.KEYWORD, definition_id="KW.LOCAL", name="局部关键词", text="保留卡牌机制"
    )
    reward_only = MechanismView(
        kind=MechanismKind.KEYWORD,
        definition_id="KW.REWARD",
        name="奖励关键词",
        text="奖励专属机制",
    )
    reward = reward.model_copy(
        update={
            "state": reward.state.model_copy(
                update={
                    "deck": ("d1",),
                    "actions": tuple(
                        action.model_copy(update={"subject": "reward_card"})
                        for action in reward.state.actions
                    ),
                }
            ),
            "objects": {
                **reward.objects,
                "d1": card.model_copy(update={"keywords": (), "local_keywords": ("KW.LOCAL",)}),
                "reward_card": card.model_copy(update={"local_keywords": ("KW.REWARD",)}),
            },
            "mechanisms": (local_keyword, reward_only),
        }
    )
    raw = reward.to_json()
    context = Context(DecisionOwner.COMBAT)
    context.append_tool_result(
        ToolCall("finish", "combat_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), reward),
    )
    final = context.model_input()[-1]
    assert isinstance(final, RunResultMessage)
    projected = final.content.observation
    assert projected is not None
    assert projected.decision.phase.value == "run"
    assert projected.decision.actions == ()
    assert projected.decision.selection is None
    assert '"offers"' not in final.text
    assert "奖励正文" not in final.text
    assert "奖励选项" not in final.text
    assert projected.scene is not MISSING
    assert {item.ref for item in projected.scene.bindings} == {"d1"}
    assert projected.current is not MISSING and projected.current.mechanisms is not MISSING
    assert [item.definition_id for item in projected.current.mechanisms] == ["KW.LOCAL"]
    assert "保留卡牌机制" in final.text
    assert "奖励专属机制" not in final.text

    assert reward.to_json() == raw

    terminal = Observation.model_validate_json(
        RUN.replace(
            '"phase":"run","screen":"map","control":"ready"',
            '"phase":"terminal","screen":"run_complete","control":"unavailable"',
        )
        .replace('"owner":"run"', '"owner":null')
        .replace('"input_readiness":"ready"', '"input_readiness":"terminal"')
        .replace('[{"ref":"travel","label":"前进","enabled":false,"reason":"等待"}]', "[]")
        .replace('"offers":[],"event":null', '"result":"victory"')
    )
    assert isinstance(terminal.state, TerminalState)
    combat = Observation.model_validate_json(COMBAT).state
    assert isinstance(combat, CombatState)
    player_power = Power(name="玩家状态", definition_id="POWER.PLAYER")
    enemy_power = Power(name="敌人状态", definition_id="POWER.ENEMY")
    terminal_mechanisms = tuple(
        MechanismView(
            kind=MechanismKind.POWER,
            definition_id=power.definition_id,
            name=power.name,
            text=power.name + "机制",
        )
        for power in (player_power, enemy_power)
    )
    terminal = terminal.model_copy(
        update={
            "state": terminal.state.model_copy(
                update={
                    "player": terminal.state.player.model_copy(update={"powers": (player_power,)}),
                    "enemies": (combat.enemies[0].model_copy(update={"powers": (enemy_power,)}),),
                }
            ),
            "mechanisms": (*terminal_mechanisms, reward_only),
        }
    )
    context.append_tool_result(
        ToolCall("terminal", "combat_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), terminal),
    )
    final = context.model_input()[-1]
    assert isinstance(final, RunResultMessage)
    assert (
        final.content.observation is not None and final.content.observation.current is not MISSING
    )
    assert final.content.observation.current.mechanisms is not MISSING
    assert {item.definition_id for item in final.content.observation.current.mechanisms} == {
        "POWER.PLAYER",
        "POWER.ENEMY",
    }
    assert "玩家状态机制" in final.text
    assert "敌人状态机制" in final.text
    assert "奖励专属机制" not in final.text

    run_context = Context(DecisionOwner.RUN)
    run_context.append_tool_result(
        ToolCall("terminal", "run_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), terminal),
    )
    run_final = run_context.model_input()[-1]
    assert isinstance(run_final, RunResultMessage)
    projection = run_final.content.observation
    assert projection is not None and projection.current is not MISSING
    assert "enemies" not in projection.current.model_dump(mode="json")
    player = projection.current.model_dump(mode="json")["player"]
    assert isinstance(player, dict) and "powers" not in player
    assert projection.current.model_dump(mode="json")["mechanisms"] == []


def test_task_omission_uses_only_the_actual_tail_and_preserves_native_assistant() -> None:

    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("act", "combat_act", "{}")
    context.append_tool_result(
        call, ActionToolResult(ActionCompleted(completed_steps=1), observation)
    )
    context.append_task(CombatActInput(), observation)
    assert len(context.model_input()) == 1
    context.append_task(CombatActInput(opening_notes="固定开场笔记"), observation)
    assert len(context.model_input()) == 2
    context.append_task(CombatActInput(opening_notes="固定开场笔记"), observation)
    assert len(context.model_input()) == 2
    assistant = AssistantMessage(
        ResponsesReply.from_json(
            '[{"type":"reasoning","id":"reasoning","summary":[],"encrypted_content":"opaque"},'
            '{"type":"function_call","call_id":"inspect","name":"combat_inspect","arguments":"{}"}]'
        )
    )
    context.append_reply(assistant)
    context.append_task(CombatActInput(opening_notes="固定开场笔记"), observation)
    assert context.model_input()[-2] is assistant
    assert len(context.model_input()) == 4
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    assert isinstance(context.model_input()[-1], RunResultMessage)
    context.append_task(CombatActInput(), observation)
    assert len(context.model_input()) == 6
    # Technical acquisition identity does not create a different gameplay observation.
    refreshed = observation.model_copy(
        update={"stamp": observation.stamp.model_copy(update={"observation_sequence": 99})}
    )
    context.append_task(CombatActInput(), refreshed)
    assert len(context.model_input()) == 6


def test_review_appends_its_actual_report_without_unrelated_live_state() -> None:

    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(opening_notes="固定开场笔记"), observation)
    report = BattleReport(
        location=BattleLocation(act=1, floor=1, room_type="monster"),
        result=TerminalResult.VICTORY,
        last_observed_turn=3,
        before=BoundaryValues(hp=10, max_hp=10, gold=0),
        after=BoundaryValues(hp=8, max_hp=10, gold=0),
    )
    context.append_task(CombatReviewInput(report))
    review = context.model_input()[-1]
    assert isinstance(review, TaskMessage)
    assert isinstance(review.content, ReviewContent)
    assert review.content.battle_report is report
    assert review.observation is None
    assert '"state"' not in review.text
    assert '"battle_report"' in review.text


def test_partial_and_failed_reads_preserve_delivery_and_run_map_sections() -> None:

    observation = Observation.model_validate_json(COMBAT)
    shared = MechanismView(
        kind=MechanismKind.KEYWORD,
        definition_id="KEYWORD.EXHAUST",
        name="消耗",
        text="离开正常牌堆循环",
    )
    observation = observation.model_copy(update={"mechanisms": (*observation.mechanisms, shared)})
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("inspect", "combat_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    details = DetailData(
        objects={}, descriptions={}, modifier_descriptions={}, generated={}, mechanisms=()
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), None, details))
    context.append_tool_result(
        call,
        QueryToolResult(
            Failed(
                reason=Reason(code=ReasonCode.QUERY_FAILED, message="Read failed."),
                suggested_actions=(Suggestion(message="Inspect the current state."),),
            ),
            None,
        ),
    )
    failed = context.model_input()[-1]
    assert isinstance(failed, RunResultMessage)
    assert failed.content.observation is None
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    same = context.model_input()[-1]
    assert isinstance(same, RunResultMessage)
    assert same.content.observation is not None
    assert same.content.observation.current is MISSING
    next_node = observation.model_copy(
        update={"location": observation.location.model_copy(update={"node_id": "next"})}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    reset = context.model_input()[-1]
    assert isinstance(reset, RunResultMessage)
    assert (
        reset.content.observation is not None and reset.content.observation.current is not MISSING
    )
    assert reset.content.observation.current.mechanisms is not MISSING
    assert len(reset.content.observation.current.mechanisms) == len(observation.mechanisms)

    run = Observation.model_validate_json(RUN).model_copy(update={"map": observation.map})
    run_context = Context(DecisionOwner.RUN)
    run_context.append_tool_result(call, QueryToolResult(ReadCompleted(), run))
    first = run_context.model_input()[-1]
    assert isinstance(first, RunResultMessage)
    assert first.content.observation is not None
    assert first.content.observation.current is not MISSING
    assert observation.map is not MISSING
    topology = first.content.observation.current.map_topology
    dynamic = first.content.observation.current.dynamic_map
    assert topology is not MISSING and topology is not None
    assert dynamic is not MISSING and dynamic is not None
    assert topology.bosses == observation.map.bosses
    assert dynamic.legal_destinations == observation.map.legal_destinations
    next_run_node = run.model_copy(
        update={"location": run.location.model_copy(update={"node_id": "next"})}
    )
    run_context.append_tool_result(
        call, QueryToolResult(ReadCompleted(), next_run_node, MapData(map=observation.map))
    )
    explicit = run_context.model_input()[-1]
    assert isinstance(explicit, RunResultMessage)
    assert explicit.content.observation is not None
    assert explicit.content.observation.current is not MISSING
    assert explicit.content.observation.current.map_topology is MISSING
    assert explicit.content.observation.current.dynamic_map is MISSING
    assert isinstance(explicit.content.data, MapData)
    assert explicit.content.data.map is observation.map
    missing_map = next_run_node.model_copy(update={"map": MISSING})
    run_context.append_tool_result(call, QueryToolResult(ReadCompleted(), missing_map))
    cleared = run_context.model_input()[-1]
    assert isinstance(cleared, RunResultMessage)
    assert cleared.content.observation is not None
    assert cleared.content.observation.current is not MISSING
    assert cleared.content.observation.current.map_topology is None
    assert cleared.content.observation.current.dynamic_map is None
    fresh = Context(DecisionOwner.RUN)
    fresh.append_tool_result(call, QueryToolResult(ReadCompleted(), run))
    fresh_message = fresh.model_input()[-1]
    assert isinstance(fresh_message, RunResultMessage)
    assert fresh_message.content.observation is not None
    assert fresh_message.content.observation.current is not MISSING
    assert fresh_message.content.observation.current.map_topology == topology
    assert fresh_message.content.observation.current.dynamic_map == dynamic


def test_run_combat_result_keeps_permanent_facts_without_combat_internals() -> None:
    raw = COMBAT.replace(
        '"game_language":"zhs",',
        '"game_language":"zhs","encounter":{"name":"遭遇","definition_id":"ENCOUNTER.ONE"},',
    )
    observation = Observation.model_validate_json(raw)
    original = observation.to_json()
    context = Context(DecisionOwner.RUN)
    context.append_tool_result(
        ToolCall("start", "run_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), observation),
    )
    result = context.model_input()[-1]
    assert isinstance(result, RunResultMessage)
    projected = result.content.observation
    assert projected is not None and projected.current is not MISSING
    current = projected.current.model_dump(mode="json")
    assert current["deck"] == [{"ref": "c1", "name": "牌", "definition_id": "CARD.ONE"}]
    assert current["encounter"] == {"name": "遭遇", "definition_id": "ENCOUNTER.ONE"}
    player = current["player"]
    assert isinstance(player, dict) and player["hp"] == 10
    assert not {"turn", "piles", "enemies", "inactive_enemies", "allies", "orbs"} & current.keys()
    assert not {"block", "powers", "resources"} & player.keys()
    assert projected.current.cards is not MISSING and len(projected.current.cards) == 1
    assert current["mechanisms"] == []
    assert observation.to_json() == original

    combat = Context(DecisionOwner.COMBAT)
    combat.append_task(CombatActInput(), observation)
    combat_message = combat.model_input()[-1]
    assert isinstance(combat_message, TaskMessage)
    assert isinstance(combat_message.content, ActContent)
    combat_projection = combat_message.content.observation
    assert combat_projection.current is not MISSING
    assert combat_projection.current.piles is not MISSING
    assert combat_projection.current.actors is not MISSING
    assert combat_projection.scene is not MISSING
    assert {item.ref for item in combat_projection.scene.bindings} == set(observation.objects)


def test_run_retains_encounter_only_within_the_same_battle() -> None:
    raw = COMBAT.replace(
        '"game_language":"zhs",',
        '"game_language":"zhs","encounter":{"name":"遭遇","definition_id":"ENCOUNTER.ONE"},',
    )
    observation = Observation.model_validate_json(raw)
    missing = observation.model_copy(update={"encounter": MISSING})
    next_node = missing.model_copy(
        update={"location": missing.location.model_copy(update={"node_id": "next"})}
    )
    context = Context(DecisionOwner.RUN)
    call = ToolCall("start", "run_act", "{}")
    for current in (observation, missing, next_node):
        context.append_tool_result(
            call, ActionToolResult(ActionCompleted(completed_steps=1), current)
        )
    first, second, third = context.model_input()
    assert isinstance(first, RunResultMessage)
    assert isinstance(second, RunResultMessage)
    assert isinstance(third, RunResultMessage)
    assert (
        first.content.observation is not None and first.content.observation.current is not MISSING
    )
    assert first.content.observation.current.model_dump(mode="json")["encounter"] == {
        "name": "遭遇",
        "definition_id": "ENCOUNTER.ONE",
    }
    assert second.content.observation is not None
    assert second.content.observation.current is MISSING
    assert (
        third.content.observation is not None and third.content.observation.current is not MISSING
    )
    assert third.content.observation.current.model_dump(mode="json")["encounter"] is None


def test_fake_http_battle_transition_preserves_run_handoff_and_notes() -> None:
    async def scenario() -> None:
        responses = iter((RUN, COMBAT, RUN.replace('"hp":10', '"hp":8')))
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content='{"status":"completed","observation":' + next(responses) + "}"
                )
            ),
        ) as client:
            observed = [await client.state() for _ in range(3)]
        assert all(isinstance(item, StateCompleted) for item in observed)
        before, during, after = (
            item.observation for item in observed if isinstance(item, StateCompleted)
        )
        context = Context(DecisionOwner.RUN)
        context.append_task(RunActInput(strategy_notes="Save potion."), before)
        context.append_tool_result(
            ToolCall("start", "run_act", "{}"),
            ActionToolResult(ActionCompleted(completed_steps=1), during),
        )
        report = BattleReport(
            location=BattleLocation(act=1, floor=1, room_type="monster"),
            result=TerminalResult.UNKNOWN,
            last_observed_turn=2,
            before=BoundaryValues(hp=10, max_hp=10, gold=0),
            after=BoundaryValues(hp=8, max_hp=10, gold=0),
        )
        context.append_task(RunHandoffInput(CombatHandoff(report, "Need faster damage.")))
        context.append_task(RunActInput(strategy_notes="Save potion."), after)
        messages = context.model_input()
        assert "Save potion." in messages[0].text
        assert '"deck":[{"ref":"c1"' in messages[1].text
        assert '"piles"' not in messages[1].text
        assert '"enemies"' not in messages[1].text
        assert "Need faster damage." in messages[2].text
        assert '"hp":8' in messages[2].text
        assert '"hp":8' in messages[3].text
        assert "Save potion." in messages[3].text

    asyncio.run(scenario())


def test_run_compaction_candidate_uses_prior_group_baseline_and_keeps_active_query() -> None:
    import json

    observation = Observation.model_validate_json(RUN)
    assert isinstance(observation.state, RunState)
    player = observation.state.player
    before = observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={"player": player.model_copy(update={"gold": 100})}
            )
        }
    )
    after = observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={"player": player.model_copy(update={"gold": 90})}
            )
        }
    )
    context = Context(DecisionOwner.RUN)
    context.begin_group(before, "old notes")
    context.append_task(RunActInput(strategy_notes="old notes"), before)
    context.append_correction("Older completed group")
    context.finish_group()
    context.begin_group(before, "current notes")
    call = ToolCall("a1", "run_act", "{}")
    assistant = AssistantMessage(
        ResponsesReply.from_json(
            json.dumps(
                [{"type": "function_call", "call_id": "a1", "name": "run_act", "arguments": "{}"}]
            )
        )
    )
    context.append_reply(assistant)
    context.append_tool_result(call, ActionToolResult(ActionCompleted(completed_steps=1), after))
    context.finish_group()
    context.begin_group(after, "current notes")
    context.append_task(RunActInput(strategy_notes="current notes"), after)
    query = AssistantMessage(
        ResponsesReply.from_json(
            json.dumps(
                [{"type": "function_call", "call_id": "q1", "name": "run_notes", "arguments": "{}"}]
            )
        )
    )
    context.append_reply(query)
    context.append_tool_result(
        ToolCall("q1", "run_notes", "{}"), QueryToolResult(ReadCompleted(), after)
    )
    original = context.model_input()
    candidate = context.compaction_candidate()
    assert context.model_input() == original
    baseline = json.loads(candidate[0].text)["context_baseline"]
    assert baseline["observation"]["current"]["player"]["gold"] == 100
    assert baseline["strategy_notes"] == "current notes"
    assert baseline["route_plan"] is None
    assert all("Older completed group" not in message.text for message in candidate)
    assert [
        message.tool_calls[0].id for message in candidate if isinstance(message, AssistantMessage)
    ] == ["a1", "q1"]
    assert [message.call_id for message in candidate if isinstance(message, RunResultMessage)] == [
        "a1",
        "q1",
    ]


def test_minimal_context_preserves_identical_baseline_for_unchanged_state() -> None:
    for role, source, task in (
        (DecisionOwner.RUN, RUN, RunActInput()),
        (DecisionOwner.COMBAT, COMBAT, CombatActInput()),
    ):
        observation = Observation.model_validate_json(source)
        context = Context(role, minimal=True)
        context.begin_group(observation, "")
        context.append_task(task, observation)
        first = context.model_input()[0].text
        context.finish_group()
        context.begin_group(observation, "")
        context.append_task(task, observation)
        assert context.model_input()[0].text == first


def test_baseline_preserves_static_descriptions_before_changing_combat_state() -> None:
    import json

    observation = Observation.model_validate_json(COMBAT)
    assert isinstance(observation.state, CombatState)
    changed = observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "turn": 2,
                    "player": observation.state.player.model_copy(update={"hp": 8}),
                    "enemies": tuple(
                        enemy.model_copy(update={"block": 1}) for enemy in observation.state.enemies
                    ),
                }
            )
        }
    )
    for minimal in (False, True):
        baselines: list[str] = []
        for current in (observation, changed):
            context = Context(DecisionOwner.COMBAT, minimal=minimal)
            context.begin_group(current, "")
            context.append_task(CombatActInput(), current)
            candidate, baseline = context.compaction_input()
            context.validate_compaction(candidate)
            context.install_compaction(candidate)
            baselines.append(baseline.text)
        shared = commonprefix(baselines)
        assert "造成3点伤害。" in shared
        assert "附魔说明" in shared
        assert '"deck"' in shared
        assert '"inventory"' in shared
        before, after = (json.loads(text)["context_baseline"]["observation"] for text in baselines)
        assert before["current"]["player"]["hp"] == 10
        assert after["current"]["player"]["hp"] == 8
        assert after["current"]["actors"]["enemies"][0]["block"] == 1


def test_baseline_preserves_map_prefix_across_run_scene_changes() -> None:
    import json

    combat = Observation.model_validate_json(COMBAT)
    observation = Observation.model_validate_json(RUN).model_copy(update={"map": combat.map})
    assert isinstance(observation.state, RunState)
    changed = observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"node_id": "next-node"}),
            "state": observation.state.model_copy(
                update={"player": observation.state.player.model_copy(update={"gold": 90})}
            ),
        }
    )
    for minimal in (False, True):
        baselines: list[str] = []
        for current in (observation, changed):
            context = Context(DecisionOwner.RUN, minimal=minimal)
            context.begin_group(current, "")
            context.append_task(RunActInput(), current)
            candidate, baseline = context.compaction_input()
            context.validate_compaction(candidate)
            baselines.append(baseline.text)
        shared = commonprefix(baselines)
        assert "ENCOUNTER.BOSS" in shared
        assert '"map_topology"' in shared
        before, after = (json.loads(text)["context_baseline"]["observation"] for text in baselines)
        assert before["scene"]["node_id"] == "node"
        assert after["scene"]["node_id"] == "next-node"
        assert after["current"]["player"]["gold"] == 90


def test_minimal_context_discards_completed_decision_and_delivers_current_task() -> None:
    import json

    observation = Observation.model_validate_json(COMBAT)
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    upgraded = observation.model_copy(
        update={"objects": {**observation.objects, "c1": card.model_copy(update={"upgrades": 1})}}
    )
    context = Context(DecisionOwner.COMBAT, minimal=True)
    context.begin_group(observation, "Opening notes must not be delivered")
    context.append_task(CombatActInput(), observation)
    context.append_correction("Old decision reasoning")
    context.append_reply(
        AssistantMessage(
            ResponsesReply.from_json(
                '[{"type":"function_call","call_id":"a1","name":"combat_act","arguments":"{}"}]'
            )
        )
    )
    context.append_tool_result(
        ToolCall("a1", "combat_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), upgraded),
    )
    context.finish_group()
    context.begin_group(upgraded, "Opening notes must not be delivered")
    context.append_task(CombatActInput(), upgraded)

    messages = context.model_input()
    assert all("Old decision reasoning" not in message.text for message in messages)
    assert not any(
        isinstance(message, (AssistantMessage, RunResultMessage)) for message in messages
    )
    assert isinstance(messages[-1], TaskMessage)
    assert isinstance(messages[-1].content, ActContent)
    baseline = json.loads(messages[0].text)["context_baseline"]
    assert "opening_notes" not in baseline
    binding = next(
        item for item in baseline["observation"]["scene"]["bindings"] if item["ref"] == "c1"
    )
    profile = baseline["observation"]["catalog_additions"][binding["profile"]]
    assert profile["content"]["upgrades"] == 1
    context.validate_compaction(context.compaction_candidate())


def test_minimal_run_context_keeps_active_queries_guidance_and_route_plan() -> None:
    import json

    from sts2_harness.knowledge import DocumentView
    from sts2_harness.material_text import MaterialText

    observation = Observation.model_validate_json(RUN)
    context = Context(DecisionOwner.RUN, minimal=True)
    guidance = DocumentView(
        id="automatic:current-act",
        name=MaterialText.create("automatic:name", "Current act guidance"),
        text=MaterialText.create("automatic:text", "Keep this automatic guidance."),
        resources=(),
    )
    context.append_materials((guidance,))
    plan: JsonValue = {"reason": "Reach a rest site", "edges": [{"from": "n1", "to": "n2"}]}
    context.begin_group(observation, "Notes must not be delivered", plan)
    context.append_task(RunActInput(), observation)
    first = context.model_input()
    context.append_reply(
        AssistantMessage(
            ResponsesReply.from_json(
                '[{"type":"function_call","call_id":"q1","name":"run_inspect","arguments":"{}"}]'
            )
        )
    )
    context.append_tool_result(
        ToolCall("q1", "run_inspect", "{}"),
        QueryToolResult(
            Failed(
                reason=Reason(code=ReasonCode.QUERY_FAILED, message="Current query failed"),
                suggested_actions=(Suggestion(message="Inspect again if needed."),),
            ),
            observation,
        ),
    )
    continuing = context.model_input()
    context.begin_group(observation, "Different notes", None)
    context.append_task(RunActInput(), observation)
    assert context.model_input()[: len(continuing)] == continuing
    assert continuing[: len(first)] == first
    assert "Current query failed" in continuing[-1].text
    context.validate_compaction(context.compaction_candidate())

    context.finish_group()
    context.begin_group(observation, "Notes must not be delivered", plan)
    context.append_task(RunActInput(), observation)
    messages = context.model_input()
    baseline = next(
        json.loads(message.text)["context_baseline"]
        for message in messages
        if '"context_baseline"' in message.text
    )
    assert baseline["route_plan"] == plan
    assert "strategy_notes" not in baseline
    assert any("Keep this automatic guidance." in message.text for message in messages)
    assert all("Current query failed" not in message.text for message in messages)
    assert isinstance(messages[-1], TaskMessage)
    context.validate_compaction(context.compaction_candidate())


def test_minimal_run_decision_starts_from_current_node_instead_of_previous_node() -> None:
    import json

    observation = Observation.model_validate_json(RUN)
    assert isinstance(observation.state, RunState)
    changed = observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"node_id": "next-node"}),
            "state": observation.state.model_copy(
                update={"player": observation.state.player.model_copy(update={"gold": 23})}
            ),
        }
    )
    context = Context(DecisionOwner.RUN, minimal=True)
    context.begin_group(observation, "")
    context.append_task(RunActInput(), observation)
    context.finish_group()
    context.begin_group(changed, "")
    context.append_task(RunActInput(), changed)

    messages = context.model_input()
    baseline = json.loads(messages[0].text)["context_baseline"]["observation"]
    assert baseline["scene"]["node_id"] == "next-node"
    assert baseline["current"]["player"]["gold"] == 23
    task = messages[-1]
    assert isinstance(task, TaskMessage)
    assert isinstance(task.content, RunActContent)
    assert task.content.observation.scene is MISSING
    assert task.content.observation.current is MISSING
    context.validate_compaction(context.compaction_candidate())


def test_run_compaction_retains_historical_profile_and_later_upgrade() -> None:
    import json

    observation = Observation.model_validate_json(RUN)
    combat = Observation.model_validate_json(COMBAT)
    assert isinstance(observation.state, RunState)
    card = combat.objects["c1"]
    assert isinstance(card, CardEntry)
    initial = observation.model_copy(
        update={
            "state": observation.state.model_copy(update={"deck": ("c1",)}),
            "objects": {"c1": card},
            "descriptions": {"c1": "Before upgrade."},
        }
    )
    upgraded = initial.model_copy(
        update={
            "objects": {"c1": card.model_copy(update={"upgrades": 1})},
            "descriptions": {"c1": "After upgrade."},
        }
    )
    context = Context(DecisionOwner.RUN)
    context.begin_group(initial, "notes")
    context.append_task(RunActInput(), initial)
    context.finish_group()
    context.begin_group(initial, "notes")
    action = AssistantMessage(
        ResponsesReply.from_json(
            json.dumps(
                [{"type": "function_call", "call_id": "a", "name": "run_act", "arguments": "{}"}]
            )
        )
    )
    context.append_reply(action)
    context.append_tool_result(
        ToolCall("a", "run_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), upgraded),
    )
    context.finish_group()
    context.begin_group(upgraded, "notes")
    context.append_task(RunActInput(strategy_notes="notes"), upgraded)
    messages = context.compaction_candidate()
    baseline = json.loads(messages[0].text)["context_baseline"]["observation"]
    binding = baseline["scene"]["bindings"][0]
    assert baseline["catalog_additions"][binding["profile"]]["content"]["upgrades"] == 0
    assert baseline["current"]["card_descriptions"][0]["text"] == "Before upgrade."
    result = next(message for message in messages if isinstance(message, RunResultMessage))
    assert result.content.observation is not None
    later = json.loads(result.text)["observation"]
    new_binding = later["scene"]["bindings"][0]
    assert later["catalog_additions"][new_binding["profile"]]["content"]["upgrades"] == 1
    assert later["current"]["card_descriptions"][0]["text"] == "After upgrade."
    context.install_compaction(messages)
    assert context.model_input() == messages


def test_run_compaction_of_handoff_before_first_observation_preserves_baseline_state() -> None:
    observation = Observation.model_validate_json(RUN)
    report = BattleReport(
        location=BattleLocation(act=1, floor=1, room_type="monster"),
        result=TerminalResult.UNKNOWN,
        last_observed_turn=2,
        before=BoundaryValues(hp=10, max_hp=10, gold=0),
        after=BoundaryValues(hp=8, max_hp=10, gold=0),
    )
    context = Context(DecisionOwner.RUN)
    context.begin_group(observation, "notes")
    context.append_task(RunHandoffInput(CombatHandoff(report, None)))
    candidate = context.compaction_candidate()
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    context.finish_group()
    context.begin_group(observation, "notes")
    context.append_task(RunActInput(), observation)
    task = context.model_input()[-1]
    assert isinstance(task, TaskMessage)
    assert isinstance(task.content, RunActContent)
    assert task.content.observation.scene is MISSING
    assert task.content.observation.current is MISSING
