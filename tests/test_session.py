import asyncio
from dataclasses import replace
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign, terminal
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame, RunEndReason
from sts2_harness.journal import Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.model_purpose import ModelPurpose
from sts2_harness.note_values import NotesState
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import RunSession, SessionDependencies


def test_live_cache_keys_follow_recording_and_role_across_contexts(tmp_path: Path) -> None:
    async def scenario() -> None:
        for recording in ("first", "second"):
            campaign = Campaign()
            async with live_session(
                ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
                Manifest(
                    materials=SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    recording_id=recording,
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                    initial_notes=NotesState(),
                ),
                tmp_path / f"{recording}.sqlite",
                model=Llm(
                    replace(
                        configuration(tmp_path),
                        reasoning_effort_run="max",
                        reasoning_effort_combat="high",
                    ),
                    transport=httpx.MockTransport(campaign.model),
                ),
            ) as session:
                assert (await session.run()).reason is RunEndReason.TERMINAL
            assert [body.get("prompt_cache_key") for body in campaign.inputs] == [
                f"{recording}:run",
                f"{recording}:run",
                f"{recording}:combat",
                f"{recording}:combat",  # The battle review stays in the combat context.
                f"{recording}:run",
                f"{recording}:run",
                f"{recording}:run",
                f"{recording}:run",
                f"{recording}:combat",
            ]
            assert [body["reasoning"] for body in campaign.inputs] == [
                {"effort": effort}
                for effort in ("max", "max", "high", "high", "max", "max", "max", "max", "high")
            ]
            assert all("prompt_cache_retention" not in body for body in campaign.inputs)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["terminal", "run", "instance"])
def test_session_preserves_notes_when_query_observes_ended_lifetime(
    tmp_path: Path, change: str
) -> None:
    campaign = Campaign()
    arguments = '{"mode":"write","other":"Changed."}'
    campaign.responses = iter([reply(("notes", "run_notes", arguments))])

    def provider(request: httpx.Request) -> httpx.Response:
        opening = campaign.current
        campaign.states[0] = (
            terminal(opening)
            if change == "terminal"
            else opening.model_copy(
                update={
                    "location": opening.location.model_copy(
                        update={"run_id": "other"}
                        if change == "run"
                        else {"game_instance_id": "other"}
                    ),
                    "stamp": opening.stamp.model_copy(
                        update={"game_instance_id": "other"} if change == "instance" else {}
                    ),
                }
            )
        )
        return campaign.model(request)

    async def scenario() -> None:
        async with (
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)) as client,
            Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)) as model,
        ):
            game = LiveGame(client, namespace="notes", game_instance_id="game", run_id="run")
            dependencies = SessionDependencies(
                game, PackagedPrompts(), FileDocuments(), lambda role, stream: model
            )
            async with RunSession(
                Manifest(
                    materials=SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    recording_id="notes",
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                    initial_notes=NotesState(other="Original."),
                ),
                dependencies,
            ) as session:
                ended = await session.run()
                assert ended.reason is (
                    RunEndReason.TERMINAL if change == "terminal" else RunEndReason.RUN_CHANGED
                )
                assert session.notes.read().other == "Original."
        assert len(campaign.inputs) == 1
        assert campaign.actions == []

    asyncio.run(scenario())


def test_session_runs_injected_dependencies_without_closing_them(tmp_path: Path) -> None:
    campaign = Campaign()
    streams: list[tuple[ModelPurpose, str]] = []

    game_transport = ClosingTransport(campaign.mod)
    model_transport = ClosingTransport(campaign.model)

    async def scenario() -> None:
        async with (
            ModClient("http://mod", transport=game_transport) as client,
            Llm(configuration(tmp_path), transport=model_transport) as model,
        ):
            game = LiveGame(
                client,
                namespace="injected",
                game_instance_id="game",
                run_id="run",
                wait_for_start=True,
            )

            def factory(role: ModelPurpose, stream: str) -> ModelClient:
                streams.append((role, stream))
                return model

            dependencies = SessionDependencies(game, PackagedPrompts(), FileDocuments(), factory)
            async with RunSession(
                Manifest(
                    materials=SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    recording_id="injected",
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                    initial_notes=NotesState(other="Opening plan."),
                ),
                dependencies,
            ) as session:
                ended = await session.run()
                assert ended.reason is RunEndReason.TERMINAL
                assert session.notes.read().other == "Reserve for act two."
            observed = await game.observe()
            assert observed.observations[-1].state.phase is native.Phase.TERMINAL
            assert not game_transport.closed and not model_transport.closed

    asyncio.run(scenario())
    assert game_transport.closed and model_transport.closed
    assert streams == [
        (ModelPurpose.RUN, "run:1"),
        (ModelPurpose.COMBAT, "combat:1"),
        (ModelPurpose.RUN, "run:2"),
        (ModelPurpose.COMBAT, "combat:2"),
    ]
    assert len(campaign.actions) == 5


class ClosingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


def test_live_resources_close_when_session_entry_fails(tmp_path: Path) -> None:
    import pytest

    from sts2_harness.execution import Execution, ToolRecoveryBudget
    from sts2_harness.journal import Journal
    from sts2_harness.live_session import live_session
    from sts2_harness.notes import Notes
    from sts2_harness.run import RunRole

    campaign = Campaign()
    game_transport = ClosingTransport(campaign.mod)
    model_transport = ClosingTransport(campaign.model)

    def fail(execution: Execution, notes: Notes, recovery: ToolRecoveryBudget) -> RunRole:
        raise RuntimeError("Synthetic role initialization failure.")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="Synthetic role initialization failure"):
            async with live_session(
                ModClient("http://mod", transport=game_transport),
                Manifest(
                    materials=SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    recording_id="entry-failed",
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                    initial_notes="",
                ),
                tmp_path / "archive.sqlite",
                model=Llm(configuration(tmp_path), transport=model_transport),
                run_factory=fail,
            ):
                pytest.fail("The session must not enter.")
        assert game_transport.closed and model_transport.closed
        archive = Journal.open(tmp_path / "archive.sqlite")
        assert archive.manifest.recording_id == "entry-failed"
        assert archive.operations() == ()
        archive.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("first_room", ["monster", "elite"])
def test_every_combat_uses_combat_effort_and_is_recorded(tmp_path: Path, first_room: str) -> None:
    import json
    from contextlib import closing

    from sts2_harness.journal import Journal
    from sts2_harness.live_session import live_session

    campaign = Campaign()
    for index, room in ((1, first_room), (4, "boss")):
        observation = campaign.states[index]
        campaign.states[index] = observation.model_copy(
            update={
                "state": observation.state.model_copy(
                    update={
                        "progress": observation.state.progress.model_copy(
                            update={"room_type": room}
                        )
                    }
                )
            }
        )

    def model(request: httpx.Request) -> httpx.Response:
        content = campaign.model(request).content
        event = json.loads(content.decode().removeprefix("data: "))
        event["response"]["usage"] = {
            "input_tokens": 100,
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 13},
        }
        return httpx.Response(
            200,
            content=("data: " + json.dumps(event) + "\n\n").encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    async def scenario() -> None:
        archive = tmp_path / "effort.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(campaign.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="effort",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
            ),
            archive,
            model=Llm(
                configuration(
                    tmp_path,
                    LLM_REASONING_EFFORT="minimal",
                    LLM_REASONING_EFFORT_RUN="low",
                    LLM_REASONING_EFFORT_COMBAT="high",
                ),
                transport=httpx.MockTransport(model),
            ),
        ) as session:
            await session.run()
        with closing(Journal.open(archive)) as journal:
            records = journal.requests()
            assert {record.stream for record in records} >= {"combat:1", "combat:2"}
            for record in records:
                effort = "high" if record.stream.startswith("combat:") else "low"
                response = record.attempts[-1].reply
                assert response is not None
                assert response.requested_effort == effort
                assert response.reasoning_tokens == 13 and response.output_tokens == 20
            reasoning = [body["reasoning"] for body in campaign.inputs]
            assert all(isinstance(item, dict) for item in reasoning)
            assert [item["effort"] for item in reasoning if isinstance(item, dict)] == [
                record.attempts[-1].reply.requested_effort
                for record in records
                if record.attempts[-1].reply is not None
            ]

    asyncio.run(scenario())
