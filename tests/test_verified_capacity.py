"""Endpoint verification admits only the observed structured HTTP rejection."""

import asyncio
from contextlib import closing
from pathlib import Path
from typing import Never

import httpx2 as httpx
import pytest
from test_compaction_full_replay import response
from test_mod_client import RUN
from test_responses import configuration

from sts2_harness.capacity import CapacityEndpointCategory
from sts2_harness.client import models as native
from sts2_harness.config import Protocol
from sts2_harness.context import Context
from sts2_harness.context.models import RunActInput
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    CapacityRejected,
    FailureKind,
    ModelUnavailable,
    TextMessage,
    TextRole,
)
from sts2_harness.model_journal import RecordedLlm
from sts2_harness.notes import Notes
from sts2_harness.run import ActStart, RunAgent
from sts2_harness.tools import ToolCatalog
from sts2_harness_eval._replay import ReplayModel, Timeline
from sts2_harness_eval.results import ModelAnchor, Reached


def test_verified_responses_http_rejection_exposes_capacity_signal(tmp_path: Path) -> None:
    config = configuration(
        tmp_path,
        LLM_BASE_URL="https://provider.invalid/v1",
        LLM_CAPACITY_VERIFIED_ENDPOINT="https://provider.invalid/v1",
    )

    async def scenario() -> None:
        async with Llm(
            config,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    400,
                    json={"error": {"code": "context_too_large", "type": "invalid_request_error"}},
                )
            ),
        ) as model:
            with pytest.raises(CapacityRejected) as failure:
                await model.complete((TextMessage(TextRole.USER, "Synthetic input"),), ())
            assert failure.value.signal == "context_too_large"
            assert failure.value.attempt == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "selector", ("", "https://other.invalid/v1", "https://provider.invalid/v1/")
)
def test_default_or_changed_endpoint_does_not_confirm_capacity(
    tmp_path: Path, selector: str
) -> None:
    config = configuration(
        tmp_path,
        LLM_BASE_URL="https://provider.invalid/v1",
        LLM_CAPACITY_VERIFIED_ENDPOINT=selector,
    )

    async def scenario() -> None:
        async with Llm(
            config,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(400, json={"error": {"code": "context_too_large"}})
            ),
        ) as model:
            assert (
                model.capacity_policy.capacity_classifier_config.endpoint_category
                is CapacityEndpointCategory.UNVERIFIED
            )
            with pytest.raises(ModelUnavailable) as failure:
                await model.complete((), ())
            assert not isinstance(failure.value, CapacityRejected)
            assert failure.value.reason is FailureKind.CONFIGURATION

    asyncio.run(scenario())


def test_verified_recovery_reopens_and_replays_with_its_recorded_policy(tmp_path: Path) -> None:
    config = configuration(
        tmp_path, LLM_CAPACITY_VERIFIED_ENDPOINT="https://example.invalid/proxy/v1"
    )
    observation = native.Observation.model_validate_json(RUN)
    role = native.DecisionOwner.RUN

    def context() -> Context:
        value = Context(role)
        for label in ("old", "retained", "active"):
            value.begin_group(observation, label)
            value.append_task(RunActInput(strategy_notes=label), observation)
            if label == "old":
                value.append_correction(" ".join(f"old{i}" for i in range(1000)))
            if label != "active":
                value.finish_group()
        return value

    async def observe() -> native.Observation:
        return observation

    async def unused(*args: object) -> Never:
        raise AssertionError("Capacity recovery must not perform game IO")

    tools = ToolCatalog(role, notes=Notes("notes"), observe=observe, inspect=unused, catalog=unused)

    class Run(RunAgent):
        async def request(self, value: Context) -> None:
            await self._request(value)

    bodies: list[bytes] = []

    def provider(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) == 1:
            return httpx.Response(
                400, json={"error": {"code": "context_too_large", "type": "invalid_request_error"}}
            )
        return httpx.Response(
            200,
            content=response(Protocol.RESPONSES, text="ok"),
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> None:
        original = context()
        async with Llm(config, transport=httpx.MockTransport(provider)) as live:
            journal = Journal.create(
                tmp_path / "verified.sqlite",
                Manifest(
                    recording_id="verified",
                    core_version="test",
                    game_instance_id="game",
                    run_id="run",
                    materials=SessionMaterials(
                        entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                    ),
                    capacity_policy=live.capacity_policy,
                ),
            )
            recorded = RecordedLlm(live, journal, role=role, stream="run:1")
            agent = Run(lambda: recorded, tools, ToolRecoveryBudget())
            await agent.enter_act(ActStart(observation))
            await agent.request(original)
            journal.close()
        assert len(bodies) == 2 and len(bodies[1]) < len(bodies[0])
        with closing(Journal.open(journal.path)) as archive:
            records = archive.requests()
            assert records[0].capacity_recovery is not None
            assert records[0].attempts[0].capacity_signal == "context_too_large"
            assert records[0].configuration.capacity_policy == archive.manifest.capacity_policy
            assert archive.manifest.capacity_policy.model_dump(mode="json")[
                "capacity_classifier_config"
            ] == {
                "protocols": ["responses"],
                "endpoint_category": "verified_responses",
                "enabled_signals": ["context_too_large"],
            }
            assert config.endpoint not in archive.manifest.model_dump_json()
            assert config.endpoint not in records[0].configuration.model_dump_json()
            reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
            timeline = Timeline(records)
            replay = ReplayModel(timeline, role, "run:1", ModelAnchor("verified", 999), reached)
            replay_agent = Run(lambda: replay, tools, ToolRecoveryBudget())
            await replay_agent.enter_act(ActStart(observation))
            replay_context = context()
            await replay_agent.request(replay_context)
            assert replay_context.model_input() == original.model_input()
            assert timeline.index == len(records)
            await replay.close()

    asyncio.run(scenario())
