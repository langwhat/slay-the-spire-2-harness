import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Literal

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign, battle_state, run_state, terminal
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame, Settlement
from sts2_harness.journal import (
    HeadlessInitialConditions,
    Journal,
    Manifest,
    NativeStartReceipt,
    RecordedGame,
    SessionMaterials,
    StandardCharacter,
)
from sts2_harness.knowledge import Documents
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import RunSession, SessionDependencies
from sts2_harness_eval import ModelAnchor, Reached, reach, take_over
from sts2_harness_eval.cases import (
    AdmissionRequest,
    AfterDecisionsSpec,
    Case,
    CaseStore,
    CheckpointRef,
    plan_admission,
)
from sts2_harness_eval.checkpoints import (
    CheckpointClient,
    CheckpointUncertain,
    IdentityFacts,
    IdentityRead,
    ReloadTarget,
)
from sts2_harness_eval.native import LoadedTarget, NativeLaunch, NativeRestorer
from sts2_harness_eval.sample import SampleBudget
from sts2_harness_eval.takeover import TakeoverCompleted, TakeoverIncomplete
from sts2_harness_eval.verification import LoadedCorrespondence


class Source(Campaign):
    def __init__(self) -> None:
        super().__init__()
        opening = run_state(1, "start")
        opening = opening.model_copy(
            update={
                "state": opening.state.model_copy(
                    update={
                        "progress": opening.state.progress.model_copy(
                            update={"room_type": "monster"}
                        ),
                    }
                )
            }
        )
        battle = battle_state(1, "battle")
        assert isinstance(battle.state, native.CombatState)
        battle = battle.model_copy(
            update={
                "input_readiness": native.InputReadiness.READY,
                "state": battle.state.model_copy(
                    update={
                        "screen": native.CombatScreen.COMBAT,
                        "selection": None,
                        "turn": 1,
                    }
                ),
            }
        )
        data = battle.to_json()
        for old, new in (("c1", "card:1:1"), ("c2", "card:1:2"), ("e1", "enemy:1:1")):
            data = data.replace('"' + old + '"', '"' + new + '"')
        battle = native.Observation.model_validate_json(data)
        self.states = [opening, battle, battle, battle, terminal(battle)]
        self.responses = iter(
            [
                reply(("travel", "run_act", '{"steps":[{"kind":"interact","action":"travel"}]}')),
                reply(("inspect", "combat_inspect", '{"area":"hand"}')),
                reply(
                    (
                        "play",
                        "combat_act",
                        '{"steps":[{"kind":"play_card","card":"card:1:1","target":"enemy:1:1"}]}',
                    )
                ),
                reply(("turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(("finish", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            ]
        )

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            return httpx.Response(200, json=json.loads(request.content))
        if request.url.path == "/inspect":
            return httpx.Response(
                200,
                content=native.InspectCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=self.current,
                    data=native.DetailData(
                        objects=self.current.objects,
                        descriptions=self.current.descriptions,
                        modifier_descriptions=self.current.modifier_descriptions,
                        generated=self.current.generated,
                        mechanisms=self.current.mechanisms,
                    ),
                ).to_json(),
            )
        return super().mod(request)


async def source_case(
    path: Path,
    *,
    start: bool,
    source: Source | None = None,
    documents: Documents | None = None,
    target_position: int = 2,
    materials: SessionMaterials | None = None,
) -> Case:
    source = source or Source()
    manifest = Manifest(
        materials=materials
        if materials is not None
        else SessionMaterials(entry_documents_enabled=False, act_guidance={}, battle_guidance=()),
        recording_id="source",
        core_version="test",
        game_instance_id="game",
        run_id="run",
        initial_notes="A plan.",
        seed="SEED",
        template="test",
        headless=HeadlessInitialConditions(
            requested_character=StandardCharacter.IRONCLAD,
            requested_ascension=0,
            effective_settings_sha256="test",
            game_assembly_sha256="test",
            mod_version="test",
            mod_sha256="test",
            initial_observation_sha256="test",
            initial_control=native.ControlReply(mode=native.ControlIntent.RUNNING),
            start_receipt=NativeStartReceipt(
                status="completed", effect_verified=True, reason_code=None, seed="SEED"
            ),
        ),
    )
    with closing(Journal.create(path, manifest)) as journal:
        async with (
            ModClient("http://source", transport=httpx.MockTransport(source.mod)) as client,
            Llm(configuration(path.parent), transport=httpx.MockTransport(source.model)) as model,
        ):

            def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
                return RecordedLlm(model, journal, role=role, stream=stream)

            async with RunSession(
                manifest,
                SessionDependencies(
                    RecordedGame(
                        LiveGame(
                            client,
                            namespace="source",
                            audit=journal,
                            game_instance_id="game",
                            run_id="run",
                        ),
                        journal,
                    ),
                    RecordedPrompts(PackagedPrompts(), journal),
                    RecordedDocuments(documents or FileDocuments(), journal),
                    factory,
                ),
            ) as session:
                if start:
                    await session.start()
                await session.run()
        requests = journal.requests()
        target = next(
            item
            for item in requests
            if item.role is native.DecisionOwner.COMBAT and item.position == target_position
        )
        anchor = ModelAnchor("source", target.sequence)
    plan = plan_admission(
        AdmissionRequest(
            id="case",
            archive=path,
            anchor=anchor,
            endpoint=AfterDecisionsSpec(count=2),
        ),
        path.parent,
    )
    return Case(
        id="case",
        source=plan.source,
        endpoint=AfterDecisionsSpec(count=2),
        binding="b" * 64,
        evidence="e" * 64,
        checkpoint=CheckpointRef(
            sha256="a" * 64,
            bytes=1,
            boundary_sequence=plan.boundary_sequence,
            boundary_cursor=plan.boundary_cursor,
            target=ReloadTarget(
                canonical_seed="SEED",
                game_mode="standard",
                character="CHARACTER.IRONCLAD",
                ascension=0,
                act=1,
                floor=1,
            ),
        ),
    )


class FakeRestorer(NativeRestorer):
    def __init__(self, directory: Path, case: Case, *, uncertain: bool = False) -> None:
        super().__init__(NativeLaunch(directory, directory, directory), CaseStore(directory))
        plan = plan_admission(
            AdmissionRequest(
                id=case.id,
                archive=case.source.archive,
                anchor=case.source.anchor,
                endpoint=case.endpoint,
            ),
            directory,
        )
        data = plan.target.to_json()
        for old, new in (
            ("game", "B"),
            ("run", "B-run"),
            ("battle", "B-battle"),
            ("card:1:1", "card:9:1"),
            ("card:1:2", "card:9:2"),
            ("enemy:1:1", "enemy:9:1"),
        ):
            data = data.replace('"' + old + '"', '"' + new + '"')
        self.observation = native.Observation.model_validate_json(data)
        self.actions: list[native.StepRequest] = []
        self.launches = 0
        self.inspections: list[native.InspectQuery] = []
        self.started = False
        self.closed = False
        self.uncertain = uncertain

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            assert not self.actions
            assert json.loads(request.content) == {"mode": "running"}
            self.started = True
            return httpx.Response(200, json={"mode": "running"})
        if request.url.path == "/action":
            assert self.started
            step = native.StepRequest.model_validate_json(request.content)
            assert step.expected_route.game_instance_id == "B"
            assert step.expected_route.run_id == "B-run"
            self.actions.append(step)
            if len(self.actions) == 1:
                state = self.observation.state
                assert isinstance(state, native.CombatState)
                self.observation = self.observation.model_copy(
                    update={
                        "state": state.model_copy(
                            update={
                                "piles": native.Piles(
                                    hand=("card:9:3",),
                                    draw=(),
                                    discard=("card:9:1",),
                                    exhaust=(),
                                    other=(),
                                )
                            }
                        ),
                    }
                )
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )
        if request.url.path == "/inspect":
            query = native.InspectRequest.model_validate_json(request.content).query
            self.inspections.append(query)
            assert isinstance(query, native.CardQuery)
            assert query.refs == ("eval-stale:card:7:1",)
            return httpx.Response(
                200,
                content=native.InspectRejected(
                    status=native.ResultStatus.REJECTED,
                    error=native.InvalidRefs(
                        code=native.ErrorCode.INVALID_REFS,
                        message="invalid",
                        invalid_refs=("eval-stale:card:7:1",),
                    ),
                    observation=None,
                ).to_json(),
            )
        if request.url.path == "/status":
            return httpx.Response(
                200,
                content=native.Status(
                    location=self.observation.location,
                    control_intent=native.ControlIntent.RUNNING,
                    input_readiness=native.InputReadiness.READY,
                    receipt=None,
                ).to_json(),
            )
        assert request.url.path == "/state"
        return httpx.Response(
            200,
            content=native.StateCompleted(
                status=native.ResultStatus.COMPLETED,
                observation=self.observation,
            ).to_json(),
        )

    @asynccontextmanager
    async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
        self.launches += 1
        if self.uncertain:
            raise CheckpointUncertain("Synthetic uncertain load")
        async with (
            ModClient("http://B", transport=httpx.MockTransport(self.respond)) as client,
            CheckpointClient(
                "http://unused", transport=httpx.MockTransport(self.respond)
            ) as checkpoints,
        ):
            try:
                yield LoadedTarget(
                    LiveGame(client, namespace="unused", game_instance_id="B", run_id="B-run"),
                    client,
                    checkpoints,
                    IdentityRead(
                        status=native.ResultStatus.COMPLETED,
                        observation=self.observation,
                        identities=IdentityFacts(cards=(), creatures=(), map_travel=(), relics=()),
                    ),
                    {"card:1:1": "card:9:1", "card:1:2": "card:9:2", "enemy:1:1": "enemy:9:1"},
                    (),
                    LoadedCorrespondence(),
                )
            finally:
                self.closed = True


@pytest.mark.parametrize("start", [False, True])
def test_inspected_request_resumes_with_native_history_and_only_b_writes(
    tmp_path: Path, start: bool
) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=start)
        restorer = FakeRestorer(tmp_path, case)
        inputs: list[bytes] = []
        responses = iter(
            [
                reply(
                    (
                        "live-play",
                        "combat_act",
                        '{"steps":[{"kind":"play_card","card":"card:1:1","target":"enemy:1:1"}]}',
                    )
                ),
                reply(("stale", "combat_inspect", '{"area":"deck","refs":["card:7:1"]}')),
                reply(
                    (
                        "live-turn",
                        "combat_act",
                        '{"steps":[{"kind":"play_card","card":"b:card:9:3","target":"enemy:1:1"},{"kind":"end_turn"}]}',
                    )
                ),
            ]
        )

        def respond(request: httpx.Request) -> httpx.Response:
            inputs.append(request.content)
            return httpx.Response(
                200, content=next(responses), headers={"Content-Type": "text/event-stream"}
            )

        branch = tmp_path / "branch.sqlite"
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as model:
            result = await take_over(
                case,
                restorer,
                branch,
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(5, 30),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverCompleted)
        assert result.sample.decisions == 2
        assert result.sample.requests == 3
        assert restorer.closed
        assert len(restorer.actions) == 3
        first = restorer.actions[0].step
        assert isinstance(first, native.PlayCard)
        assert (first.card, first.target) == ("card:9:1", "enemy:9:1")
        second = restorer.actions[1].step
        assert isinstance(second, native.PlayCard)
        assert second.card == "card:9:3"
        assert len(restorer.inspections) == 1
        with (
            closing(Journal.open(branch)) as journal,
            closing(Journal.open(case.source.archive)) as parent,
        ):
            target = parent.request(case.source.anchor.model_sequence)
            inspections = [
                record
                for record in parent.operations(stop=target.sequence)
                if record.request.kind.value == "inspect"
            ]
            assert len(inspections) == 1
            assert inspections[0].reply is not None
            resumed = journal.request(target.sequence)
            assert resumed.input == target.input
            assert resumed.tools == target.tools
            assert journal.manifest.parent is not None
            assert journal.manifest.parent.before_sequence == target.sequence
            new_anchor = ModelAnchor(journal.manifest.recording_id, journal.requests()[-1].sequence)
            settlement = next(
                item
                for item in journal.operations(start=target.sequence)
                if item.request.kind.value == "settle"
            )
            assert settlement.reply is not None
            assert isinstance(settlement.reply.outcome, Settlement)
            assert isinstance(settlement.reply.outcome.request.step, native.PlayCard)
            assert settlement.reply.outcome.request.step.card == "card:1:1"
            assert settlement.reply.outcome.request.operation_id == restorer.actions[0].operation_id
        async with reach(branch, new_anchor) as reached:
            assert isinstance(reached, Reached)
        assert len(inputs) == 3

    asyncio.run(scenario())


def test_uncertain_native_load_preserves_incomplete_evidence_without_writes(tmp_path: Path) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        restorer = FakeRestorer(tmp_path, case, uncertain=True)
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(lambda _: pytest.fail("No live request")),
        ) as model:
            result = await take_over(
                case,
                restorer,
                tmp_path / "branch.sqlite",
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(5, 30),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverIncomplete)
        assert not restorer.actions
        assert not restorer.started
        assert result.anchor is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["prefix", "protocol", "rejected"])
def test_refusal_never_releases_live_dependencies(tmp_path: Path, failure: str) -> None:
    import sqlite3
    from dataclasses import replace

    from sts2_harness.config import Protocol
    from sts2_harness_eval.checkpoints import (
        CheckpointCause,
        CheckpointErrorCode,
        CheckpointRejected,
    )
    from sts2_harness_eval.takeover import ProtocolMismatch, TakeoverUnsupported

    class RejectedRestorer(FakeRestorer):
        @asynccontextmanager
        async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
            self.launches += 1
            raise CheckpointRejected(
                CheckpointCause(
                    code=CheckpointErrorCode.NATIVE_REJECTED,
                    message="Synthetic native refusal",
                )
            )
            yield  # pragma: no cover

    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        restorer = (RejectedRestorer if failure == "rejected" else FakeRestorer)(tmp_path, case)
        if failure == "prefix":
            with closing(Journal.open(case.source.archive)) as journal:
                changed = journal.manifest.model_copy(
                    update={"initial_notes": "Different cognition"}
                )
            with sqlite3.connect(case.source.archive) as connection:
                connection.execute("UPDATE manifest SET value=?", (changed.model_dump_json(),))
        configured = configuration(tmp_path)
        if failure == "protocol":
            configured = replace(configured, protocol=Protocol.CHAT_COMPLETIONS)
        async with Llm(
            configured,
            transport=httpx.MockTransport(lambda _: pytest.fail("No live model request")),
        ) as model:
            result = await take_over(
                case,
                restorer,
                tmp_path / "branch.sqlite",
                model,
                PackagedPrompts(),
                FileDocuments(),
                SampleBudget(5, 30),
                stop_requested=lambda: False,
            )
        assert isinstance(result, TakeoverUnsupported)
        if failure == "protocol":
            assert isinstance(result.evidence, ProtocolMismatch)
        assert not restorer.started
        assert not restorer.actions
        assert not restorer.closed
        assert restorer.launches == (1 if failure == "rejected" else 0)

    asyncio.run(scenario())


def test_cancellation_during_load_survives_restorer_cleanup_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        loading = asyncio.Event()
        blocked = asyncio.Event()

        class LoadingRestorer(FakeRestorer):
            @asynccontextmanager
            async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
                async with super().loaded(case) as loaded:
                    try:
                        loading.set()
                        await blocked.wait()
                        yield loaded
                    finally:
                        raise OSError("Synthetic load cleanup failure")

        restorer = LoadingRestorer(tmp_path, case)
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: pytest.fail("The live model was not released.")
            ),
        ) as model:
            task = asyncio.create_task(
                take_over(
                    case,
                    restorer,
                    tmp_path / "branch.sqlite",
                    model,
                    PackagedPrompts(),
                    FileDocuments(),
                    SampleBudget(5, 30),
                    stop_requested=lambda: False,
                )
            )
            await loading.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert restorer.closed
        assert not restorer.started
        assert not restorer.actions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("cancel", "failure"),
    [(0, None), (1, None), (2, None), (1, "feedback"), (1, "cleanup")],
)
def test_stop_waits_for_submitted_batch_feedback_before_closing_b(
    tmp_path: Path,
    cancel: int,
    failure: Literal["feedback", "cleanup"] | None,
) -> None:
    async def scenario() -> None:
        case = await source_case(tmp_path / "source.sqlite", start=False)
        submitted = asyncio.Event()
        stop = asyncio.Event()
        confirm = asyncio.Event()

        class PendingRestorer(FakeRestorer):
            async def pending(self, request: httpx.Request) -> httpx.Response:
                if request.url.path == "/action":
                    response = self.respond(request)
                    submitted.set()
                    await confirm.wait()
                    assert not self.closed
                    if failure == "feedback" and len(self.actions) == 2:
                        raise OSError("Synthetic feedback failure")
                    return response
                return self.respond(request)

            @asynccontextmanager
            async def loaded(self, case: Case) -> AsyncGenerator[LoadedTarget]:
                async with super().loaded(case) as loaded:
                    async with ModClient(
                        "http://B", transport=httpx.MockTransport(self.pending)
                    ) as client:
                        try:
                            yield LoadedTarget(
                                loaded.game,
                                client,
                                loaded.checkpoints,
                                loaded.read,
                                loaded.refs,
                                loaded.differences,
                                loaded.correspondence,
                            )
                        finally:
                            if failure == "cleanup":
                                raise OSError("Synthetic cleanup failure")

        restorer = PendingRestorer(tmp_path, case)
        async with Llm(
            configuration(tmp_path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=reply(
                        (
                            "batch",
                            "combat_act",
                            '{"steps":[{"kind":"end_turn"},{"kind":"end_turn"}]}',
                        )
                    ),
                    headers={"Content-Type": "text/event-stream"},
                ),
            ),
        ) as model:
            branch = tmp_path / "branch.sqlite"
            task = asyncio.create_task(
                take_over(
                    case,
                    restorer,
                    branch,
                    model,
                    PackagedPrompts(),
                    FileDocuments(),
                    SampleBudget(5, 30),
                    stop_requested=stop.is_set,
                )
            )
            await submitted.wait()
            if cancel:
                for _ in range(cancel):
                    task.cancel()
                    await asyncio.sleep(0)
            else:
                stop.set()
            await asyncio.sleep(0)
            assert not task.done()
            assert not restorer.closed
            confirm.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                from sts2_harness_eval.takeover import OperatorStopped

                result = await task
                assert isinstance(result, TakeoverIncomplete)
                assert isinstance(result.evidence, OperatorStopped)
        assert restorer.closed
        assert len(restorer.actions) == 2
        with closing(Journal.open(branch)) as journal:
            settled = [
                record
                for record in journal.operations(start=case.source.anchor.model_sequence)
                if isinstance(record.reply.outcome if record.reply else None, Settlement)
            ]
            assert len(settled) == (1 if failure == "feedback" else 2)
            assert all(record.reply is not None for record in settled)

    asyncio.run(scenario())
