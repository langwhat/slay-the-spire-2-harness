import asyncio
from hashlib import sha256
from pathlib import Path
from typing import Literal

import httpx2 as httpx
import pytest
from test_combat import reply
from test_laboratory import RECIPE
from test_lifecycle import Campaign, battle_state, run_state
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import Settlement
from sts2_harness.journal import Journal, SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.materials import FileDocuments
from sts2_harness.prompts import PackagedPrompts
from sts2_harness_eval.cases import TargetBattle
from sts2_harness_eval.laboratory import LaboratoryClient, LaboratoryOutcome
from sts2_harness_eval.laboratory_sample import (
    LaboratoryResult,
    LaboratorySource,
    run_laboratory_sample,
)
from sts2_harness_eval.sample import Completed, SampleBudget
from sts2_harness_eval.takeover import OperatorStopped

MATERIALS = SessionMaterials(entry_documents_enabled=False, act_guidance={}, battle_guidance=())


@pytest.mark.parametrize("status", (LaboratoryOutcome.REJECTED, LaboratoryOutcome.UNKNOWN))
def test_unconfirmed_construction_preserves_source_without_start_or_model(
    tmp_path: Path, status: LaboratoryOutcome
) -> None:
    calls: list[str] = []

    def native(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/operator/eval/scenario"
        return httpx.Response(
            200,
            json={
                "status": status.value,
                "error": {"code": "native_incomplete", "message": "Not confirmed."},
                "prepared_observation": None,
                "observation": None,
            },
        )

    def forbidden_model(request: httpx.Request) -> httpx.Response:
        pytest.fail("An unconfirmed construction must not call the model.")

    directory = tmp_path / "sample"

    async def scenario() -> LaboratoryResult:
        async with (
            ModClient("http://mod", transport=httpx.MockTransport(native)) as client,
            LaboratoryClient("http://mod", transport=httpx.MockTransport(native)) as laboratory,
            Llm(configuration(tmp_path), transport=httpx.MockTransport(forbidden_model)) as model,
        ):
            return await run_laboratory_sample(
                client,
                laboratory,
                RECIPE,
                directory,
                model,
                PackagedPrompts(),
                FileDocuments(()),
                MATERIALS,
                "",
                SampleBudget(10, 30),
                TargetBattle(),
                stop_requested=lambda: False,
            )

    result = asyncio.run(scenario())
    source = LaboratorySource.model_validate_json((directory / "source.json").read_bytes())
    assert source.construction.status is status
    assert source.start is None and source.opening is None
    assert calls == ["/operator/eval/scenario"]
    assert result.archive is None and result.sample is None
    assert result.source_sha256 == sha256((directory / "source.json").read_bytes()).hexdigest()
    assert LaboratoryResult.model_validate_json((directory / "result.json").read_bytes()) == result
    assert not (directory / "journal.sqlite").exists()


@pytest.mark.parametrize(
    ("cancel", "failure"),
    [(0, None), (1, None), (2, None), (2, "feedback"), (2, "cleanup")],
)
def test_stop_or_cancellation_waits_for_the_whole_model_batch_and_retains_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: int,
    failure: Literal["feedback", "cleanup"] | None,
) -> None:
    game = LaboratoryGame()
    game.states.insert(1, game.states[0])
    game.responses = iter(
        [reply(("batch", "combat_act", '{"steps":[{"kind":"end_turn"},{"kind":"end_turn"}]}'))]
    )
    directory = tmp_path / "sample"
    close = Journal.close

    def close_recording(journal: Journal) -> None:
        close(journal)
        if failure == "cleanup":
            raise OSError("Synthetic cleanup failure")

    monkeypatch.setattr(Journal, "close", close_recording)

    def native_reply(request: httpx.Request) -> httpx.Response:
        response = game.mod(request)
        if failure == "feedback" and request.url.path == "/action" and len(game.actions) == 2:
            raise OSError("Synthetic feedback failure")
        return response

    async def scenario() -> None:
        requested = asyncio.Event()
        release = asyncio.Event()
        stopped = False

        async def model_reply(request: httpx.Request) -> httpx.Response:
            requested.set()
            await release.wait()
            return game.model(request)

        async with (
            ModClient("http://mod", transport=httpx.MockTransport(native_reply)) as client,
            LaboratoryClient("http://mod", transport=httpx.MockTransport(game.mod)) as laboratory,
            Llm(configuration(tmp_path), transport=httpx.MockTransport(model_reply)) as model,
        ):
            task = asyncio.create_task(
                run_laboratory_sample(
                    client,
                    laboratory,
                    RECIPE,
                    directory,
                    model,
                    PackagedPrompts(),
                    FileDocuments(),
                    MATERIALS,
                    "",
                    SampleBudget(10, 30),
                    TargetBattle(),
                    stop_requested=lambda: stopped,
                )
            )
            await asyncio.wait_for(requested.wait(), 5)
            if cancel:
                for _ in range(cancel):
                    task.cancel()
                    await asyncio.sleep(0)
            else:
                stopped = True
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert isinstance(result.sample, (Completed, OperatorStopped))
            assert isinstance(await client.status(), native.Status)

    asyncio.run(scenario())
    assert len(game.actions) == 2
    result = LaboratoryResult.model_validate_json((directory / "result.json").read_bytes())
    assert result.archive is not None
    assert result.archive.sha256 == sha256(result.archive.path.read_bytes()).hexdigest()
    assert game.calls.count("/operator/eval/scenario") == 1
    journal = Journal.open(result.archive.path)
    try:
        settled = [
            record
            for record in journal.operations()
            if isinstance(record.reply.outcome if record.reply else None, Settlement)
        ]
        assert len(settled) == (1 if failure == "feedback" else 2)
        assert len(journal.requests()) == 1
    finally:
        close(journal)


class LaboratoryGame(Campaign):
    def __init__(self) -> None:
        super().__init__()
        opening = battle_state(1, "laboratory")
        self.states = [
            opening.model_copy(
                update={
                    "input_readiness": native.InputReadiness.READY,
                    "state": opening.state.model_copy(
                        update={"screen": native.CombatScreen.COMBAT, "selection": None}
                    ),
                }
            ),
            run_state(1, "reward"),
        ]
        self.calls: list[str] = []
        self.responses = iter([reply(("end", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))])

    def mod(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/operator/eval/scenario":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "error": None,
                    "prepared_observation": run_state(1, "map").model_dump(mode="json"),
                    "observation": self.current.model_dump(mode="json"),
                },
            )
        if request.url.path == "/control":
            return httpx.Response(200, content=request.content)
        return super().mod(request)


def test_sample_starts_in_combat_and_closes_an_independently_readable_recording(
    tmp_path: Path,
) -> None:
    game = LaboratoryGame()
    directory = tmp_path / "sample"

    async def scenario() -> LaboratoryResult:
        async with (
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)) as client,
            LaboratoryClient("http://mod", transport=httpx.MockTransport(game.mod)) as laboratory,
            Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)) as model,
        ):
            result = await run_laboratory_sample(
                client,
                laboratory,
                RECIPE,
                directory,
                model,
                PackagedPrompts(),
                FileDocuments(),
                MATERIALS,
                "Keep the potion.",
                SampleBudget(10, 30),
                TargetBattle(),
                stop_requested=lambda: False,
            )
            assert isinstance(await client.status(), native.Status)
            return result

    result = asyncio.run(scenario())
    assert isinstance(result.sample, Completed)
    assert result.sample.decisions == 1 and result.sample.requests == 1
    assert game.calls.count("/operator/eval/scenario") == 1
    assert game.calls.count("/control") == 1
    assert len(game.actions) == 1
    assert result.archive is not None
    assert result.archive.sha256 == sha256(result.archive.path.read_bytes()).hexdigest()
    journal = Journal.open(result.archive.path)
    try:
        assert journal.manifest.recording_id == result.archive.recording_id
        assert journal.manifest.initial_notes == "Keep the potion."
        assert journal.manifest.materials == MATERIALS
        assert journal.manifest.parent is None and journal.manifest.variant is None
        assert journal.manifest.headless is None
        requests = journal.requests()
        assert len(requests) == 1 and requests[0].role is native.DecisionOwner.COMBAT
    finally:
        journal.close()
    source = LaboratorySource.model_validate_json((directory / "source.json").read_bytes())
    assert isinstance(source.start, native.ControlReply)
    assert source.opening == game.states[0]
    assert result.source_sha256 == sha256((directory / "source.json").read_bytes()).hexdigest()
