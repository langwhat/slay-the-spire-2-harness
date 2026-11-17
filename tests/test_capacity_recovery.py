"""Confirmed provider rejections can only retry a genuinely smaller model request."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Never
from unittest.mock import patch

import anthropic
import httpx2 as httpx
import pytest
from openai import APIStatusError
from pydantic import JsonValue
from test_compaction_full_replay import response
from test_mod_client import COMBAT, RUN
from test_responses import configuration

from sts2_harness.capacity import CapacityClassifierConfig, RecoveryComparison
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent
from sts2_harness.config import Protocol
from sts2_harness.context import Context
from sts2_harness.context.models import CombatActInput, RunActInput
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.journal import ArchiveError, Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import AttemptCause
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import (
    AssistantMessage,
    CapacityRejected,
    FailureKind,
    ModelAttemptSink,
    ModelClient,
    ModelInput,
    ModelReply,
    ModelUnavailable,
    ModelUsageAnchor,
    PreparedRequest,
    ResponsesReply,
    ToolDefinition,
)
from sts2_harness.model_journal import RecordedLlm
from sts2_harness.note_values import NotesState, NotesView
from sts2_harness.notes import Notes
from sts2_harness.run import ActStart, RunAgent
from sts2_harness.tools import ToolCatalog
from sts2_harness_eval._replay import ReplayModel, ReplayRejected, Timeline
from sts2_harness_eval.results import ModelAnchor, Reached


class FakeClassifierLlm(Llm):
    def install_fake_classifier(self) -> None:
        original = self._adapter.complete

        async def classify(
            body: dict[str, JsonValue], *, evidence: ModelAttemptSink | None = None
        ) -> ModelReply:
            try:
                return await original(body, evidence=evidence)
            except (APIStatusError, anthropic.APIStatusError):
                raise CapacityRejected("test-confirmed", attempt=1) from None

        self._adapter.complete = classify


@pytest.mark.parametrize(
    ("protocol", "role", "mode"),
    [
        (Protocol.RESPONSES, native.DecisionOwner.RUN, "recover"),
        (Protocol.RESPONSES, native.DecisionOwner.COMBAT, "twice"),
        (Protocol.CHAT_COMPLETIONS, native.DecisionOwner.RUN, "transport"),
        (Protocol.MESSAGES, native.DecisionOwner.COMBAT, "minimal"),
    ],
)
def test_confirmed_rejection_retries_once_with_smaller_request(
    tmp_path: Path, protocol: Protocol, role: native.DecisionOwner, mode: str
) -> None:
    if mode == "invalid" and protocol is not Protocol.RESPONSES:
        pytest.skip("The incomplete tool-call fixture uses Responses native output.")

    verify_untrusted: Callable[[], Awaitable[None]] | None = None

    async def scenario() -> None:
        nonlocal verify_untrusted
        obs = native.Observation.model_validate_json(
            RUN if role is native.DecisionOwner.RUN else COMBAT
        )

        async def observe() -> native.Observation:
            return obs

        async def unused(*args: object) -> Never:
            raise AssertionError("Recovery must not perform game IO")

        tools = ToolCatalog(
            role,
            notes=Notes(NotesState(other="notes")),
            observe=observe,
            inspect=unused,
            catalog=unused,
        )

        def make_context() -> Context:
            context = Context(role)
            if mode != "minimal":
                context.begin_group(obs, NotesView(other="old"))
                context.append_task(
                    RunActInput(strategy_notes=NotesView(other="old"))
                    if role is native.DecisionOwner.RUN
                    else CombatActInput(NotesView(other="old")),
                    obs,
                )
                context.append_correction(" ".join(f"old{i}" for i in range(1000)))
                context.finish_group()
                context.begin_group(obs, NotesView(other="retained"))
                context.append_task(
                    RunActInput(strategy_notes=NotesView(other="retained"))
                    if role is native.DecisionOwner.RUN
                    else CombatActInput(NotesView(other="retained")),
                    obs,
                )
                context.finish_group()
            context.begin_group(obs, NotesView(other="active"))
            context.append_task(
                RunActInput(strategy_notes=NotesView(other="active"))
                if role is native.DecisionOwner.RUN
                else CombatActInput(NotesView(other="active")),
                obs,
            )
            if mode == "invalid":
                context.append_reply(
                    AssistantMessage(
                        ResponsesReply.from_json(
                            json.dumps(
                                [
                                    {
                                        "type": "function_call",
                                        "call_id": "pending",
                                        "name": "inspect",
                                        "arguments": "{}",
                                    }
                                ]
                            )
                        )
                    )
                )
            return context

        context = make_context()
        replay_context = make_context()
        bodies: list[bytes] = []
        journal = Journal.create(
            tmp_path / "capacity.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="capacity",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )

        def provider(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content)
            if mode == "transport" and len(bodies) == 1:
                raise httpx.ConnectError("synthetic connection failure", request=request)
            if len(bodies) == (2 if mode == "transport" else 1) or mode == "twice":
                return httpx.Response(
                    400, json={"error": {"type": "test-confirmed", "message": "test"}}
                )
            return httpx.Response(
                200,
                content=response(protocol, text="ok"),
                headers={"Content-Type": "text/event-stream"},
            )

        class Run(RunAgent):
            async def request(self, value: Context) -> None:
                await self._request(value)

        class Combat(CombatAgent):
            async def request(self, value: Context) -> None:
                await self._request(value)

        async def make_agent(model: ModelClient) -> Run | Combat:
            if role is native.DecisionOwner.RUN:
                run = Run(lambda: model, tools, ToolRecoveryBudget())
                await run.enter_act(ActStart(obs))
                return run
            return Combat(model, tools, ToolRecoveryBudget(), opening_notes=NotesView(other="old"))

        config = replace(configuration(tmp_path), protocol=protocol)

        async def no_sleep(seconds: float) -> None:
            return None

        async with FakeClassifierLlm(
            config, transport=httpx.MockTransport(provider), sleep=no_sleep
        ) as llm:
            if mode != "unrelated":
                llm.install_fake_classifier()
            model = RecordedLlm(llm, journal, role=role, stream="role:1")
            agent = await make_agent(model)
            original_prepare = llm.prepare
            prepare_calls = 0

            def prepare(
                input: ModelInput,
                tools: tuple[ToolDefinition, ...],
                *,
                usage_anchor: ModelUsageAnchor | None = None,
            ) -> PreparedRequest:
                nonlocal prepare_calls
                prepare_calls += 1
                if prepare_calls == 2:
                    raise ValueError("synthetic invalid candidate")
                return original_prepare(input, tools, usage_anchor=usage_anchor)

            fault = (
                patch.object(llm, "prepare", side_effect=prepare)
                if mode == "encoding"
                else nullcontext()
            )
            with fault:
                if mode in {"recover", "transport"}:
                    await agent.request(context)
                else:
                    with pytest.raises(ModelUnavailable) as failure:
                        await agent.request(context)
                    expected = (
                        FailureKind.CAPACITY
                        if mode in {"minimal", "invalid", "encoding", "twice"}
                        else FailureKind.CONFIGURATION
                    )
                    assert failure.value.reason is expected
            expected_requests = (
                3 if mode == "transport" else 2 if mode in {"recover", "twice"} else 1
            )
            assert len(bodies) == expected_requests
            if mode in {"recover", "twice", "transport"}:
                assert all("old0" not in str(item) for item in context.model_input())

        records = journal.requests()
        assert len(records) == len(bodies) - (1 if mode == "transport" else 0)
        rejected = records[0]
        if mode in {"recover", "twice", "transport"}:
            retried = records[1]
            assert rejected.capacity_recovery is not None
            assert rejected.capacity_recovery.retry_sequence == retried.sequence
            assert retried.attempts[0].cause is AttemptCause.CAPACITY_RETRY
            assert retried.attempts[0].recovery_of == rejected.capacity_recovery.rejected
            assert len(bodies[-1]) < len(bodies[0])
            if mode == "transport":
                assert len(rejected.attempts) == 2
                assert rejected.capacity_recovery.rejected.attempt == 2
        elif mode in {"minimal", "invalid", "encoding"}:
            assert rejected.capacity_recovery is not None
            assert rejected.capacity_recovery.retry_sequence is None
            if mode in {"invalid", "encoding"}:
                assert rejected.capacity_recovery.comparison is RecoveryComparison.INVALID
                assert rejected.capacity_recovery.candidate_input is None
        else:
            assert rejected.capacity_recovery is None
        reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
        timeline = Timeline(records)
        replay = ReplayModel(timeline, role, "role:1", ModelAnchor("capacity", 999), reached)
        replay_agent = await make_agent(replay)
        if mode in {"recover", "transport"}:
            await replay_agent.request(replay_context)
        elif mode == "encoding":
            with pytest.raises(ReplayRejected):
                await replay_agent.request(replay_context)
        else:
            with pytest.raises(ModelUnavailable):
                await replay_agent.request(replay_context)
        assert timeline.index == len(records)
        assert replay_context.model_input() == context.model_input()
        await replay.close()
        if (
            mode == "recover"
            and role is native.DecisionOwner.RUN
            and protocol is Protocol.RESPONSES
        ):
            recovery = rejected.capacity_recovery
            assert recovery is not None and recovery.candidate_estimate is not None
            broken_records = (
                (
                    rejected.model_copy(
                        update={
                            "capacity_recovery": recovery.model_copy(
                                update={"candidate_estimate": recovery.candidate_estimate + 1}
                            )
                        }
                    ),
                    records[1],
                ),
                (
                    rejected.model_copy(
                        update={
                            "capacity_recovery": recovery.model_copy(
                                update={"retry_sequence": None}
                            )
                        }
                    ),
                    records[1],
                ),
                (
                    rejected,
                    records[1].model_copy(
                        update={
                            "confirmed_operation_cursor": rejected.confirmed_operation_cursor + 1
                        }
                    ),
                ),
                (
                    rejected,
                    records[1].model_copy(
                        update={
                            "attempts": (
                                records[1].attempts[0].model_copy(update={"recovery_of": None}),
                            )
                        }
                    ),
                ),
                (
                    rejected,
                    records[1].model_copy(
                        update={
                            "attempts": (records[1].attempts[0].model_copy(update={"index": 7}),)
                        }
                    ),
                ),
            )
            for broken in broken_records:
                broken_replay = ReplayModel(
                    Timeline(broken), role, "role:1", ModelAnchor("capacity", 999), reached
                )
                broken_agent = await make_agent(broken_replay)
                with pytest.raises(ReplayRejected):
                    await broken_agent.request(make_context())
                await broken_replay.close()

            async def reject_unverified_record() -> None:
                replay_reached: asyncio.Future[Reached] = asyncio.get_running_loop().create_future()
                unsupported = ReplayModel(
                    Timeline(records), role, "role:1", ModelAnchor("capacity", 999), replay_reached
                )
                unsupported_agent = await make_agent(unsupported)
                with pytest.raises(ReplayRejected):
                    await unsupported_agent.request(make_context())
                await unsupported.close()

            verify_untrusted = reject_unverified_record
            journal.model_attempt(
                records[1].sequence, records[1].attempts[0].model_copy(update={"index": 7})
            )
            with pytest.raises(ArchiveError):
                journal.requests()
        journal.close()

    # Only the test boundary claims its synthetic signal is verified.
    classifier = (
        nullcontext()
        if mode in {"unverified", "unrelated"}
        else patch.object(CapacityClassifierConfig, "supports", return_value=True)
    )
    with classifier:
        asyncio.run(scenario())
    if verify_untrusted is not None:
        asyncio.run(verify_untrusted())


@pytest.mark.parametrize("protocol", tuple(Protocol))
def test_recovery_replays_campaign_without_repeating_game_actions_or_notes(
    tmp_path: Path, protocol: Protocol
) -> None:
    from contextlib import closing

    from test_compaction_full_replay import campaign_with_history
    from test_replay import record_campaign

    from sts2_harness.config import ModelConfiguration
    from sts2_harness_eval import Reached as ReachedResult
    from sts2_harness_eval import reach

    async def scenario() -> None:
        campaign = campaign_with_history(protocol)
        live_model = campaign.model
        rejected = False

        def provider(request: httpx.Request) -> httpx.Response:
            nonlocal rejected
            if len(campaign.inputs) == 8 and not rejected:
                rejected = True
                return httpx.Response(
                    400, json={"error": {"type": "test-confirmed", "message": "test"}}
                )
            return live_model(request)

        def fake_llm(
            config: ModelConfiguration, *, transport: httpx.AsyncBaseTransport | None = None
        ) -> Llm:
            llm = FakeClassifierLlm(config, transport=transport)
            llm.install_fake_classifier()
            return llm

        path = tmp_path / "campaign.sqlite"
        with (
            patch.object(campaign, "model", provider),
            patch("test_replay.Llm", side_effect=fake_llm),
            patch(
                "test_replay.configuration",
                return_value=replace(configuration(tmp_path), protocol=protocol),
            ),
        ):
            anchors = await record_campaign(path, campaign)
        assert rejected
        with closing(Journal.open(path)) as archive:
            records = archive.requests()
            operations = archive.operations()
        source = next(record for record in records if record.capacity_recovery is not None)
        assert source.capacity_recovery is not None
        assert source.capacity_recovery.retry_sequence is not None
        retry = next(
            record
            for record in records
            if record.sequence == source.capacity_recovery.retry_sequence
        )
        retained = "".join(message.model_dump_json() for message in retry.input)
        assert "Updated once." in retained
        assert len(campaign.actions) == 5
        actions = tuple(campaign.actions)
        requests = tuple(campaign.inputs)
        original = path.read_bytes()
        for anchor in anchors:
            if anchor.model_sequence <= retry.sequence:
                continue
            async with reach(path, anchor) as result:
                assert isinstance(result, ReachedResult), result
        assert tuple(campaign.actions) == actions
        assert tuple(campaign.inputs) == requests
        assert path.read_bytes() == original
        with closing(Journal.open(path)) as archive:
            assert archive.operations() == operations

    with patch.object(CapacityClassifierConfig, "supports", return_value=True):
        asyncio.run(scenario())
