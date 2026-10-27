from types import MappingProxyType

from pydantic import MISSING
from test_mod_client import COMBAT, RUN

from sts2_harness.client.models import (
    CardEntry,
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
    TerminalResult,
    TerminalState,
)
from sts2_harness.context import (
    ActionToolResult,
    ActTask,
    Context,
    QueryToolResult,
    ReadCompleted,
    ReviewTask,
)
from sts2_harness.context.models import (
    ActContent,
    BattleLocation,
    BattleReport,
    BoundaryValues,
    ResultMessage,
    ReviewContent,
    TaskMessage,
)
from sts2_harness.context.results import ActionCompleted, Failed, Reason, ReasonCode, Suggestion
from sts2_harness.llm.messages import AssistantMessage, ResponsesReply, ToolCall


def test_task_delivers_complete_state_and_latest_details_without_rewriting_history() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.append_task(ActTask(), observation)
    first_input = context.model_input()
    assert len(first_input) == 1
    first = first_input[0]
    assert isinstance(first, TaskMessage)
    assert isinstance(first.content, ActContent)
    assert isinstance(first.content.observation.state, CombatState)
    assert first.content.observation.state.piles.hand == ("c2", "c1")
    assert first.content.observation.updates.objects is not MISSING
    assert first.content.observation.updates.objects["c1"].name == "牌"
    context.append_task(ActTask(), observation)
    assert context.model_input() == first_input


def test_results_deliver_latest_changes_and_full_explicit_data_independently() -> None:

    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("inspect", "combat_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    original = context.model_input()[0]
    assert isinstance(original, ResultMessage)
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    changed = Observation(
        stamp=observation.stamp,
        location=observation.location,
        input_readiness=observation.input_readiness,
        state=observation.state,
        objects={
            **observation.objects,
            "c1": card.model_copy(update={"cost": ResourceCosts(energy=0)}),
        },
        descriptions={**observation.descriptions, "c1": "新正文"},
        modifier_descriptions={"c1": ModifierDescriptions(enchantment=None, affliction=None)},
        generated=observation.generated,
        mechanisms=observation.mechanisms,
        upgrades=observation.upgrades,
        map=observation.map,
    )
    numeric_only = Observation(
        stamp=changed.stamp,
        location=changed.location,
        input_readiness=changed.input_readiness,
        state=changed.state,
        objects=changed.objects,
        descriptions=observation.descriptions,
        modifier_descriptions=observation.modifier_descriptions,
        generated=changed.generated,
        mechanisms=changed.mechanisms,
        upgrades=changed.upgrades,
        map=changed.map,
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), numeric_only))
    numeric_message = context.model_input()[-1]
    assert isinstance(numeric_message, ResultMessage)
    assert numeric_message.content.updates.objects == {"c1": changed.objects["c1"]}
    assert numeric_message.content.updates.descriptions is MISSING
    assert numeric_message.content.updates.modifier_descriptions is MISSING
    details = DetailData(
        objects=changed.objects,
        descriptions=changed.descriptions,
        modifier_descriptions=changed.modifier_descriptions,
        generated={},
        mechanisms=(),
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), changed, details))
    changed_message = context.model_input()[-1]
    assert isinstance(changed_message, ResultMessage)
    assert changed_message.content.data is details
    assert changed_message.content.updates.objects is MISSING
    assert changed_message.content.updates.descriptions == {"c1": "新正文"}
    assert changed_message.content.updates.modifier_descriptions == {
        "c1": ModifierDescriptions(enchantment=None, affliction=None)
    }
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), changed))
    unchanged = context.model_input()[-1]
    assert isinstance(unchanged, ResultMessage)
    assert unchanged.content.updates.to_json() == "{}"
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    returned = context.model_input()[-1]
    assert isinstance(returned, ResultMessage)
    assert returned.content.updates.descriptions == {"c1": None}
    assert original.content.updates.objects is not MISSING
    assert original.content.updates.objects["c1"] == card
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
    assert isinstance(moved, ResultMessage)
    assert isinstance(moved.content.state, CombatState)
    assert moved.content.state.piles.hand == ("c2",)
    assert moved.content.state.piles.discard == ("c1",)
    assert moved.content.state.player.powers == ()
    assert moved.content.updates.objects is MISSING
    assert isinstance(original.content.state, CombatState)
    assert original.content.state.player.powers[0].amount == 0


def test_generated_sources_clear_and_reset_while_mechanisms_keep_owner_semantics() -> None:

    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("inspect", "combat_inspect", "{}")
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    first = context.model_input()[-1]
    assert isinstance(first, ResultMessage)
    assert first.content.updates.generated is not MISSING
    assert tuple(first.content.updates.generated) == ("c1",)
    same_definition_new_source = observation.model_copy(
        update={
            "generated": MappingProxyType(
                {**observation.generated, "c2": observation.generated["c1"]}
            )
        }
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), same_definition_new_source))
    second = context.model_input()[-1]
    assert isinstance(second, ResultMessage)
    assert second.content.updates.generated == {"c2": observation.generated["c1"]}
    cleared = observation.model_copy(update={"generated": MappingProxyType({})})
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), cleared))
    empty = context.model_input()[-1]
    assert isinstance(empty, ResultMessage)
    assert empty.content.updates.generated == {"c1": (), "c2": ()}
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    returned = context.model_input()[-1]
    assert isinstance(returned, ResultMessage)
    assert returned.content.updates.generated == {"c1": observation.generated["c1"]}
    changed_text = observation.mechanisms[0].model_copy(update={"text": "甲变化"})
    same_owner_same_id = changed_text.model_copy(update={"text": "同 ID 的第二条正文"})
    mechanisms = observation.model_copy(
        update={"mechanisms": (changed_text, same_owner_same_id, observation.mechanisms[1])}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), mechanisms))
    current = context.model_input()[-1]
    assert isinstance(current, ResultMessage)
    assert current.content.updates.mechanisms == (changed_text, same_owner_same_id)
    next_node = observation.model_copy(
        update={"location": observation.location.model_copy(update={"node_id": "next"})}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    reset = context.model_input()[-1]
    assert isinstance(reset, ResultMessage)
    assert reset.content.updates.objects == observation.objects
    assert reset.content.updates.generated == {"c1": observation.generated["c1"]}
    assert reset.content.updates.mechanisms == observation.mechanisms
    empty_mechanisms = next_node.model_copy(update={"mechanisms": ()})
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), empty_mechanisms))
    cleared_mechanisms = context.model_input()[-1]
    assert isinstance(cleared_mechanisms, ResultMessage)
    assert cleared_mechanisms.content.updates.to_json() == "{}"
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    restored_mechanisms = context.model_input()[-1]
    assert isinstance(restored_mechanisms, ResultMessage)
    assert restored_mechanisms.content.updates.mechanisms == observation.mechanisms


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
    assert isinstance(final, ResultMessage)
    assert final.content.state is not None
    assert final.content.state.phase.value == "run"
    assert '"actions"' not in final.text
    assert '"selection"' not in final.text
    assert '"offers"' not in final.text
    assert "奖励正文" not in final.text
    assert "奖励选项" not in final.text
    assert final.content.updates.objects is not MISSING
    assert set(final.content.updates.objects) == {"d1"}
    assert final.content.updates.mechanisms == (local_keyword,)
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
    assert isinstance(final, ResultMessage)
    assert final.content.updates.mechanisms == terminal_mechanisms
    assert "玩家状态机制" in final.text
    assert "敌人状态机制" in final.text
    assert "奖励专属机制" not in final.text


def test_task_omission_uses_only_the_actual_tail_and_preserves_native_assistant() -> None:

    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    call = ToolCall("act", "combat_act", "{}")
    context.append_tool_result(
        call, ActionToolResult(ActionCompleted(completed_steps=1), observation)
    )
    context.append_task(ActTask(), observation)
    assert len(context.model_input()) == 1
    context.append_task(ActTask(opening_notes="固定开场笔记"), observation)
    assert len(context.model_input()) == 2
    context.append_task(ActTask(opening_notes="固定开场笔记"), observation)
    assert len(context.model_input()) == 2
    assistant = AssistantMessage(
        ResponsesReply.from_json(
            '[{"type":"reasoning","id":"reasoning","summary":[],"encrypted_content":"opaque"},'
            '{"type":"function_call","call_id":"inspect","name":"combat_inspect","arguments":"{}"}]'
        )
    )
    context.append_reply(assistant)
    context.append_task(ActTask(opening_notes="固定开场笔记"), observation)
    assert context.model_input()[-2] is assistant
    assert len(context.model_input()) == 4
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    assert isinstance(context.model_input()[-1], ResultMessage)
    context.append_task(ActTask(), observation)
    assert len(context.model_input()) == 6
    # Technical acquisition identity does not create a different gameplay observation.
    refreshed = observation.model_copy(
        update={"stamp": observation.stamp.model_copy(update={"observation_sequence": 99})}
    )
    context.append_task(ActTask(), refreshed)
    assert len(context.model_input()) == 6


def test_review_appends_its_actual_report_without_unrelated_live_state() -> None:

    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.append_task(ActTask(opening_notes="固定开场笔记"), observation)
    report = BattleReport(
        location=BattleLocation(act=1, floor=1, room_type="monster"),
        result=TerminalResult.VICTORY,
        last_observed_turn=3,
        before=BoundaryValues(hp=10, max_hp=10, gold=0),
        after=BoundaryValues(hp=8, max_hp=10, gold=0),
    )
    context.append_task(ReviewTask(report))
    review = context.model_input()[-1]
    assert isinstance(review, TaskMessage)
    assert isinstance(review.content, ReviewContent)
    assert review.content.battle_report is report
    assert review.observation is None
    assert '"state"' not in review.text
    assert '"battle_report"' in review.text


def test_partial_and_failed_reads_preserve_delivery_and_run_map_lasts_for_context() -> None:

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
    assert isinstance(failed, ResultMessage)
    assert failed.content.state is None
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), observation))
    same = context.model_input()[-1]
    assert isinstance(same, ResultMessage)
    assert same.content.updates.to_json() == "{}"
    next_node = observation.model_copy(
        update={"location": observation.location.model_copy(update={"node_id": "next"})}
    )
    context.append_tool_result(call, QueryToolResult(ReadCompleted(), next_node))
    reset = context.model_input()[-1]
    assert isinstance(reset, ResultMessage)
    assert reset.content.updates.mechanisms == observation.mechanisms[:-1]

    run = Observation.model_validate_json(RUN).model_copy(update={"map": observation.map})
    run_context = Context(DecisionOwner.RUN)
    run_context.append_tool_result(call, QueryToolResult(ReadCompleted(), run))
    first = run_context.model_input()[-1]
    assert isinstance(first, ResultMessage)
    assert first.content.updates.map is observation.map
    assert observation.map is not MISSING
    next_run_node = run.model_copy(
        update={"location": run.location.model_copy(update={"node_id": "next"})}
    )
    run_context.append_tool_result(
        call, QueryToolResult(ReadCompleted(), next_run_node, MapData(map=observation.map))
    )
    explicit = run_context.model_input()[-1]
    assert isinstance(explicit, ResultMessage)
    assert explicit.content.updates.map is MISSING
    assert isinstance(explicit.content.data, MapData)
    assert explicit.content.data.map is observation.map
    fresh = Context(DecisionOwner.RUN)
    fresh.append_tool_result(call, QueryToolResult(ReadCompleted(), run))
    fresh_message = fresh.model_input()[-1]
    assert isinstance(fresh_message, ResultMessage)
    assert fresh_message.content.updates.map is observation.map
