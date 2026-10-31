import json
from pathlib import Path
from typing import Never

from test_mod_client import RUN

from sts2_harness.client import models as native
from sts2_harness.context import Context
from sts2_harness.context.models import RunActInput
from sts2_harness.knowledge import DocumentView
from sts2_harness.llm.messages import AssistantMessage, ResponsesReply, ToolCall, ToolResultMessage
from sts2_harness.material_text import MaterialText
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    ActionUnknown,
    QueryToolResult,
    ReadCompleted,
    Reason,
    ReasonCode,
    Suggestion,
)


def call(context: Context, call_id: str, name: str) -> ToolCall:
    value = {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}
    context.append_reply(AssistantMessage(ResponsesReply.from_json(json.dumps([value]))))
    return ToolCall(call_id, name, "{}")


def test_repeated_compaction_keeps_unknown_prefix_and_fresh_full_query() -> None:
    obs = native.Observation.model_validate_json(RUN)
    context = Context(native.DecisionOwner.RUN)
    document = DocumentView(
        id="example",
        name=MaterialText.create("name", "Example"),
        text=MaterialText.create("text", "Complete current knowledge text."),
        resources=(),
    )
    context.begin_group(obs, "old notes")
    context.append_task(RunActInput(strategy_notes="old notes"), obs)
    context.append_tool_result(
        call(context, "q1", "run_skill"), QueryToolResult(ReadCompleted(), obs, document)
    )
    context.append_tool_result(
        call(context, "a1", "run_act"),
        ActionToolResult(
            ActionUnknown(
                confirmed_steps=1,
                uncertain_step=2,
                reason=Reason(code=ReasonCode.EXECUTION_UNCONFIRMED, message="Not confirmed."),
                suggested_actions=(Suggestion(message="Inspect before choosing again."),),
            ),
            None,
        ),
    )
    context.finish_group()
    context.begin_group(obs, "new notes")
    context.append_task(RunActInput(strategy_notes="new notes"), obs)
    first = context.compaction_candidate()
    first_text = "\n".join(message.text for message in first)
    assert '"confirmed_steps":1' in first_text
    assert '"uncertain_step":2' in first_text
    assert '"observation":null' in first_text
    context.install_compaction(first)
    context.append_tool_result(
        call(context, "a2", "run_act"), ActionToolResult(ActionCompleted(completed_steps=1), obs)
    )
    context.finish_group()
    context.begin_group(obs, "final notes")
    context.append_task(RunActInput(strategy_notes="final notes"), obs)
    context.append_tool_result(
        call(context, "q2", "run_skill"), QueryToolResult(ReadCompleted(), obs, document)
    )
    second = context.compaction_candidate()
    second_text = "\n".join(message.text for message in second)
    assert "a1" not in [
        message.call_id for message in second if isinstance(message, ToolResultMessage)
    ]
    assert "Complete current knowledge text." in second_text
    assert "final notes" in second_text
    context.install_compaction(second)
    assert context.model_input() == second


def test_minimum_above_trigger_is_not_reinstalled_until_a_group_changes(tmp_path: Path) -> None:
    import asyncio
    from dataclasses import replace

    import httpx2 as httpx
    from test_combat import reply
    from test_responses import configuration

    from sts2_harness.capacity import CapacityPolicy, CompactionDecision
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.journal import Journal, Manifest, SessionMaterials
    from sts2_harness.llm.client import Llm
    from sts2_harness.model_journal import RecordedLlm
    from sts2_harness.notes import Notes
    from sts2_harness.run import ActStart, RunAgent
    from sts2_harness.tools import ToolCatalog

    async def scenario() -> None:
        obs = native.Observation.model_validate_json(RUN)

        async def observe() -> native.Observation:
            return obs

        async def unused(*args: object) -> Never:
            raise AssertionError("No game IO expected")

        tools = ToolCatalog(
            native.DecisionOwner.RUN,
            notes=Notes("notes"),
            observe=observe,
            inspect=unused,
            catalog=unused,
        )
        context = Context(native.DecisionOwner.RUN)
        context.begin_group(obs, "one")
        context.append_task(RunActInput(strategy_notes="one"), obs)
        context.append_correction(" ".join(f"discarded{i}" for i in range(5000)))
        context.finish_group()
        context.begin_group(obs, "two")
        context.append_task(RunActInput(strategy_notes="two"), obs)
        context.finish_group()
        context.begin_group(obs, "three")
        context.append_task(RunActInput(strategy_notes="three"), obs)
        context.append_correction(" ".join(f"retained{i}" for i in range(4000)))
        async with Llm(configuration(tmp_path)) as probe:
            minimal = probe.prepare(context.compaction_candidate(), tools.definitions)
        window = int(minimal.measurement.estimated_input_tokens * 1.05) + 1
        journal = Journal.create(
            tmp_path / "repeat.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="repeat",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                capacity_policy=CapacityPolicy(context_window=window),
            ),
        )
        requests = 0

        def provider(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(
                200, content=reply(text="ok"), headers={"Content-Type": "text/event-stream"}
            )

        class TestAgent(RunAgent):
            async def request(self, value: Context) -> None:
                await self._request(value)

        async with Llm(
            replace(configuration(tmp_path), context_window=window),
            transport=httpx.MockTransport(provider),
        ) as llm:
            model = RecordedLlm(llm, journal, role=native.DecisionOwner.RUN, stream="run:1")
            agent = TestAgent(lambda: model, tools, ToolRecoveryBudget())
            await agent.enter_act(ActStart(obs))
            await agent.request(context)
            await agent.request(context)
            context.finish_group()
            context.begin_group(obs, "four")
            context.append_task(RunActInput(strategy_notes="four"), obs)
            await agent.request(context)
        assert requests == 3
        decisions = [
            record.preparation.decision for record in journal.requests() if record.preparation
        ]
        assert decisions == [
            CompactionDecision.COMPACTED,
            CompactionDecision.UNCHANGED,
            CompactionDecision.COMPACTED,
        ]
        journal.close()

    asyncio.run(scenario())


def test_removed_query_material_can_be_delivered_as_guidance_again() -> None:
    from sts2_harness.knowledge import GuidanceContent
    from sts2_harness.llm.messages import TextMessage

    obs = native.Observation.model_validate_json(RUN)
    context = Context(native.DecisionOwner.RUN)
    document = DocumentView(
        id="example",
        name=MaterialText.create("name", "Example"),
        text=MaterialText.create("text", "Complete guidance."),
        resources=(),
    )
    context.begin_group(obs, "notes")
    context.append_task(RunActInput(), obs)
    context.append_tool_result(
        call(context, "old-query", "run_skill"), QueryToolResult(ReadCompleted(), obs, document)
    )
    context.finish_group()
    context.begin_group(obs, "notes")
    context.append_task(RunActInput(strategy_notes="next"), obs)
    context.finish_group()
    context.begin_group(obs, "notes")
    context.append_task(RunActInput(strategy_notes="current"), obs)
    context.install_compaction(context.compaction_candidate())
    assert all("Complete guidance." not in message.text for message in context.model_input())
    context.append_materials((document,))
    message = context.model_input()[-1]
    assert isinstance(message, TextMessage)
    assert isinstance(message.value, GuidanceContent)
    assert message.value.guidance == (document,)


def test_context_returns_baseline_as_part_of_the_candidate() -> None:
    from sts2_harness.llm.messages import TextMessage

    obs = native.Observation.model_validate_json(RUN)
    context = Context(native.DecisionOwner.RUN)
    context.begin_group(obs, "notes")
    context.append_task(RunActInput(), obs)
    candidate, baseline = context.compaction_input()
    assert isinstance(baseline, TextMessage)
    assert baseline in candidate
    assert json.loads(baseline.text)["context_baseline"]["strategy_notes"] == "notes"
