import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue
from test_combat import reply
from test_mod_client import RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import (
    ActTask,
    BattleLocation,
    BattleReport,
    BoundaryValues,
    CombatHandoff,
    HandoffTask,
    TaskComplete,
)
from sts2_harness.execution import Executed, Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.journal import SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.notes import Notes
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


def handoff(review: str | None = None) -> CombatHandoff:
    return CombatHandoff(
        BattleReport(
            location=BattleLocation(act=1, floor=1, room_type="monster"),
            result=native.TerminalResult.UNKNOWN,
            last_observed_turn=2,
            before=BoundaryValues(hp=10, max_hp=10, gold=0),
            after=BoundaryValues(hp=8, max_hp=10, gold=5),
        ),
        review,
    )


class RunHTTP:
    def __init__(self, responses: list[bytes]) -> None:
        self.observation = native.Observation.model_validate_json(RUN)
        self.responses = iter(responses)
        self.actions: list[native.StepRequest] = []
        self.inputs: list[dict[str, JsonValue]] = []
        self.fail_second = False

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": self.observation.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=self.observation
                ).to_json(),
            )
        assert request.url.path == "/action"
        submitted = native.StepRequest.model_validate_json(request.content)
        self.actions.append(submitted)
        if self.fail_second and len(self.actions) == 2:
            return httpx.Response(
                200,
                json={
                    "operation_id": submitted.operation_id,
                    "status": "failed",
                    "error": {
                        "code": "dispatch_failed",
                        "message": "The anticipated choice could not be dispatched.",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "operation_id": submitted.operation_id,
                "status": "completed",
                "boundary": "ready",
            },
        )

    def model(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.content))
        return httpx.Response(
            200, content=next(self.responses), headers={"Content-Type": "text/event-stream"}
        )


def test_run_queries_notes_executes_batch_and_receives_result_in_same_context(
    tmp_path: Path,
) -> None:
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics

    console = StringIO()
    diagnostics = Diagnostics("run", console=console)
    http = RunHTTP(
        [
            reply(("read", "run_notes", '{"mode":"read"}')),
            reply(("edit", "run_notes", '{"mode":"edit","old_text":"potion","new_text":"Boss"}')),
            reply(
                (
                    "act",
                    "run_act",
                    '{"steps":[{"kind":"interact","action":"travel"},{"kind":"select","refs":[]},'
                    '{"kind":"interact","action":"unexecuted-tail"}]}',
                )
            ),
            reply(("next", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            reply(text="Report assessed."),
        ]
    )
    http.fail_second = True
    notes = Notes("Reserve potion.")

    async def scenario() -> None:
        from sts2_harness.context.models import HandoffContent, RunActContent, RunActHandoffContent
        from sts2_harness.journal import Journal, Manifest, RecordedGame
        from sts2_harness.journal_models import (
            GuidanceText,
            NotesData,
            ResultSnapshot,
            RunTaskSnapshot,
            TextSnapshot,
        )
        from sts2_harness.knowledge import DocumentView, SkillRead
        from sts2_harness.materials import DocumentSource, FileDocuments
        from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
        from sts2_harness.prompts import PackagedPrompts
        from sts2_harness.results import ActionStopped

        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="run",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=notes.read(),
            ),
        )
        guide = tmp_path / "guide.txt"
        guide.write_text("Recorded Run guidance.")
        documents = RecordedDocuments(
            FileDocuments((DocumentSource(id="run", name="Run", description="Act", path=guide),)),
            journal,
        )

        async def materials(_: native.Observation) -> tuple[DocumentView, ...]:
            return (await documents.read(SkillRead("run")),)

        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                RecordedGame(
                    LiveGame(
                        client,
                        namespace="run-test",
                        game_instance_id="game",
                        run_id="run",
                        audit=journal,
                    ),
                    journal,
                ),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                notes=notes,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(
                    lambda: RecordedLlm(
                        llm, journal, role=native.DecisionOwner.RUN, stream="run:1"
                    ),
                    tools,
                    budget,
                    diagnostics=diagnostics,
                    materials=materials,
                    prompts=RecordedPrompts(PackagedPrompts(), journal),
                )
                route = native.RouteKey.model_validate(http.observation.location.model_dump())
                result = await execution.run_unit(agent, ActTask(route))
                assert isinstance(result, Executed)
                assert isinstance(result.outcome.outcome, ActionStopped)
                assert result.outcome.outcome.completed_steps == 1
                assert result.outcome.outcome.next_step == 2
                assert not result.handoff_processed
                assert notes.read() == "Reserve Boss."
                combined = await execution.run_unit(
                    agent, ActTask(route, handoff("Prepare for the next Boss."))
                )
                assert isinstance(combined, Executed) and combined.handoff_processed
                completed = await execution.run_unit(agent, HandoffTask(handoff()))
                assert isinstance(completed, TaskComplete) and completed.text == "Report assessed."
        assert [step.step.kind for step in http.actions] == [
            native.StepKind.INTERACT,
            native.StepKind.SELECT,
            native.StepKind.INTERACT,
        ]
        assert budget.failures == 0
        tools_wire = http.inputs[0]["tools"]
        assert isinstance(tools_wire, list)
        assert {
            t["name"] for t in tools_wire if isinstance(t, dict) and isinstance(t["name"], str)
        } == {
            "run_act",
            "run_inspect",
            "run_knowledge",
            "run_skill",
            "run_notes",
            "run_plan",
            "run_next",
        }
        wire = http.inputs[-1]["input"]
        assert isinstance(wire, list)
        outputs = [
            item
            for item in wire
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        assert [item["call_id"] for item in outputs] == ["read", "edit", "act", "next"]
        expected = [{"text": "Reserve potion."}, {"updated": True}]
        for output, data in zip(outputs[:2], expected, strict=True):
            assert isinstance(output["output"], str)
            assert json.loads(output["output"])["data"] == data
        assert isinstance(outputs[2]["output"], str)
        stopped = json.loads(outputs[2]["output"])["outcome"]
        assert stopped["status"] == "stopped" and stopped["completed_steps"] == 1
        assert stopped["next_step"] == 2
        assert isinstance(outputs[-1]["output"], str)
        assert json.loads(outputs[-1]["output"])["outcome"] == {
            "status": "completed",
            "completed_steps": 1,
        }
        journal.close()
        guide.write_text("Future source, not historical guidance.")
        notes.replace("Future notes.")
        archive = Journal.open(journal.path)
        requests = archive.requests()
        assert len(requests) == 5
        assert archive.manifest.initial_notes == "Reserve potion."
        initial = requests[0].input[-1]
        assert isinstance(initial, RunTaskSnapshot) and isinstance(initial.content, RunActContent)
        assert initial.content.strategy_notes == "Reserve potion."
        read = requests[1].input[-2]
        assert isinstance(read, ResultSnapshot) and isinstance(read.data, NotesData)
        assert read.data.value.model_dump() == {"text": "Reserve potion."}
        edit = requests[2].input[-2]
        assert isinstance(edit, ResultSnapshot) and isinstance(edit.data, NotesData)
        assert edit.data.value.model_dump() == {"updated": True}
        combined_task = requests[3].input[-1]
        assert isinstance(combined_task, RunTaskSnapshot)
        assert isinstance(combined_task.content, RunActHandoffContent)
        assert combined_task.content.combat_review == "Prepare for the next Boss."
        handoff_task = requests[4].input[-1]
        assert isinstance(handoff_task, RunTaskSnapshot) and isinstance(
            handoff_task.content, HandoffContent
        )
        assert handoff_task.content.combat_review is None
        guidance = requests[0].input[1]
        assert isinstance(guidance, TextSnapshot) and isinstance(guidance.value, GuidanceText)
        assert guidance.value.documents[0].text.body == "Recorded Run guidance."
        assert archive.gaps == ()
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        tools = [event for event in events if event["event"] == "tool_finished"]
        assert [(event["tool"], event["outcome"]) for event in tools] == [
            ("run_notes", "completed"),
            ("run_notes", "completed"),
            ("run_act", "stopped"),
            ("run_act", "completed"),
        ]
        assert [event["level"] for event in tools] == ["INFO", "INFO", "ERROR", "INFO"]
        diagnostics.close()

    asyncio.run(scenario())


def test_notes_edits_reject_missing_ambiguous_and_overlapping_fragments(tmp_path: Path) -> None:
    from sts2_harness.llm.messages import ToolCall
    from sts2_harness.results import NotesText, NotesUpdated, ReadCompleted, ReasonCode, Rejected

    http = RunHTTP([])
    notes = Notes("aaa / alpha / alpha")

    async def scenario() -> None:
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="notes", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=notes,
                observe=execution.observe,
                inspect=client.inspect,
                catalog=client.catalog,
            )
            for old in ("aa", "absent", "alpha"):
                call = ToolCall(
                    "conflict",
                    "run_notes",
                    json.dumps({"mode": "edit", "old_text": old, "new_text": "changed"}),
                )
                result = await tools.dispatch(call)
                assert isinstance(result.outcome, Rejected)
                assert result.outcome.reason.code is ReasonCode.NOTES_CONFLICT
                assert notes.read() == "aaa / alpha / alpha"
            for arguments, expected in (
                ('{"mode":"replace","text":"first second"}', "first second"),
                ('{"mode":"edit","old_text":" second","new_text":""}', "first"),
                ('{"mode":"replace","text":""}', ""),
            ):
                result = await tools.dispatch(ToolCall("write", "run_notes", arguments))
                assert isinstance(result.outcome, ReadCompleted)
                assert isinstance(result.data, NotesUpdated)
                assert result.data.to_json() == '{"updated":true}'
                assert notes.read() == expected
            result = await tools.dispatch(ToolCall("read", "run_notes", '{"mode":"read"}'))
            assert isinstance(result.data, NotesText) and result.data.text == ""
            assert http.actions == []

    asyncio.run(scenario())


def test_handoff_closes_bad_calls_without_side_effects_and_can_update_notes(tmp_path: Path) -> None:
    http = RunHTTP(
        [
            reply(
                ("game", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}'),
                ("write", "run_notes", '{"mode":"replace","text":"must not write"}'),
            ),
            reply(
                ("invalid", "run_notes", '{"mode":"replace","text":"must not write","extra":true}')
            ),
            reply(("read", "run_notes", '{"mode":"read"}')),
            reply(("forbidden", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            reply(
                (
                    "update",
                    "run_notes",
                    '{"mode":"replace","text":"Use the next shop deliberately."}',
                )
            ),
            reply(text="Report assessed without acting."),
        ]
    )
    notes = Notes("")

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="handoff", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=notes,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                result = await execution.run_unit(
                    RunAgent(lambda: llm, tools, budget), HandoffTask(handoff())
                )
        assert isinstance(result, TaskComplete)
        assert notes.read() == "Use the next shop deliberately."
        assert http.actions == [] and budget.failures == 0
        wire = http.inputs[-1]["input"]
        assert isinstance(wire, list)
        outputs = [
            item
            for item in wire
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        assert [item["call_id"] for item in outputs] == [
            "game",
            "write",
            "invalid",
            "read",
            "forbidden",
            "update",
        ]
        for output in outputs[:3] + outputs[4:5]:
            assert isinstance(output["output"], str)
            assert json.loads(output["output"])["outcome"]["status"] == "rejected"
        assert isinstance(outputs[3]["output"], str)
        assert json.loads(outputs[3]["output"])["data"] == {"text": ""}
        first = http.inputs[0]["input"]
        assert isinstance(first, list) and isinstance(first[-1], dict)
        assert isinstance(first[-1]["content"], str)
        task = json.loads(first[-1]["content"])
        assert task == {
            "task": "handoff",
            "instruction": (
                "Assess the battle report and any Combat review using available evidence. "
                "Update strategy notes if useful, then finish with a concise response. "
                "Do not perform game actions."
            ),
            "battle_report": handoff().report.model_dump(mode="json"),
            "combat_review": None,
            "strategy_notes": "",
        }

    asyncio.run(scenario())


def test_run_tasks_keep_new_handoffs_and_notes_without_consuming_observation_delivery() -> None:
    from pydantic import MISSING

    from sts2_harness.context import Context
    from sts2_harness.context.models import (
        HandoffContent,
        RunActContent,
        RunActHandoffContent,
        RunActInput,
        RunHandoffInput,
        TaskMessage,
    )
    from sts2_harness.llm.messages import ToolCall
    from sts2_harness.results import ActionCompleted, ActionToolResult

    observation = native.Observation.model_validate_json(RUN)
    context = Context(native.DecisionOwner.RUN)
    context.append_task(RunHandoffInput(handoff(), ""))
    first = context.model_input()[-1]
    assert isinstance(first, TaskMessage) and isinstance(first.content, HandoffContent)
    assert first.observation is None
    context.append_task(RunActInput(), observation)
    task = context.model_input()[-1]
    assert isinstance(task, TaskMessage) and isinstance(task.content, RunActContent)
    assert task.content.observation.decision.phase == "run"
    context.append_tool_result(
        ToolCall("act", "run_act", "{}"),
        ActionToolResult(ActionCompleted(completed_steps=1), observation),
        defer_observation=True,
    )
    size = len(context.model_input())
    context.append_task(RunActInput(), observation)
    assert len(context.model_input()) == size + 1
    task_input = RunActInput(handoff=handoff("A new lesson."))
    context.append_task(task_input, observation)
    current = context.model_input()[-1]
    assert isinstance(current, TaskMessage) and isinstance(current.content, RunActHandoffContent)
    assert current.content.battle_report == handoff().report
    assert current.content.combat_review == "A new lesson."
    assert current.content.strategy_notes is MISSING
    assert len(context.model_input()) == size + 2
    context.append_task(task_input, observation)
    assert len(context.model_input()) == size + 2
    context.append_task(RunActInput("Changed notes", handoff("A new lesson.")), observation)
    assert len(context.model_input()) == size + 3
    assert first.content.strategy_notes == ""


def test_enter_act_retries_initialization_and_delivers_current_notes_in_fresh_context(
    tmp_path: Path,
) -> None:
    import pytest

    from sts2_harness.execution.recovery import DecisionInterrupted
    from sts2_harness.knowledge import DocumentView, SkillRead
    from sts2_harness.materials import DocumentSource, FileDocuments
    from sts2_harness.results import Reason, ReasonCode
    from sts2_harness.run import ActStart

    http = RunHTTP([reply(text="Old act handoff."), reply(text="New act handoff.")])
    notes = Notes("Initial notes.")
    path = tmp_path / "guide.txt"
    path.write_text("Old act guidance.")
    documents = FileDocuments(
        (DocumentSource(id="act", name="Act", description="Guidance", path=path),)
    )

    async def materials(_: native.Observation) -> tuple[DocumentView, ...]:
        return (await documents.read(SkillRead("act")),)

    async def scenario() -> None:
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="act", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=notes,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(http.model)
            ) as llm:
                agent = RunAgent(lambda: llm, tools, budget, materials=materials)
                first = await execution.run_unit(agent, HandoffTask(handoff()))
                assert isinstance(first, TaskComplete)
                notes.replace("Notes persist across acts.")
                agent = RunAgent(lambda: llm, tools, budget, materials=materials)
                path.unlink()
                next_act = ActStart(http.observation)
                with pytest.raises(DecisionInterrupted) as caught:
                    await agent.enter_act(next_act)
                assert caught.value.pause_required
                assert isinstance(caught.value.reason, Reason)
                assert caught.value.reason.code is ReasonCode.CONTENT_UNAVAILABLE
                assert len(http.inputs) == 1 and budget.failures == 0
                assert notes.read() == "Notes persist across acts."
                path.write_text("New act guidance.")
                await agent.enter_act(next_act)
                assert len(http.inputs) == 1
                notes.replace("Updated after initialization.")
                second = await execution.run_unit(agent, HandoffTask(handoff()))
                assert isinstance(second, TaskComplete)
        latest = http.inputs[-1]["input"]
        assert isinstance(latest, list) and len(latest) == 3
        task = latest[-1]
        assert isinstance(task, dict) and isinstance(task["content"], str)
        assert json.loads(task["content"])["strategy_notes"] == "Updated after initialization."
        guide = latest[1]
        assert isinstance(guide, dict) and isinstance(guide["content"], str)
        assert json.loads(guide["content"])["guidance"][0]["text"] == "New act guidance."
        assert http.actions == []

    asyncio.run(scenario())


@pytest.mark.parametrize("protocol", ("responses", "chat-completions", "messages"))
def test_run_compaction_sends_retained_group_and_records_zero_attempt_when_too_large(
    tmp_path: Path,
    protocol: str,
) -> None:
    from dataclasses import replace

    import pytest

    from sts2_harness.capacity import CapacityPolicy
    from sts2_harness.context import Context
    from sts2_harness.context.models import RunActInput
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.llm.messages import FailureKind, ModelUnavailable, TextMessage, TextRole
    from sts2_harness.model_journal import RecordedLlm

    async def scenario(window: int) -> None:
        from test_chat_completions import chat_reply
        from test_messages import events, sse

        response = (
            reply(text="Considered.")
            if protocol == "responses"
            else chat_reply(text="Considered.")
            if protocol == "chat-completions"
            else sse(events())
        )
        http = RunHTTP([response])
        config = replace(configuration(tmp_path, LLM_PROTOCOL=protocol), context_window=window)
        journal = Journal.create(
            tmp_path / f"compaction-{window}.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="compaction",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="notes",
                capacity_policy=CapacityPolicy(context_window=window),
            ),
        )
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:

            async def observe() -> native.Observation:
                return http.observation

            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=Notes("notes"),
                observe=observe,
                inspect=client.inspect,
                catalog=client.catalog,
            )
            async with Llm(config, transport=httpx.MockTransport(http.model)) as llm:
                recorded = RecordedLlm(llm, journal, role=native.DecisionOwner.RUN, stream="run:1")

                class TestRunAgent(RunAgent):
                    async def request(self, context: Context) -> None:
                        await self._request(context)

                agent = TestRunAgent(lambda: recorded, tools, ToolRecoveryBudget())
                from sts2_harness.run import ActStart

                await agent.enter_act(ActStart(http.observation))

                def make_context() -> Context:
                    context = Context(
                        native.DecisionOwner.RUN,
                        system=(TextMessage(TextRole.SYSTEM, "Make a decision."),),
                    )
                    obs = http.observation
                    context.begin_group(obs, "old notes")
                    context.append_task(RunActInput(strategy_notes="old notes"), obs)
                    context.append_correction(" ".join(f"obsolete{i}" for i in range(10000)))
                    context.finish_group()
                    context.begin_group(obs, "new notes", {"reason": "shop", "edges": []})
                    context.append_task(RunActInput(strategy_notes="new notes"), obs)
                    context.finish_group()
                    context.begin_group(obs, "later notes")
                    context.append_task(RunActInput(strategy_notes="later notes"), obs)
                    return context

                context = make_context()
                before = context.model_input()
                replay_context = make_context()
                if window == 2000:
                    with pytest.raises(ModelUnavailable) as error:
                        await agent.request(context)
                    assert error.value.reason is FailureKind.CAPACITY
                    assert context.model_input() == before
                    assert http.inputs == []
                else:
                    await agent.request(context)
                    assert len(http.inputs) == 1
                    payload = json.dumps(http.inputs[0], ensure_ascii=False)
                    assert "obsolete9999" not in payload
                    assert "shop" in payload and "new notes" in payload
                    assert "later notes" in payload
        journal.close()
        archive = Journal.open(journal.path)
        record = archive.requests()[0]
        assert record.preparation is not None
        if window == 2000:
            assert not record.attempts
            assert record.preparation.decision == "minimum_exceeds_window"
            assert record.preparation.input_after is not None
        else:
            assert len(record.attempts) == 1
            assert record.preparation.decision == "compacted"
            from sts2_harness_eval._replay import ReplayModel, Timeline
            from sts2_harness_eval.results import ModelAnchor, Reached

            reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
            replay = ReplayModel(
                Timeline((record,)),
                native.DecisionOwner.RUN,
                "run:1",
                ModelAnchor("compaction", 999),
                reached,
            )
            try:
                replay_agent = TestRunAgent(lambda: replay, tools, ToolRecoveryBudget())
                await replay_agent.enter_act(ActStart(http.observation))
                await replay_agent.request(replay_context)
                assert replay_context.model_input() == context.model_input()
            finally:
                await replay.close()
        archive.close()

    asyncio.run(scenario(10000))
    asyncio.run(scenario(2000))
