import json

from test_mod_client import COMBAT, RUN

from sts2_harness.client.models import (
    CardEntry,
    CombatState,
    DecisionOwner,
    MechanismKind,
    MechanismView,
    Observation,
    ResourceCosts,
    RunState,
)
from sts2_harness.context import CombatActInput, Context, QueryToolResult, ReadCompleted
from sts2_harness.context.models import RunActInput
from sts2_harness.llm.messages import AssistantMessage, ResponsesReply, ToolCall


def test_changed_hand_is_complete_and_original_history_stays_unchanged() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    original = context.model_input()[0].text
    initial = json.loads(original)["observation"]
    assert [row["ref"] for row in initial["hand"]] == ["c2", "c1"]
    assert initial["enemies"][0]["intents"][1]["hits"] == 2
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    changed = observation.model_copy(
        update={
            "objects": {
                **observation.objects,
                "c1": card.model_copy(update={"cost": ResourceCosts(energy=0)}),
            }
        }
    )
    context.append_tool_result(
        ToolCall("inspect", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), changed)
    )
    update = json.loads(context.model_input()[-1].text)["observation"]
    assert [row["ref"] for row in update["hand"]] == ["c2", "c1"]
    assert update["hand"][1]["cost"]["energy"] == 0
    assert "defs" not in update
    assert "enemies" not in update
    assert context.model_input()[0].text == original


def test_pile_membership_aggregates_without_order_or_instance_refs() -> None:
    observation = Observation.model_validate_json(COMBAT)
    state = observation.state
    assert isinstance(state, CombatState)
    observation = observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "piles": state.piles.model_copy(
                        update={
                            "hand": (),
                            "draw": ("c1", "c2"),
                        }
                    )
                }
            ),
            "descriptions": {"c1": "造成3点伤害。", "c2": "造成3点伤害。"},
            "modifier_descriptions": {},
        }
    )
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    state = json.loads(context.model_input()[0].text)["observation"]
    assert len(state["draw"]) == 1
    assert state["draw"][0]["id"] == "CARD.ONE"
    assert state["draw"][0]["n"] == 2
    assert "ref" not in state["draw"][0]


def test_compaction_restores_defs_sections_and_opening_notes() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.COMBAT)
    context.begin_group(observation, "ENGINE test")
    context.append_task(CombatActInput("ENGINE test"), observation)
    context.finish_group()
    candidate = context.compaction_candidate()
    context.validate_compaction(candidate)
    context.install_compaction(candidate)
    context.begin_group(observation, "ENGINE test")
    context.append_reply(
        AssistantMessage(
            ResponsesReply.from_json(
                '[{"type":"function_call","id":"fc","call_id":"read",'
                '"name":"combat_inspect","arguments":"{}"}]'
            )
        )
    )
    context.append_tool_result(
        ToolCall("read", "combat_inspect", "{}"), QueryToolResult(ReadCompleted(), observation)
    )
    context.finish_group()
    context.install_compaction(context.compaction_candidate())
    baseline = json.loads(context.model_input()[0].text)["context_baseline"]
    assert baseline["observation"]["baseline"] is True
    assert baseline["observation"]["defs"][0]["id"] == "CARD.ONE"
    assert baseline["opening_notes"] == "ENGINE test"
    assert baseline["observation"]["hand"][0]["ref"] == "c2"


def test_role_boundary_hides_combat_menu_and_aggregates_native_deck_text() -> None:
    observation = Observation.model_validate_json(COMBAT)
    context = Context(DecisionOwner.RUN)
    context.append_task(RunActInput(), observation)
    state = json.loads(context.model_input()[0].text)["observation"]
    assert "hand" not in state
    assert "actions" not in state["choice"]
    assert state["deck"][0]["id"] == "CARD.ONE"
    assert "text" not in state["deck"][0]
    assert not any(row["id"].startswith("CARD.") for row in state.get("defs", []))
    assert "ref" not in state["deck"][0]
    context.append_task(RunActInput(), Observation.model_validate_json(RUN))
    update = json.loads(context.model_input()[-1].text)["observation"]
    assert update["deck"] == []
    assert update["choice"]["actions"][0]["ref"] == "travel"


def test_automatic_upgrade_previews_remain_beside_candidates_and_update() -> None:
    observation = Observation.model_validate_json(COMBAT)
    combat_state = observation.state
    assert isinstance(combat_state, CombatState)
    run = Observation.model_validate_json(RUN)
    assert isinstance(run.state, RunState)
    for role, state, task in (
        (DecisionOwner.COMBAT, combat_state, CombatActInput()),
        (
            DecisionOwner.RUN,
            run.state.model_copy(update={"selection": combat_state.selection}),
            RunActInput(),
        ),
    ):
        current = observation.model_copy(
            update={
                "state": state,
                "location": observation.location.model_copy(update={"owner": role}),
            }
        )
        context = Context(role)
        context.append_task(task, current)
        initial = json.loads(context.model_input()[-1].text)["observation"]
        candidates = (
            initial["hand"] if role is DecisionOwner.COMBAT
            else initial["choice"]["selection"]["candidates"]
        )
        assert candidates[1]["upgrade"] == {
            "status": "available", "text": "造成3点伤害。"
        }
        assert candidates[0]["upgrade"] == {"status": "not_upgradable"}

        changed = current.model_copy(update={"upgrades": {}})
        context.append_task(task, changed)
        update = json.loads(context.model_input()[-1].text)["observation"]
        updated = (
            update["hand"] if role is DecisionOwner.COMBAT
            else update["choice"]["selection"]["candidates"]
        )
        assert all("upgrade" not in row for row in updated)


def test_history_use_names_its_base_definition_scope_across_upgrade_rows() -> None:
    observation = Observation.model_validate_json(COMBAT)
    original = observation.objects["c1"]
    assert isinstance(original, CardEntry)
    state = observation.state
    assert isinstance(state, CombatState)
    observation = observation.model_copy(
        update={
            "state": state.model_copy(update={"deck": ("c1", "c2")}),
            "objects": {
                **observation.objects,
                "c2": original.model_copy(update={"upgrades": 1}),
            },
        }
    )
    context = Context(DecisionOwner.RUN)
    context.set_deck_stats({"observed_plays": 3}, {"CARD.ONE": 3})
    context.append_task(RunActInput(), observation)
    deck = json.loads(context.model_input()[-1].text)["observation"]["deck"]
    assert {row["id"] for row in deck} == {"CARD.ONE", "CARD.ONE+1"}
    assert all(
        row["use"] == {"scope": "base_definition", "definition_id": "CARD.ONE", "observed_plays": 3}
        for row in deck
    )


def test_mechanisms_are_once_delivered_without_owners_and_restore_after_compaction() -> None:
    observation = Observation.model_validate_json(COMBAT)
    mechanism = MechanismView(
        kind=MechanismKind.POWER,
        definition_id="POWER.ONE",
        name="Power",
        owner="c1",
        text="Native rule",
    )
    observation = observation.model_copy(update={"mechanisms": (
        mechanism, mechanism.model_copy(update={"owner": "c2"}),
    )})
    context = Context(DecisionOwner.COMBAT)
    context.append_task(CombatActInput(), observation)
    first = json.loads(context.model_input()[-1].text)["observation"]
    assert first["mechanisms"] == [{
        "kind": "power", "definition_id": "POWER.ONE", "name": "Power", "text": "Native rule",
    }]
    assert "deck" not in first
    context.begin_group(observation, "")
    state = observation.state
    assert isinstance(state, CombatState)
    changed = observation.model_copy(update={
        "state": state.model_copy(update={"turn": 2}),
        "mechanisms": (mechanism.model_copy(update={"owner": "c2"}),),
    })
    context.append_task(CombatActInput(), changed)
    assert "mechanisms" not in json.loads(context.model_input()[-1].text)["observation"]
    context.finish_group()
    context.install_compaction(context.compaction_candidate())
    baseline = json.loads(context.model_input()[0].text)["context_baseline"]["observation"]
    assert baseline["mechanisms"] == first["mechanisms"]
    changed = changed.model_copy(update={"mechanisms": (
        mechanism.model_copy(update={"text": "Changed native rule"}),
    )})
    context.append_task(CombatActInput(), changed)
    update = json.loads(context.model_input()[-1].text)["observation"]
    assert update["mechanisms"][0]["text"] == "Changed native rule"


def test_current_unknown_values_override_known_card_text_and_cost() -> None:
    observation = Observation.model_validate_json(COMBAT)
    card = observation.objects["c1"]
    assert isinstance(card, CardEntry)
    context = Context(DecisionOwner.COMBAT)
    known = observation.model_copy(update={
        "descriptions": {"c1": "Known current effect", "c2": "Known current effect"}
    })
    context.append_task(CombatActInput(), known)
    unknown = known.model_copy(update={
        "objects": {
            **known.objects, "c1": card.model_copy(update={"cost": ResourceCosts(energy=None)})
        },
        "descriptions": {"c1": None},
    })
    context.append_task(CombatActInput(), unknown)
    row = json.loads(context.model_input()[-1].text)["observation"]["hand"][1]
    assert row["text"] is None
    assert row["cost"]["energy"] is None
