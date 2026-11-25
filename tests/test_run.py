import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue
from test_combat import reply
from test_mod_client import RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import (
    ActTask,
    BattleLocation,
    BattleReport,
    BattleResult,
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
from sts2_harness.note_values import NotesState, NotesView
from sts2_harness.notes import Notes
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


def handoff(review: str | None = None) -> CombatHandoff:
    return CombatHandoff(
        BattleReport(
            location=BattleLocation(act=1, floor=1, room_type="monster"),
            result=BattleResult.UNKNOWN,
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
            reply(("edit", "run_notes", '{"mode":"write","other":"Reserve Boss."}')),
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
    notes = Notes(NotesState(other="Reserve potion."))

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
                initial_notes=NotesState(other=notes.read().other),
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
                assert notes.read().other == "Reserve Boss."
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
        expected = [
            {"notes": NotesView(other="Reserve potion.").model_dump(mode="json")},
            {"updated": True},
        ]
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
        notes.write(NotesState(other="Future notes."), {"other"})
        archive = Journal.open(journal.path)
        requests = archive.requests()
        assert len(requests) == 5
        assert archive.manifest.initial_notes == NotesState(other="Reserve potion.")
        initial = requests[0].input[-1]
        assert isinstance(initial, RunTaskSnapshot) and isinstance(initial.content, RunActContent)
        assert isinstance(initial.content.observation.notes, NotesView)
        assert initial.content.observation.notes.other == "Reserve potion."
        read = requests[1].input[-2]
        assert isinstance(read, ResultSnapshot) and isinstance(read.data, NotesData)
        assert read.data.value.model_dump() == {
            "notes": NotesView(other="Reserve potion.").model_dump()
        }
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
        rules = requests[0].input[1]
        assert isinstance(rules, TextSnapshot)
        from sts2_harness.journal_models import LiteralText

        assert isinstance(rules.value, LiteralText)
        assert json.loads(rules.value.body)["run_rules"] == http.observation.run_rules.model_dump(
            mode="json"
        )
        guidance = requests[0].input[2]
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


def test_notes_replace_plan_and_preserve_other_fields(tmp_path: Path) -> None:
    from test_mod_client import CARD

    from sts2_harness.llm.messages import ToolCall
    from sts2_harness.results import NotesText, NotesUpdated, ReadCompleted

    http = RunHTTP([])
    assert isinstance(http.observation.state, native.RunState)
    card = native.CardEntry.model_validate_json(CARD)
    http.observation = http.observation.model_copy(
        update={
            "state": http.observation.state.model_copy(update={"deck": ("owned",)}),
            "objects": {"owned": card},
        }
    )
    notes = Notes(NotesState(plan="Initial plan.", other="Reserve potion."))

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
            for plan in ("Block until CARD.ONE arrives.", ""):
                result = await tools.dispatch(
                    ToolCall("write", "run_notes", json.dumps({"mode": "write", "plan": plan}))
                )
                assert isinstance(result.outcome, ReadCompleted)
                assert isinstance(result.data, NotesUpdated)
                assert notes.read().plan == plan
                assert notes.read().other == "Reserve potion."
            for fields in (
                {"plan": "x" * 301},
                {"wants": ["Old format."]},
                {"plan": "Must not write.", "summary": "Only for a handoff."},
            ):
                result = await tools.dispatch(
                    ToolCall("invalid", "run_notes", json.dumps({"mode": "write", **fields}))
                )
                assert not isinstance(result.outcome, ReadCompleted)
                assert notes.read().plan == ""
                assert notes.read().other == "Reserve potion."
            result = await tools.dispatch(ToolCall("read", "run_notes", '{"mode":"read"}'))
            assert isinstance(result.data, NotesText)
            assert result.data.notes.plan == ""
            for core in ([card.definition_id] * 2, ["x"] * 6):
                result = await tools.dispatch(
                    ToolCall(
                        "invalid-core", "run_notes", json.dumps({"mode": "write", "core": core})
                    )
                )
                assert not isinstance(result.outcome, ReadCompleted)
                assert notes.read().core == ()
            result = await tools.dispatch(
                ToolCall(
                    "core",
                    "run_notes",
                    json.dumps({"mode": "write", "core": ["CARD.NOT_OWNED", card.definition_id]}),
                )
            )
            assert isinstance(result.outcome, ReadCompleted)
            assert notes.read().core == ("CARD.NOT_OWNED", card.definition_id)
            assert notes.read().other == "Reserve potion."
            assert isinstance(http.observation.state, native.RunState)
            http.observation = http.observation.model_copy(
                update={"state": http.observation.state.model_copy(update={"deck": ()})}
            )
            result = await tools.dispatch(ToolCall("retained", "run_notes", '{"mode":"read"}'))
            assert isinstance(result.data, NotesText)
            assert result.data.notes.core == ("CARD.NOT_OWNED", card.definition_id)
            assert http.actions == []

    asyncio.run(scenario())


@pytest.mark.parametrize("combine_summary", [False, True])
def test_handoff_closes_bad_calls_without_side_effects_and_can_update_notes(
    tmp_path: Path, combine_summary: bool
) -> None:
    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.model_journal import RecordedLlm
    from sts2_harness.results import HandoffCompletion

    summary = "Report assessed without acting."
    http = RunHTTP(
        [
            reply(
                ("game", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}'),
                ("write", "run_notes", '{"mode":"write","other":"must not write"}'),
            ),
            reply(
                (
                    "invalid",
                    "run_notes",
                    '{"mode":"write","summary":"A write needs a note field."}',
                ),
            ),
            reply(("read", "run_notes", '{"mode":"read"}')),
            reply(("forbidden", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
            reply(
                (
                    "update",
                    "run_notes",
                    json.dumps(
                        {
                            "mode": "write",
                            "other": "Use the next shop deliberately.",
                            **({"summary": summary} if combine_summary else {}),
                        }
                    ),
                ),
            ),
            *([] if combine_summary else [reply(text=summary)]),
            reply(text="Next report assessed."),
        ]
    )
    notes = Notes("")

    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "handoff.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="handoff",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
        )
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
                agent = RunAgent(
                    lambda: RecordedLlm(
                        llm, journal, role=native.DecisionOwner.RUN, stream="run:1"
                    ),
                    tools,
                    budget,
                )
                result = await execution.run_unit(agent, HandoffTask(handoff()))
                assert result == TaskComplete(summary)
                assert len(http.inputs) == (5 if combine_summary else 6)
                record = journal.requests()[-1]
                assert record.handoff == (
                    HandoffCompletion(call_id="update", summary=summary)
                    if combine_summary
                    else None
                )
                assert record.attempts[-1].handoff is MISSING
                assert await execution.run_unit(
                    agent, HandoffTask(handoff("Assess the next battle."))
                ) == TaskComplete("Next report assessed.")
        journal.close()
        assert notes.read().other == "Use the next shop deliberately."
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
        assert json.loads(outputs[3]["output"])["data"] == {
            "notes": NotesView().model_dump(mode="json")
        }
        assert isinstance(outputs[-1]["output"], str)
        assert json.loads(outputs[-1]["output"])["data"] == {
            "updated": True,
            **({"summary": summary} if combine_summary else {}),
        }
        first = http.inputs[0]["input"]
        assert isinstance(first, list) and isinstance(first[-1], dict)
        assert isinstance(first[-1]["content"], str)
        task = json.loads(first[-1]["content"])
        assert task == {
            "task": "handoff",
            "battle_report": handoff().report.model_dump(mode="json"),
            "combat_review": None,
            "notes": NotesView().model_dump(mode="json"),
        }

    asyncio.run(scenario())


def test_enter_act_retries_initialization_and_delivers_current_notes_in_fresh_context(
    tmp_path: Path,
) -> None:

    from sts2_harness.execution.recovery import DecisionInterrupted
    from sts2_harness.knowledge import DocumentView, SkillRead
    from sts2_harness.materials import DocumentSource, FileDocuments
    from sts2_harness.results import Reason, ReasonCode
    from sts2_harness.run import ActStart

    http = RunHTTP([reply(text="Old act handoff."), reply(text="New act handoff.")])
    notes = Notes(NotesState(other="Initial notes."))
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
                notes.write(NotesState(other="Notes persist across acts."), {"other"})
                agent = RunAgent(lambda: llm, tools, budget, materials=materials)
                path.unlink()
                next_act = ActStart(http.observation)
                with pytest.raises(DecisionInterrupted) as caught:
                    await agent.enter_act(next_act)
                assert caught.value.pause_required
                assert isinstance(caught.value.reason, Reason)
                assert caught.value.reason.code is ReasonCode.CONTENT_UNAVAILABLE
                assert len(http.inputs) == 1 and budget.failures == 0
                assert notes.read().other == "Notes persist across acts."
                path.write_text("New act guidance.")
                await agent.enter_act(next_act)
                assert len(http.inputs) == 1
                notes.write(NotesState(other="Updated after initialization."), {"other"})
                second = await execution.run_unit(agent, HandoffTask(handoff()))
                assert isinstance(second, TaskComplete)
        latest = http.inputs[-1]["input"]
        assert isinstance(latest, list) and len(latest) == 4
        task = latest[-1]
        assert isinstance(task, dict) and isinstance(task["content"], str)
        assert json.loads(task["content"])["notes"]["other"] == "Updated after initialization."
        guide = latest[2]
        assert isinstance(guide, dict) and isinstance(guide["content"], str)
        assert json.loads(guide["content"])["guidance"][0]["text"] == "New act guidance."
        assert http.actions == []

    asyncio.run(scenario())


def test_journal_reads_legacy_attempt_handoff_without_handoff_table(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    from sts2_harness.journal import Journal, Manifest
    from sts2_harness.journal_models import ModelAttempt
    from sts2_harness.model_journal import RecordedLlm
    from sts2_harness.results import HandoffCompletion

    summary = "Legacy handoff summary."
    http = RunHTTP([reply(text="ok")])

    async def scenario() -> None:
        path = tmp_path / "legacy-handoff.sqlite"
        journal = Journal.create(
            path,
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="legacy-handoff",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
        )
        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace="legacy", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=Notes(""),
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
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
                )
                assert await execution.run_unit(agent, HandoffTask(handoff())) == TaskComplete("ok")
        record = journal.requests()[-1]
        legacy_completion = HandoffCompletion(call_id="legacy-call", summary=summary)
        journal.model_attempt(
            record.sequence,
            ModelAttempt.model_validate(
                {**record.attempts[-1].model_dump(mode="python"), "handoff": legacy_completion}
            ),
        )
        journal.close()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DROP TABLE handoff_completions")
            connection.commit()
        reopened = Journal.open(path)
        try:
            loaded = reopened.requests()[-1]
            assert loaded.handoff == legacy_completion
            assert loaded.attempts[-1].handoff == legacy_completion
        finally:
            reopened.close()

    asyncio.run(scenario())


def test_journal_handoff_guards_read_only_and_rejects_inconsistent_records(tmp_path: Path) -> None:
    from sts2_harness.capacity import ModelAttemptReference
    from sts2_harness.journal import ArchiveError, Journal, Manifest
    from sts2_harness.journal_models import ModelAttempt
    from sts2_harness.llm.messages import FailureKind
    from sts2_harness.model_journal import RecordedLlm
    from sts2_harness.results import HandoffCompletion

    completion = HandoffCompletion(call_id="handoff-1", summary="Summary.")

    async def record_one_handoff_request(name: str) -> tuple[Journal, int]:
        journal = Journal.create(
            tmp_path / f"{name}.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id=name,
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
        )
        budget = ToolRecoveryBudget()
        http = RunHTTP([reply(text="ok")])
        async with ModClient("http://mod", transport=httpx.MockTransport(http.mod)) as client:
            execution = Execution(
                LiveGame(client, namespace=name, game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                notes=Notes(""),
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
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
                )
                assert await execution.run_unit(agent, HandoffTask(handoff())) == TaskComplete("ok")
        return journal, journal.requests()[-1].sequence

    async def scenario() -> None:
        # 1. Read-only archive rejects record_handoff.
        ro_journal, sequence = await record_one_handoff_request("ro")
        ro_path = ro_journal.path
        ro_journal.close()
        reader = Journal.open(ro_path)
        try:
            with pytest.raises(ArchiveError):
                reader.record_handoff(
                    ModelAttemptReference(sequence=sequence, attempt=1), completion
                )
        finally:
            reader.close()

        # 2. Handoff attached to a failed/undelivered request is rejected on read.
        failed_journal, failed_seq = await record_one_handoff_request("failed-live")
        try:
            failed_journal.finish_model(failed_seq, failure=FailureKind.TIMEOUT)
            failed_journal.record_handoff(
                ModelAttemptReference(sequence=failed_seq, attempt=1), completion
            )
            with pytest.raises(ArchiveError):
                failed_journal.requests()
        finally:
            failed_journal.close()

        # 3. Handoff referencing a non-final attempt is rejected on read.
        stale_attempt, stale_seq = await record_one_handoff_request("stale-attempt")
        try:
            attempt = stale_attempt.requests()[-1].attempts[-1]
            stale_attempt.model_attempt(stale_seq, attempt.model_copy(update={"index": 2}))
            stale_attempt.record_handoff(
                ModelAttemptReference(sequence=stale_seq, attempt=1), completion
            )
            with pytest.raises(ArchiveError):
                stale_attempt.requests()
        finally:
            stale_attempt.close()

        # 4. Stray legacy handoff on a non-final attempt is rejected on read.
        stray, stray_seq = await record_one_handoff_request("stray-legacy")
        try:
            attempt = stray.requests()[-1].attempts[-1]
            stray.model_attempt(
                stray_seq,
                ModelAttempt.model_validate(
                    {**attempt.model_dump(mode="python"), "handoff": completion}
                ),
            )
            stray.model_attempt(stray_seq, attempt.model_copy(update={"index": 2}))
            with pytest.raises(ArchiveError):
                stray.requests()
        finally:
            stray.close()

    asyncio.run(scenario())
