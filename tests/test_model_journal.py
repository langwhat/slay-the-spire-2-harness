import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from test_combat import BattleHTTP, reply
from test_responses import OUTPUT, completed, configuration

from sts2_harness.capacity import measure
from sts2_harness.cli import main
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent, CombatTask
from sts2_harness.decisions import CombatHandoff
from sts2_harness.execution import Execution
from sts2_harness.game import LiveGame
from sts2_harness.journal import Journal, Manifest, Record, RecordedGame, SessionMaterials
from sts2_harness.journal_models import (
    AssistantSnapshot,
    DocumentData,
    DocumentLookupResult,
    DocumentReadResult,
    GuidanceText,
    MaterialKind,
    ModelRecord,
    PromptReadResult,
    ResultSnapshot,
    TextSnapshot,
)
from sts2_harness.knowledge import DocumentView, SubjectId
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import FailureKind, ModelUnavailable, TextMessage, TextRole
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.note_values import NotesView
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.results import ActionUnknown
from sts2_harness.tools import ToolCatalog


def test_combat_requests_keep_retry_query_and_native_continuation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        battle = BattleHTTP(
            [
                reply(("read", "combat_skill", '{"mode":"read","id":"guide"}')),
                reply(("reread", "combat_skill", '{"mode":"read","id":"guide"}')),
                reply(("act", "combat_act", '{"steps":[{"kind":"end_turn"},{"kind":"end_turn"}]}')),
                reply(("resume", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
                reply(text="Review complete."),
            ],
            steps_to_end=2,
        )
        battle.unknown_first = True
        guide = tmp_path / "guide.txt"
        guide.write_text("Recorded guidance.", encoding="utf-8")
        notes = ""
        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="recording",
                core_version="test",
                game_instance_id=battle.opening.location.game_instance_id,
                run_id="run",
                initial_notes=notes,
            ),
        )
        subject = SubjectId(category="monster", definition_id="SYNTHETIC")
        documents = RecordedDocuments(
            FileDocuments(
                (
                    DocumentSource(
                        id="guide",
                        name="Guide",
                        description="Battle guide",
                        path=guide,
                        subjects=(subject,),
                    ),
                )
            ),
            journal,
        )

        async def materials(_: native.Observation) -> tuple[DocumentView, ...]:
            return tuple(document.view() for document in await documents.lookup((subject,)))

        physical = 0
        encoding_digests: list[str] = []

        def provider(request: httpx.Request) -> httpx.Response:
            nonlocal physical
            physical += 1
            encoding_digests.append(
                hashlib.sha256(
                    json.dumps(
                        json.loads(request.content),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            )
            if physical == 1:
                raise httpx.ConnectError("Synthetic connection failure.")
            return battle.model(request)

        async def sleep(_: float) -> None:
            pass

        async with ModClient("http://mod", transport=httpx.MockTransport(battle.mod)) as client:
            live = LiveGame(
                client,
                namespace="recording",
                audit=journal,
                wait=battle.wait,
                game_instance_id=battle.opening.location.game_instance_id,
                run_id="run",
            )
            execution = Execution(
                RecordedGame(live, journal),
                game_instance_id=battle.opening.location.game_instance_id,
                run_id="run",
                recovery=battle.budget,
            )
            tools = ToolCatalog(
                native.DecisionOwner.COMBAT,
                observe=execution.observe,
                inspect=execution.inspect,
                catalog=execution.catalog,
                skills=documents,
            )
            async with Llm(
                replace(configuration(tmp_path), extra_retries=1),
                transport=httpx.MockTransport(provider),
                sleep=sleep,
            ) as llm:
                agent = CombatAgent(
                    RecordedLlm(llm, journal, role=native.DecisionOwner.COMBAT, stream="combat:1"),
                    tools,
                    battle.budget,
                    opening_notes=NotesView(other=notes),
                    materials=materials,
                    prompts=RecordedPrompts(PackagedPrompts(), journal),
                )
                running = asyncio.create_task(
                    CombatTask(execution, agent, battle.opening, battle.budget).run()
                )
                await battle.waiting.wait()
                assert len(battle.actions) == 1 and physical == 4
                battle.start()
                result = await running
        assert isinstance(result, CombatHandoff) and result.review == "Review complete."
        journal.close()
        guide.write_text("Future guidance.", encoding="utf-8")
        archive = Journal.open(journal.path)
        requests = archive.requests()
        assert len(requests) == 5
        first, second, reread, resumed, third = requests
        assert first.confirmed_operation_cursor == second.confirmed_operation_cursor == 0
        assert third.confirmed_operation_cursor == 1
        assert len(first.attempts) == 2 and first.attempts[0].failure is not None
        assert first.attempts[1].reply is not None and first.delivered
        assert first.attempts[0].protocol_encoding_digest == encoding_digests[0]
        assert first.attempts[1].protocol_encoding_digest == encoding_digests[1]
        assert first.semantic_digest != encoding_digests[0]
        assert archive.storage.document_blocks == 2
        assert archive.storage.native_blocks == 5
        assert second.base_sequence == first.sequence
        assert second.prefix_length == len(first.input)
        assert len(second.input) == len(first.input) + 3
        result = second.input[-2]
        assert isinstance(result, ResultSnapshot) and isinstance(result.data, DocumentData)
        assert result.data.document.text.body == "Recorded guidance."
        assert isinstance(second.input[-3], AssistantSnapshot)
        assert second.input[-3].native_output == first.attempts[1].reply.native_output
        assert isinstance(reread.input[-2], ResultSnapshot)
        material_reads = archive.materials()
        prompt_reads = [item for item in material_reads if item.request.kind is MaterialKind.PROMPT]
        explicit_reads = [item for item in material_reads if item.request.kind is MaterialKind.READ]
        lookups = [item for item in material_reads if item.request.kind is MaterialKind.LOOKUP]
        assert len(explicit_reads) == 2 and len(lookups) == 5
        assert all(isinstance(item.reply, DocumentLookupResult) for item in lookups)
        guidance = first.input[1]
        assert isinstance(guidance, TextSnapshot) and isinstance(guidance.value, GuidanceText)
        assert guidance.value.documents[0] == result.data.document
        assert all(isinstance(item.reply, PromptReadResult) for item in prompt_reads)
        assert isinstance(explicit_reads[0].reply, DocumentReadResult)
        assert explicit_reads[0].reply.document == result.data.document
        ordered = archive.timeline()
        assert [item.sequence for item in ordered] == list(range(1, ordered[-1].sequence + 1))
        assert len([item for item in ordered if isinstance(item, ModelRecord)]) == 5
        settlement = [
            item for item in ordered if isinstance(item, Record) and item.operation_id is not None
        ]
        assert len(settlement) == 2
        assert reread.sequence < settlement[0].sequence < resumed.sequence
        assert resumed.sequence < settlement[1].sequence < third.sequence
        assert resumed.confirmed_operation_cursor == 0
        unknown = resumed.input[-2]
        assert isinstance(unknown, ResultSnapshot) and isinstance(unknown.outcome, ActionUnknown)
        assert unknown.outcome.confirmed_steps == 0 and unknown.outcome.uncertain_step == 1
        assert battle.actions[0].operation_id != battle.actions[1].operation_id
        assert reread.suffix_length == second.suffix_length == 3
        assert archive.manifest.initial_notes == ""
        assert archive.manifest.model_requests_supported
        destination = tmp_path / "selected.json"
        assert (
            main(
                [
                    "journal",
                    "--archive",
                    str(journal.path),
                    "--view",
                    "request",
                    "--sequence",
                    str(second.sequence),
                    "--expand",
                    "--export",
                    str(destination),
                ]
            )
            == 0
        )
        exported = json.loads(destination.read_text())
        assert len(exported["records"]) == 1
        assert exported["records"][0]["sequence"] == second.sequence
        assert (
            exported["records"][0]["input"][-2]["data"]["document"]["text"]["body"]
            == "Recorded guidance."
        )
        captured = capsys.readouterr()
        assert json.loads(captured.out) == exported
        events = [json.loads(line) for line in captured.err.splitlines()]
        assert sum(event["event"] == "model_request_started" for event in events) == 5
        assert sum(event["event"] == "model_request_completed" for event in events) == 5
        recovered = [event for event in events if event["event"] == "model_recovered"]
        assert len(recovered) == 1 and recovered[0]["failures"] == 1
        assert (
            main(
                [
                    "journal",
                    "--archive",
                    str(journal.path),
                    "--view",
                    "operation",
                    "--sequence",
                    str(second.sequence),
                ]
            )
            == 2
        )
        assert len(battle.actions) == 2 and physical == 6

    asyncio.run(scenario())


class ClosingFailure(httpx.AsyncByteStream):
    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self.arrived_before_exit = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield completed()

    async def aclose(self) -> None:
        record = Journal.open(self._journal.path).request(1)
        self.arrived_before_exit = record.attempts[0].reply is not None
        raise httpx.ReadTimeout("Synthetic stream close failure.")


def test_native_reply_is_durable_before_a_failing_stream_exit(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="closing",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        stream = ClosingFailure(journal)

        def provider(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(provider),
        ) as live:
            model = RecordedLlm(live, journal, role=native.DecisionOwner.COMBAT, stream="combat:1")
            with pytest.raises(ModelUnavailable) as error:
                await model.complete((), ())
        assert error.value.reason is FailureKind.TIMEOUT and stream.arrived_before_exit
        journal.close()
        record = Journal.open(journal.path).request(1)
        assert not record.delivered and record.failure is FailureKind.TIMEOUT
        assert record.attempts[0].failure is FailureKind.TIMEOUT
        received = record.attempts[0].reply
        assert received is not None
        assert json.loads(received.native_output) == OUTPUT
        assert Journal.open(journal.path).storage.native_blocks == 1

    asyncio.run(scenario())


def test_recorded_request_accounts_for_each_attempt_and_wire_input(tmp_path: Path) -> None:
    bodies: list[dict[str, JsonValue]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        bodies.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(
            200, content=completed(), headers={"Content-Type": "text/event-stream"}
        )

    async def no_sleep(_: float) -> None:
        return None

    async def scenario() -> None:
        config = configuration(tmp_path, LLM_CONTEXT_WINDOW="60000", LLM_EXTRA_RETRIES="1")
        from sts2_harness.journal import CapacityPolicy

        journal = Journal.create(
            tmp_path / "accounting.sqlite",
            Manifest(
                recording_id="accounting",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                capacity_policy=CapacityPolicy(context_window=config.context_window),
            ),
        )
        async with Llm(config, transport=httpx.MockTransport(provider), sleep=no_sleep) as live:
            model = RecordedLlm(live, journal, role=native.DecisionOwner.RUN, stream="run:1")
            await model.complete((TextMessage(TextRole.USER, "中文 input"),), ())
        journal.close()
        archive = Journal.open(journal.path)
        record = archive.request(1)
        assert archive.manifest.capacity_policy.context_window == 60000
        classifier = archive.manifest.capacity_policy.capacity_classifier_config
        assert classifier.endpoint_category == "unverified"
        assert classifier.enabled_signals == ()
        assert config.protocol in classifier.protocols
        assert record.configuration.capacity_policy == archive.manifest.capacity_policy
        assert len(record.attempts) == 2
        body = bodies[0]
        assert isinstance(body["input"], list) and isinstance(body["tools"], list)
        expected = measure(body, config.protocol).estimated_input_tokens
        assert archive.manifest.capacity_policy.estimator_id == "reported_usage_bytes_v1"
        assert bodies[0] == bodies[1]
        assert record.attempts[0].input_digest == record.attempts[1].input_digest
        assert record.attempts[0].input_digest is not None
        assert [attempt.estimated_input_tokens for attempt in record.attempts] == [
            expected,
            expected,
        ]
        assert record.attempts[0].reply is None
        assert (
            record.attempts[1].reply is not None and record.attempts[1].reply.input_tokens == 12000
        )

    asyncio.run(scenario())


def test_invalid_request_preparation_records_failure_without_physical_attempt(
    tmp_path: Path,
) -> None:
    from sts2_harness.llm.chat_native import ChatCompletionsReply
    from sts2_harness.llm.messages import AssistantMessage

    journal = Journal.create(
        tmp_path / "prepare-failure.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="prepare-failure",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    native_reply = ChatCompletionsReply.from_json(
        json.dumps(
            {
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Done."},
                    }
                ],
            }
        )
    )

    def no_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("No physical request should be sent.")

    async def scenario() -> None:
        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(no_network)) as live:
            model = RecordedLlm(live, journal, role=native.DecisionOwner.RUN, stream="run:1")
            with pytest.raises(ModelUnavailable):
                await model.complete((AssistantMessage(native_reply),), ())
            record = journal.request(1)
            assert not record.delivered
            assert record.failure is FailureKind.CONFIGURATION
            assert record.attempts == ()
        journal.close()

    asyncio.run(scenario())
