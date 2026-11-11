import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue
from test_combat import reply
from test_mod_client import COMBAT, RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    Decision,
    DecisionTask,
    TaskComplete,
)
from sts2_harness.game import RunEndReason
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.note_values import NotesState, NotesView
from sts2_harness.results import ActionToolResult


def run_state(act: int, node: str) -> native.Observation:
    observation = native.Observation.model_validate_json(RUN)
    return observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"node_id": node}),
            "state": observation.state.model_copy(
                update={
                    "progress": observation.state.progress.model_copy(
                        update={"act": act, "definition_id": f"ACT.{act}"}
                    )
                }
            ),
            "map": native.PublicMap(bosses=(), nodes=(), legal_destinations=None),
        }
    )


def battle_state(act: int, node: str) -> native.Observation:
    observation = native.Observation.model_validate_json(COMBAT)
    return observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"node_id": node}),
            "state": observation.state.model_copy(
                update={
                    "progress": observation.state.progress.model_copy(
                        update={"act": act, "definition_id": f"ACT.{act}"}
                    )
                }
            ),
            "map": native.PublicMap(bosses=(), nodes=(), legal_destinations=None),
        }
    )


def terminal(observation: native.Observation) -> native.Observation:
    data = json.loads(observation.to_json())
    data["location"]["owner"] = None
    data["input_readiness"] = "terminal"
    data["state"] = {
        "control": "unavailable",
        "progress": data["state"]["progress"],
        "player": data["state"]["player"],
        "relics": [],
        "potions": [],
        "actions": [],
        "selection": None,
        "phase": "terminal",
        "screen": "run_complete",
        "result": "victory",
        "deck": [],
    }
    return native.Observation.model_validate_json(json.dumps(data))


def unavailable_history(observation: native.Observation) -> httpx.Response:
    return httpx.Response(
        200,
        content=native.InspectRejected(
            status=native.ResultStatus.REJECTED,
            error=native.AreaUnavailable(
                code=native.ErrorCode.AREA_UNAVAILABLE,
                message="This fixture does not supply battle history.",
            ),
            observation=observation,
        ).to_json(),
    )


class Campaign:
    def __init__(self, *, code_combat: bool = False) -> None:
        self.states = [
            run_state(1, "start"),
            battle_state(1, "battle1"),
            run_state(1, "reward"),
            run_state(2, "next-act"),
            battle_state(2, "battle2"),
        ]
        self.states.append(terminal(self.states[-1]))
        for index in (0, 3):
            state = json.loads(self.states[index].to_json())
            state["state"]["progress"]["position"] = "0:0"
            state["state"]["actions"][0].update(enabled=True, destination="1:0")
            state["state"]["actions"][0].pop("reason", None)
            state["map"] = {
                "bosses": [],
                "nodes": [
                    {"position": "0:0", "room_type": "event", "children": ["1:0", "1:1"]},
                    {"position": "1:0", "room_type": "monster", "children": []},
                    {"position": "1:1", "room_type": "monster", "children": []},
                ],
                "legal_destinations": ["1:0", "1:1"],
            }
            self.states[index] = native.Observation.model_validate_json(json.dumps(state))
        reward = json.loads(self.states[2].to_json())
        reward["state"]["screen"] = "event"
        reward["state"]["actions"][0]["ref"] = "advance"
        self.states[2] = native.Observation.model_validate_json(json.dumps(reward))
        self.index = 0
        self.actions: list[native.StepRequest] = []
        self.presentations: list[native.PresentRequest] = []
        self.map = native.PublicMap(bosses=(), nodes=(), legal_destinations=None)
        self.inputs: list[dict[str, JsonValue]] = []
        first = [
            reply(("battle1", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="A useful battle review."),
        ]
        second = [reply(("battle2", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))]
        self.responses = iter(
            [
                reply(
                    (
                        "plan",
                        "run_plan",
                        '{"reason":"Enter combat.","edges":[{"from":"0:0","to":"1:0"}]}',
                    )
                ),
                reply(("travel", "run_next", '{"reason":"Enter combat."}')),
                *([] if code_combat else first),
                reply(("notes", "run_notes", '{"mode":"write","other":"Reserve for act two."}')),
                reply(("advance", "run_act", '{"steps":[{"kind":"interact","action":"advance"}]}')),
                reply(
                    (
                        "plan2",
                        "run_plan",
                        '{"reason":"Enter combat.","edges":[{"from":"0:0","to":"1:0"}]}',
                    )
                ),
                reply(("travel2", "run_next", '{"reason":"Enter combat."}')),
                *([] if code_combat else second),
            ]
        )

    @property
    def current(self) -> native.Observation:
        return self.states[self.index]

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": self.current.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": self.current.input_readiness.value,
                    "receipt": None,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=self.current
                ).to_json(),
            )
        if request.url.path == "/inspect":
            query = native.InspectRequest.model_validate_json(request.content)
            if query.query.area is native.InspectArea.COMBAT_HISTORY:
                return unavailable_history(self.current)
            assert query.query.area is native.InspectArea.MAP
            if self.current.map is MISSING:
                self.states[self.index] = self.current.model_copy(update={"map": self.map})
            return httpx.Response(
                200,
                content=native.InspectCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=self.current,
                    data=native.MapData(map=self.map),
                ).to_json(),
            )
        if request.url.path == "/map/present":
            presentation = native.PresentRequest.model_validate_json(request.content)
            self.presentations.append(presentation)
            return httpx.Response(
                200,
                json={
                    "operation_id": presentation.operation_id,
                    "status": "rejected",
                    "error": {"code": "action_unavailable", "message": "Presentation unavailable."},
                },
            )
        assert request.url.path == "/action"
        step = native.StepRequest.model_validate_json(request.content)
        self.actions.append(step)
        self.index += 1
        return httpx.Response(
            200,
            json={"operation_id": step.operation_id, "status": "completed", "boundary": "ready"},
        )

    def model(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.content))
        return httpx.Response(
            200, content=next(self.responses), headers={"Content-Type": "text/event-stream"}
        )


class CodeCombat:
    async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
        if not isinstance(task, ActTask):
            return TaskComplete()
        return ActionDecision(ActionProposal((ActionStep(ActionKind.END_TURN),)))

    async def accept(self, outcome: ActionToolResult) -> None:
        pass


@pytest.mark.parametrize("code_combat", [False, True])
def test_session_keeps_notes_and_replaces_act_context_before_next_battle(
    tmp_path: Path, code_combat: bool
) -> None:
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics

    console = StringIO()
    diagnostics = Diagnostics("campaign", console=console)
    campaign = Campaign(code_combat=code_combat)
    from sts2_harness.knowledge import SubjectId
    from sts2_harness.materials import DocumentSource, FileDocuments

    sources: list[DocumentSource] = []
    for identity, subject in (
        ("act-one", SubjectId(category="act", definition_id="ACT.1")),
        ("act-two", SubjectId(category="act", definition_id="ACT.2")),
        ("boss", SubjectId(category="encounter", definition_id="ENCOUNTER.BOSS")),
        ("other-boss", SubjectId(category="encounter", definition_id="ENCOUNTER.OTHER")),
        ("enemy", SubjectId(category="enemy", definition_id="ENEMY.ONE")),
    ):
        path = tmp_path / f"{identity}.txt"
        path.write_text(f"Synthetic {identity} guidance.", encoding="utf-8")
        sources.append(
            DocumentSource(
                id=identity, name=identity, description="Synthetic", path=path, subjects=(subject,)
            )
        )
    from pydantic import MISSING

    initial_map = campaign.states[0].map
    assert initial_map is not MISSING
    campaign.map = native.PublicMap(
        bosses=(
            native.NamedDefinition(name="Boss", definition_id="ENCOUNTER.BOSS"),
            native.NamedDefinition(name="Other Boss", definition_id="ENCOUNTER.OTHER"),
        ),
        nodes=initial_map.nodes,
        legal_destinations=initial_map.legal_destinations,
    )
    campaign.states[0] = campaign.states[0].model_copy(update={"map": MISSING})
    battle = json.loads(campaign.states[1].to_json())
    battle["encounter"] = {"name": "Boss", "definition_id": "ENCOUNTER.BOSS"}
    campaign.states[1] = native.Observation.model_validate_json(json.dumps(battle))
    openings: list[str] = []

    async def scenario() -> None:
        from sts2_harness.execution import Execution
        from sts2_harness.execution.recovery import ToolRecoveryBudget

        def combat(
            execution: Execution,
            recovery: ToolRecoveryBudget,
            observation: native.Observation,
            notes: NotesView,
        ) -> CodeCombat:
            openings.append(notes.other)
            return CodeCombat()

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=True, act_guidance={}, battle_guidance=()
                ),
                recording_id="campaign",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(other="Initial strategy."),
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            combat_factory=combat if code_combat else None,
            documents=FileDocuments(tuple(sources)),
            diagnostics=diagnostics,
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL
            assert session.notes.read().other == "Reserve for act two."
        assert [action.expected_route.node_id for action in campaign.actions] == [
            "start",
            "battle1",
            "reward",
            "next-act",
            "battle2",
        ]
        if code_combat:
            assert openings == ["Initial strategy.", "Reserve for act two."]
        from sts2_harness.game import Inspect

        archive = Journal.open(tmp_path / "archive.sqlite")
        assert any(isinstance(item.request, Inspect) for item in archive.operations())
        from sts2_harness.game import Present, Presented
        from sts2_harness.journal_models import PlanArchiveData, ResultSnapshot

        presentations = [item for item in archive.operations() if isinstance(item.request, Present)]
        assert len(campaign.presentations) == len(presentations) == 2
        assert all(
            item.reply is not None and isinstance(item.reply.outcome, Presented)
            for item in presentations
        )
        assert archive.manifest.initial_notes == NotesState(other="Initial strategy.")
        requests = archive.requests()
        plan_results = [
            message.data.value
            for request in requests
            for message in request.input
            if isinstance(message, ResultSnapshot) and isinstance(message.data, PlanArchiveData)
        ]
        assert len(plan_results) >= 2
        assert all(result.plan.status == "accepted" for result in plan_results)
        assert all(result.presentation.status == "not_attempted" for result in plan_results)
        assert {request.stream for request in requests} == (
            {"run:1", "run:2"} if code_combat else {"run:1", "run:2", "combat:1", "combat:2"}
        )
        first_run = next(request for request in requests if request.stream == "run:2")
        from sts2_harness.context.models import RunActContent
        from sts2_harness.journal_models import GuidanceText, RunTaskSnapshot, TextSnapshot

        assert len(first_run.input) == 3
        task = first_run.input[-1]
        assert isinstance(task, RunTaskSnapshot)
        assert isinstance(task.content, RunActContent)
        assert isinstance(task.content.observation.notes, NotesView)
        assert task.content.observation.notes.other == "Reserve for act two."
        system_guidance = first_run.input[1]
        assert isinstance(system_guidance, TextSnapshot)
        assert isinstance(system_guidance.value, GuidanceText)
        assert [doc.id for doc in system_guidance.value.documents] == ["act-two"]
        first_guidance = requests[0].input[1]
        assert isinstance(first_guidance, TextSnapshot)
        assert isinstance(first_guidance.value, GuidanceText)
        assert {doc.id for doc in first_guidance.value.documents} == {
            "act-one",
            "boss",
            "other-boss",
        }
        if not code_combat:
            battle = next(request for request in requests if request.stream == "combat:1")
            battle_guidance = battle.input[1]
            assert isinstance(battle_guidance, TextSnapshot)
            assert isinstance(battle_guidance.value, GuidanceText)
            assert {doc.id for doc in battle_guidance.value.documents} == {"enemy", "boss"}
        from sts2_harness.context.models import RunActHandoffContent

        handoff_task = next(
            message
            for request in requests
            for message in request.input
            if isinstance(message, RunTaskSnapshot)
            and isinstance(message.content, RunActHandoffContent)
        )
        assert isinstance(handoff_task.content, RunActHandoffContent)
        assert handoff_task.content.battle_report.encounter == native.NamedDefinition(
            name="Boss", definition_id="ENCOUNTER.BOSS"
        )
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        assert [line["act"] for line in events if line["event"] == "act_entered"] == [1, 2]
        assert [line["role"] for line in events if line["event"] == "role_changed"] == [
            "run",
            "combat",
            "run",
            "combat",
        ]
        assert [line["node_id"] for line in events if line["event"] == "battle_started"] == [
            "battle1",
            "battle2",
        ]
        finished = [line for line in events if line["event"] == "battle_finished"]
        assert [(line["node_id"], line["ending"]) for line in finished] == [
            ("battle1", "handoff"),
            ("battle2", "run_ended"),
        ]
        diagnostics.close()
        archive.close()

    asyncio.run(scenario())


def test_handoff_survives_interruption_and_rejection_before_constructing_next_combat(
    tmp_path: Path,
) -> None:
    from sts2_harness.decisions import HandoffTask
    from sts2_harness.execution import Execution
    from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
    from sts2_harness.notes import Notes
    from sts2_harness.results import Reason, ReasonCode, Rejected
    from sts2_harness.run import ActStart

    campaign = Campaign(code_combat=True)
    campaign.states = [
        run_state(1, "start"),
        battle_state(1, "battle1"),
        battle_state(1, "battle2"),
        terminal(battle_state(1, "battle2")),
    ]
    openings: list[str] = []
    rejected: list[ActionToolResult] = []

    class Run:
        def __init__(self, notes: Notes, recovery: ToolRecoveryBudget) -> None:
            self.notes = notes
            self.recovery = recovery
            self.handoffs = 0

        async def enter_act(self, start: ActStart) -> None:
            pass

        async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
            proposal = ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "travel"),)))
            if isinstance(task, ActTask):
                return proposal
            assert isinstance(task, HandoffTask)
            assert task.handoff.report.location.act == 1 and task.handoff.review is None
            self.handoffs += 1
            if self.handoffs == 1:
                raise DecisionInterrupted(
                    Reason(code=ReasonCode.QUERY_FAILED, message="Synthetic query interruption."),
                    self.recovery.record_failure(),
                )
            if self.handoffs == 2:
                return proposal
            self.notes.write(NotesState(other="Handoff assessed before opening."), {"other"})
            return TaskComplete()

        async def accept(self, outcome: ActionToolResult) -> None:
            if isinstance(outcome.outcome, Rejected):
                rejected.append(outcome)

    def combat(
        execution: Execution,
        recovery: ToolRecoveryBudget,
        observation: native.Observation,
        notes: NotesView,
    ) -> CodeCombat:
        if openings:
            assert recovery.failures == 0
        openings.append(notes.other)
        return CodeCombat()

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="handoff",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(other="Opening strategy."),
            ),
            tmp_path / "archive.sqlite",
            run_factory=lambda execution, notes, recovery: Run(notes, recovery),
            combat_factory=combat,
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL
        assert openings == ["Opening strategy.", "Handoff assessed before opening."]
        assert [step.expected_route.node_id for step in campaign.actions] == [
            "start",
            "battle1",
            "battle2",
        ]
        assert len(rejected) == 1 and isinstance(rejected[0].outcome, Rejected)
        assert rejected[0].outcome.reason.code is ReasonCode.WRONG_ROLE
        archive = Journal.open(tmp_path / "archive.sqlite")
        assert archive.requests() == ()
        archive.close()

    asyncio.run(scenario())


def test_unknown_act_does_not_start_a_new_run_context(tmp_path: Path) -> None:
    campaign = Campaign(code_combat=True)
    reward = campaign.states[2]
    campaign.states[2] = reward.model_copy(
        update={
            "state": reward.state.model_copy(
                update={"progress": reward.state.progress.model_copy(update={"act": None})}
            )
        }
    )

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="unknown-act",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(other="Initial strategy."),
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            combat_factory=lambda execution, recovery, observation, notes: CodeCombat(),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        archive = Journal.open(tmp_path / "archive.sqlite")
        requests = archive.requests()
        assert [request.stream for request in requests] == ["run:1"] * 4 + ["run:2"] * 2
        archive.close()

    asyncio.run(scenario())
