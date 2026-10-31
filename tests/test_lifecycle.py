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
                reply(("notes", "run_notes", '{"mode":"replace","text":"Reserve for act two."}')),
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
            notes: str,
        ) -> CodeCombat:
            openings.append(notes)
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
                initial_notes="Initial strategy.",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            combat_factory=combat if code_combat else None,
            documents=FileDocuments(tuple(sources)),
            diagnostics=diagnostics,
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL
            assert session.notes.read() == "Reserve for act two."
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
        assert archive.manifest.initial_notes == "Initial strategy."
        requests = archive.requests()
        assert {request.stream for request in requests} == (
            {"run:1", "run:2"} if code_combat else {"run:1", "run:2", "combat:1", "combat:2"}
        )
        first_run = next(request for request in requests if request.stream == "run:2")
        from sts2_harness.journal_models import GuidanceText, RunTaskSnapshot, TextSnapshot

        assert len(first_run.input) == 3
        task = first_run.input[-1]
        assert isinstance(task, RunTaskSnapshot)
        assert task.content.strategy_notes == "Reserve for act two."
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


@pytest.mark.parametrize("resume", ["same_run", "terminal", "different_run", "no_run"])
def test_act_initialization_waits_for_start_and_rechecks_lifetime_without_replaying(
    tmp_path: Path, resume: str
) -> None:
    from sts2_harness.materials import DocumentSource, FileDocuments

    campaign = Campaign(code_combat=True)
    guide = tmp_path / "act-two.txt"
    documents = FileDocuments(
        (
            DocumentSource(
                id="act-two",
                name="Act two",
                description="Synthetic guidance",
                path=guide,
            ),
        )
    )
    paused = asyncio.Event()
    waiting = asyncio.Event()
    release = asyncio.Event()
    is_paused = False

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal is_paused
        if request.url.path == "/control":
            is_paused = True
            paused.set()
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status" and is_paused:
            return httpx.Response(
                200,
                json={
                    "location": campaign.current.location.model_dump(mode="json"),
                    "control_intent": "paused",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        return campaign.mod(request)

    async def wait(seconds: float) -> None:
        waiting.set()
        await release.wait()

    async def scenario() -> None:
        nonlocal is_paused
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=True, act_guidance={2: ("act-two",)}, battle_guidance=()
                ),
                recording_id="initialization",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="Initial strategy.",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            documents=documents,
            combat_factory=lambda execution, recovery, observation, notes: CodeCombat(),
            wait=wait,
        ) as session:
            running = asyncio.create_task(session.run())
            await asyncio.wait_for(paused.wait(), 3)
            await asyncio.wait_for(waiting.wait(), 3)
            assert len(campaign.actions) == 3 and len(campaign.inputs) == 4
            assert session.notes.read() == "Reserve for act two."
            assert not running.done()
            guide.write_text("Synthetic act two planning.", encoding="utf-8")
            if resume == "terminal":
                campaign.states[campaign.index] = terminal(campaign.current)
            elif resume in ("different_run", "no_run"):
                campaign.states[campaign.index] = campaign.current.model_copy(
                    update={
                        "location": campaign.current.location.model_copy(
                            update={
                                "run_id": "another" if resume == "different_run" else None,
                                "owner": native.DecisionOwner.RUN
                                if resume == "different_run"
                                else None,
                            }
                        )
                    }
                )
            is_paused = False
            release.set()
            ended = await asyncio.wait_for(running, 3)
            assert ended.reason is (
                RunEndReason.RUN_CHANGED
                if resume == "different_run"
                else RunEndReason.NO_RUN
                if resume == "no_run"
                else RunEndReason.TERMINAL
            )
        assert len(campaign.actions) == (5 if resume == "same_run" else 3)
        assert len(campaign.inputs) == (6 if resume == "same_run" else 4)
        archive = Journal.open(tmp_path / "archive.sqlite")
        assert {request.stream for request in archive.requests()} == (
            {"run:1", "run:2"} if resume == "same_run" else {"run:1"}
        )
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
            self.notes.replace("Handoff assessed before opening.")
            return TaskComplete()

        async def accept(self, outcome: ActionToolResult) -> None:
            if isinstance(outcome.outcome, Rejected):
                rejected.append(outcome)

    def combat(
        execution: Execution,
        recovery: ToolRecoveryBudget,
        observation: native.Observation,
        notes: str,
    ) -> CodeCombat:
        if openings:
            assert recovery.failures == 0
        openings.append(notes)
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
                initial_notes="Opening strategy.",
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


@pytest.mark.parametrize("resume", ["same_run", "changed_act", "terminal"])
def test_transition_pause_admits_latest_state_before_loading_new_act(
    tmp_path: Path, resume: str
) -> None:
    from sts2_harness.journal_models import DocumentRead
    from sts2_harness.materials import DocumentSource, FileDocuments

    campaign = Campaign(code_combat=True)
    sources: list[DocumentSource] = []
    for act in (2, 3):
        path = tmp_path / f"act-{act}.txt"
        path.write_text(f"Synthetic act {act}.", encoding="utf-8")
        sources.append(
            DocumentSource(id=f"act-{act}", name=f"Act {act}", description="Synthetic", path=path)
        )
    paused = False
    waiting = asyncio.Event()
    release = asyncio.Event()

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal paused
        if request.url.path == "/status" and paused:
            return httpx.Response(
                200,
                json={
                    "location": campaign.current.location.model_dump(mode="json"),
                    "control_intent": "paused",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        result = campaign.mod(request)
        if request.url.path == "/action" and campaign.index == 3:
            paused = True
        return result

    async def wait(seconds: float) -> None:
        waiting.set()
        await release.wait()

    async def scenario() -> None:
        nonlocal paused
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=True,
                    act_guidance={2: ("act-2",), 3: ("act-3",)},
                    battle_guidance=(),
                ),
                recording_id="manual-pause",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="Initial strategy.",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(campaign.model)),
            documents=FileDocuments(tuple(sources)),
            combat_factory=lambda execution, recovery, observation, notes: CodeCombat(),
            wait=wait,
        ) as session:
            running = asyncio.create_task(session.run())
            await asyncio.wait_for(waiting.wait(), 3)
            assert len(campaign.actions) == 3 and len(campaign.inputs) == 4
            archive = Journal.open(tmp_path / "archive.sqlite")
            assert not any(isinstance(item.request, DocumentRead) for item in archive.materials())
            archive.close()
            if resume == "changed_act":
                campaign.states[3] = campaign.states[3].model_copy(
                    update={
                        "location": campaign.states[3].location.model_copy(
                            update={"node_id": "latest-act"}
                        ),
                        "state": campaign.states[3].state.model_copy(
                            update={
                                "progress": campaign.states[3].state.progress.model_copy(
                                    update={"act": 3, "definition_id": "ACT.3"}
                                )
                            }
                        ),
                    }
                )
                campaign.states[4] = battle_state(3, "battle2")
            elif resume == "terminal":
                campaign.states[3] = terminal(campaign.current)
            paused = False
            release.set()
            ended = await asyncio.wait_for(running, 3)
            assert ended.reason is RunEndReason.TERMINAL
        archive = Journal.open(tmp_path / "archive.sqlite")
        reads = {
            item.request.id
            for item in archive.materials()
            if isinstance(item.request, DocumentRead)
        }
        assert reads == (
            set() if resume == "terminal" else {"act-3"} if resume == "changed_act" else {"act-2"}
        )
        assert len(campaign.actions) == (3 if resume == "terminal" else 5)
        archive.close()

    asyncio.run(scenario())


def test_session_keeps_same_act_history_and_starts_fresh_after_reward_free_transition(
    tmp_path: Path,
) -> None:
    from sts2_harness.decisions import HandoffTask
    from sts2_harness.execution import Execution
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.notes import Notes
    from sts2_harness.run import ActStart

    campaign = Campaign(code_combat=True)
    campaign.states = [
        run_state(1, "start"),
        battle_state(1, "boss-one"),
        battle_state(1, "boss-two"),
        run_state(2, "next-act"),
        battle_state(2, "next-battle"),
        terminal(battle_state(2, "next-battle")),
    ]
    openings: list[str] = []

    class Run:
        def __init__(self, notes: Notes) -> None:
            self.notes = notes
            self.saw_first_boss = False

        async def enter_act(self, start: ActStart) -> None:
            pass

        async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
            if isinstance(task, HandoffTask):
                if observation.location.node_id == "boss-two":
                    self.notes.replace(
                        "First boss reviewed."
                        if self.saw_first_boss
                        else "First boss history lost."
                    )
                return TaskComplete()
            if observation.location.node_id == "start":
                self.saw_first_boss = True
            if observation.location.node_id == "next-act":
                self.notes.replace(
                    self.notes.read() + (" Same history." if self.saw_first_boss else " Fresh act.")
                )
            return ActionDecision(ActionProposal((ActionStep(ActionKind.INTERACT, "travel"),)))

        async def accept(self, outcome: ActionToolResult) -> None:
            pass

    def combat(
        execution: Execution,
        recovery: ToolRecoveryBudget,
        observation: native.Observation,
        notes: str,
    ) -> CodeCombat:
        openings.append(notes)
        return CodeCombat()

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="double-boss",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="Initial strategy.",
            ),
            tmp_path / "archive.sqlite",
            run_factory=lambda execution, notes, recovery: Run(notes),
            combat_factory=combat,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
            assert session.notes.read() == "First boss reviewed. Fresh act."
        assert openings == [
            "Initial strategy.",
            "First boss reviewed.",
            "First boss reviewed. Fresh act.",
        ]
        assert [action.expected_route.node_id for action in campaign.actions] == [
            "start",
            "boss-one",
            "boss-two",
            "next-act",
            "next-battle",
        ]

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
                initial_notes="Initial strategy.",
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
