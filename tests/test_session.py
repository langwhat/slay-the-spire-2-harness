import asyncio
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
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ModelClient
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.session import RunSession, SessionDependencies


@pytest.mark.parametrize("change", ["terminal", "run", "instance"])
@pytest.mark.parametrize("mode", ["replace", "edit"])
def test_session_preserves_notes_when_query_observes_ended_lifetime(
    tmp_path: Path, change: str, mode: str
) -> None:
    campaign = Campaign()
    arguments = (
        '{"mode":"replace","text":"Changed."}'
        if mode == "replace"
        else '{"mode":"edit","old_text":"Original.","new_text":"Changed."}'
    )
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
                    initial_notes="Original.",
                ),
                dependencies,
            ) as session:
                ended = await session.run()
                assert ended.reason is (
                    RunEndReason.TERMINAL if change == "terminal" else RunEndReason.RUN_CHANGED
                )
                assert session.notes.read() == "Original."
        assert len(campaign.inputs) == 1
        assert campaign.actions == []

    asyncio.run(scenario())


def test_session_runs_injected_dependencies_without_closing_them(tmp_path: Path) -> None:
    campaign = Campaign()
    streams: list[tuple[native.DecisionOwner, str]] = []

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

            def factory(role: native.DecisionOwner, stream: str) -> ModelClient:
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
                    initial_notes="Opening plan.",
                ),
                dependencies,
            ) as session:
                ended = await session.run()
                assert ended.reason is RunEndReason.TERMINAL
                assert session.notes.read() == "Reserve for act two."
            observed = await game.observe()
            assert observed.observations[-1].state.phase is native.Phase.TERMINAL
            assert not game_transport.closed and not model_transport.closed

    asyncio.run(scenario())
    assert game_transport.closed and model_transport.closed
    assert streams == [
        (native.DecisionOwner.RUN, "run:1"),
        (native.DecisionOwner.COMBAT, "combat:1"),
        (native.DecisionOwner.RUN, "run:2"),
        (native.DecisionOwner.COMBAT, "combat:2"),
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
