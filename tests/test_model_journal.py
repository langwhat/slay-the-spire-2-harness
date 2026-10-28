import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from io import StringIO
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import BattleHTTP, reply
from test_responses import OUTPUT, completed, configuration

from sts2_harness.cli import main
from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent, CombatTask
from sts2_harness.decisions import CombatHandoff
from sts2_harness.diagnostics import Diagnostics
from sts2_harness.execution import Execution
from sts2_harness.game import LiveGame
from sts2_harness.journal import Journal, Manifest, Record, RecordedGame, SessionMaterials
from sts2_harness.journal_models import (
    AssistantSnapshot,
    DocumentData,
    DocumentLookupResult,
    DocumentReadResult,
    GuidanceText,
    MaterialFailed,
    MaterialFailure,
    MaterialKind,
    ModelRecord,
    PromptReadResult,
    ResultSnapshot,
    TextSnapshot,
)
from sts2_harness.knowledge import DocumentView, SkillRead, SubjectId, UnknownDocument
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import FailureKind, ModelUnavailable, TextMessage, TextRole
from sts2_harness.material_text import MaterialText, MaterialUnavailable
from sts2_harness.materials import DocumentSource, FileDocuments
from sts2_harness.model_journal import RecordedDocuments, RecordedLlm, RecordedPrompts
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.results import ActionUnknown
from sts2_harness.tools import ToolCatalog


@pytest.mark.parametrize(
    ("statuses", "retries", "expected"),
    [
        ((401,), 2, ("model_failed",)),
        ((503,), 0, ("model_failed",)),
        ((503, 503), 1, ("model_retry", "model_failed")),
        ((503, 200), 1, ("model_retry", "model_recovered")),
    ],
)
def test_model_diagnostics_describe_actual_retries_and_terminal_failures(
    tmp_path: Path, statuses: tuple[int, ...], retries: int, expected: tuple[str, ...]
) -> None:
    console = StringIO()
    diagnostics = Diagnostics("attempts", console=console)
    journal = Journal.create(
        tmp_path / "archive.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="attempts",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
        diagnostics=diagnostics,
    )
    requests = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        status = statuses[requests]
        requests += 1
        if status == 200:
            return httpx.Response(
                200, content=completed(), headers={"Content-Type": "text/event-stream"}
            )
        return httpx.Response(status, json={"error": {"code": "synthetic"}})

    async def sleep(_: float) -> None:
        return None

    async def scenario() -> None:
        async with Llm(
            replace(configuration(tmp_path), extra_retries=retries),
            transport=httpx.MockTransport(provider),
            sleep=sleep,
        ) as live:
            model = RecordedLlm(live, journal, role=native.DecisionOwner.RUN, stream="run:1")
            if statuses[-1] == 200:
                await model.complete((), ())
            else:
                with pytest.raises(ModelUnavailable):
                    await model.complete((), ())
        journal.close()
        diagnostics.close()

    asyncio.run(scenario())
    events = [json.loads(line) for line in console.getvalue().splitlines()]
    assert (
        tuple(
            event["event"]
            for event in events
            if event["event"] not in ("model_request_started", "model_request_completed")
        )
        == expected
    )
    assert requests == len(statuses)
    for event in events:
        if event["event"] == "model_retry":
            assert event["attempt"] == 2
        if event["event"] == "model_failed":
            assert event["level"] == "ERROR"


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
                    opening_notes=notes,
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
        assert len(second.input) == len(first.input) + 2
        result = second.input[-1]
        assert isinstance(result, ResultSnapshot) and isinstance(result.data, DocumentData)
        assert result.data.document.text.body == "Recorded guidance."
        assert isinstance(second.input[-2], AssistantSnapshot)
        assert second.input[-2].native_output == first.attempts[1].reply.native_output
        assert isinstance(reread.input[-1], ResultSnapshot)
        material_reads = archive.materials()
        prompt_reads = [item for item in material_reads if item.request.kind is MaterialKind.PROMPT]
        explicit_reads = [item for item in material_reads if item.request.kind is MaterialKind.READ]
        lookups = [item for item in material_reads if item.request.kind is MaterialKind.LOOKUP]
        assert len(prompt_reads) == 5 and len(explicit_reads) == 2 and len(lookups) == 5
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
        unknown = resumed.input[-1]
        assert isinstance(unknown, ResultSnapshot) and isinstance(unknown.outcome, ActionUnknown)
        assert unknown.outcome.confirmed_steps == 0 and unknown.outcome.uncertain_step == 1
        assert battle.actions[0].operation_id != battle.actions[1].operation_id
        assert reread.suffix_length == second.suffix_length == 2
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
            exported["records"][0]["input"][-1]["data"]["document"]["text"]["body"]
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


def test_recording_recovers_from_a_locked_archive_without_using_a_missing_input_base(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="recovery",
                core_version="test",
                game_instance_id="game",
                run_id="run",
            ),
        )
        replies = iter([reply(text="First."), reply(text="Second."), reply(text="Third.")])

        def provider(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=next(replies), headers={"Content-Type": "text/event-stream"}
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)) as live:
            model = RecordedLlm(live, journal, role=native.DecisionOwner.COMBAT, stream="combat:1")
            initial = TextMessage(
                TextRole.SYSTEM, MaterialText.create("prompt:test", "Instructions.")
            )
            first = await model.complete((initial,), ())
            with sqlite3.connect(journal.path, timeout=0) as lock:
                lock.execute("BEGIN EXCLUSIVE")
                second_input = (initial, first.assistant, TextMessage(TextRole.USER, "Next."))
                second = await model.complete(second_input, ())
                assert second.assistant.text == "Second."
            third_input = (*second_input, second.assistant, TextMessage(TextRole.USER, "Continue."))
            await model.complete(third_input, ())
        journal.close()
        archive = Journal.open(journal.path)
        first_record, third_record = archive.requests()
        assert [item.sequence for item in archive.requests()] == [1, 3]
        assert third_record.base_sequence == first_record.sequence
        assert third_record.prefix_length == 1 and third_record.suffix_length == 4
        assert len(third_record.input) == 5
        assert [(gap.first_sequence, gap.last_sequence) for gap in archive.gaps] == [(2, 2)]
        assert archive.storage.material_blocks == 1

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


def test_material_failures_and_outer_lookup_reads_remain_distinct_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        journal = Journal.create(
            tmp_path / "archive.sqlite",
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="materials",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=None,
            ),
        )
        path = tmp_path / "missing.txt"
        subject = SubjectId(category="monster", definition_id="SYNTHETIC")
        documents = RecordedDocuments(
            FileDocuments(
                (
                    DocumentSource(
                        id="guide",
                        name="Guide",
                        description="Description",
                        path=path,
                        subjects=(subject,),
                    ),
                )
            ),
            journal,
        )
        with pytest.raises(MaterialUnavailable) as prompt_failure:
            RecordedPrompts(PackagedPrompts(), journal).read("missing-required-instructions")
        with pytest.raises(MaterialUnavailable) as lookup_failure:
            await documents.lookup((subject,))
        with pytest.raises(UnknownDocument) as read_failure:
            await documents.read(SkillRead(id="unknown"))
        listed = await documents.list(native.DecisionOwner.COMBAT)
        assert len(listed) == 1
        path.write_text("Now readable.", encoding="utf-8")
        found = await documents.lookup((subject,))
        assert len(found) == 1 and found[0].text.body == "Now readable."
        journal.close()
        records = Journal.open(journal.path).materials()
        assert [record.request.kind for record in records] == [
            MaterialKind.PROMPT,
            MaterialKind.LOOKUP,
            MaterialKind.READ,
            MaterialKind.LIST,
            MaterialKind.LOOKUP,
        ]
        assert [
            record.reply.failure for record in records if isinstance(record.reply, MaterialFailed)
        ] == [
            MaterialFailure.UNAVAILABLE,
            MaterialFailure.UNAVAILABLE,
            MaterialFailure.UNKNOWN,
        ]
        assert [
            record.reply.message for record in records if isinstance(record.reply, MaterialFailed)
        ] == [str(prompt_failure.value), str(lookup_failure.value), str(read_failure.value)]
        assert Journal.open(journal.path).manifest.initial_notes is None

    asyncio.run(scenario())


def test_indexed_model_and_material_summaries_keep_historical_input_and_incomplete_state(
    tmp_path: Path,
) -> None:
    from test_mod_client import RUN

    from sts2_harness.game import GameReply, Observe, Observed, Settle
    from sts2_harness.journal import DependencyKind, ModelSummary, evidence
    from sts2_harness.journal_models import (
        ConfigurationSnapshot,
        ModelAttempt,
        PromptRead,
        ToolSet,
        archive_input,
    )

    journal = Journal.create(
        tmp_path / "selection.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="selection",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    observation = native.Observation.model_validate_json(RUN)
    journal.begin(Observe())
    journal.finish(
        GameReply(outcome=Observed(observation=observation), observations=(observation,))
    )
    journal.end()
    messages = archive_input((TextMessage(TextRole.USER, "historical input"),))
    first = journal.begin_model(
        native.DecisionOwner.RUN,
        "run:1",
        messages,
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path)),
    )
    journal.model_attempt(first, ModelAttempt(index=1, failure=FailureKind.CONNECTION))
    journal.finish_model(first)
    journal.end_model()
    # A potential game write invalidates prior node knowledge before its result exists.
    route = native.RouteKey.model_validate(observation.location.model_dump())
    journal.begin(
        Settle(route=route, step=native.Interact(kind=native.StepKind.INTERACT, action="travel"))
    )
    journal.end()
    unfinished = journal.begin_model(
        native.DecisionOwner.COMBAT,
        "combat:1",
        messages,
        ToolSet(tools=()),
        ConfigurationSnapshot.of(configuration(tmp_path)),
    )
    from sts2_harness.config import Protocol
    from sts2_harness.journal_models import ReplySnapshot

    journal.model_attempt(
        unfinished,
        ModelAttempt(
            index=1,
            reply=ReplySnapshot(
                protocol=Protocol.RESPONSES,
                native_output=json.dumps(OUTPUT),
                input_tokens=1,
                output_tokens=1,
                cache_read_tokens=None,
                cache_write_tokens=None,
                requested_model="synthetic",
                requested_effort="low",
                response_model="synthetic",
                response_service_tier=None,
            ),
        ),
    )
    journal.end_model()
    material = journal.begin_material(PromptRead(name="guide"))
    journal.finish_material(
        material,
        MaterialFailed(failure=MaterialFailure.UNAVAILABLE, message="Synthetic guide unavailable."),
    )
    journal.close()
    reader = Journal.open(journal.path)
    selected = evidence(
        reader,
        kind=DependencyKind.MODEL,
        role=native.DecisionOwner.RUN,
        node_id=observation.location.node_id,
    )
    assert len(selected.records) == 1
    summary = selected.records[0]
    assert isinstance(summary, ModelSummary)
    assert summary.input_messages == 1 and summary.physical_attempts == 1 and summary.delivered
    pending = evidence(reader, sequence=unfinished).records[0]
    assert isinstance(pending, ModelSummary) and pending.failure is None and not pending.delivered
    assert pending.node_id is None
    assert [row.sequence for row in evidence(reader, errors_only=True).records] == [
        3,
        unfinished,
        material,
    ]
    expanded = evidence(reader, sequence=first, expand=True).records[0]
    assert isinstance(expanded, ModelRecord) and expanded.input == messages
    reader.close()


def test_summary_storage_failure_preserves_gap_and_allows_later_model_recording(
    tmp_path: Path,
) -> None:
    from sts2_harness.journal import evidence
    from sts2_harness.journal_models import (
        ConfigurationSnapshot,
        ModelAttempt,
        ToolSet,
        archive_input,
    )

    journal = Journal.create(
        tmp_path / "index-io.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="io",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )
    messages = archive_input((TextMessage(TextRole.USER, "Keep playing after archive failure."),))
    snapshot = ConfigurationSnapshot.of(configuration(tmp_path))
    # A missing index destination simulates a failed SQLite write, not a model failure.
    with sqlite3.connect(journal.path) as connection:
        connection.execute("ALTER TABLE summaries RENAME TO unavailable_summaries")
    failed = journal.begin_model(
        native.DecisionOwner.RUN, "run", messages, ToolSet(tools=()), snapshot
    )
    assert journal.availability.error is not None
    with sqlite3.connect(journal.path) as connection:
        connection.execute("ALTER TABLE unavailable_summaries RENAME TO summaries")
    journal.model_attempt(failed, ModelAttempt(index=1))
    journal.finish_model(failed)
    journal.end_model()
    recovered = journal.begin_model(
        native.DecisionOwner.RUN, "run", messages, ToolSet(tools=()), snapshot
    )
    journal.finish_model(recovered)
    journal.end_model()
    journal.close()
    reader = Journal.open(journal.path)
    selected = evidence(reader)
    assert [row.sequence for row in selected.records] == [recovered]
    assert [(gap.first_sequence, gap.last_sequence) for gap in selected.gaps] == [(failed, failed)]
    expanded = evidence(reader, sequence=recovered, expand=True).records[0]
    assert isinstance(expanded, ModelRecord) and expanded.input == messages
    reader.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE model_requests SET delivered=0,failure='protocol'",
        "UPDATE model_requests SET cursor=9",
        "UPDATE model_requests SET role='run'",
        "UPDATE model_requests SET stream='different'",
        "UPDATE model_requests SET position=9",
        "UPDATE records SET cursor=9",
        "UPDATE records SET kind='pause'",
        "UPDATE records SET operation_id='different'",
        "UPDATE materials SET cursor=9",
        "UPDATE materials SET reply=NULL",
    ],
)
def test_summary_rejects_disagreement_with_authoritative_metadata(
    tmp_path: Path,
    mutation: str,
) -> None:
    from test_mod_client import RUN

    from sts2_harness.game import GameReply, Observe, Observed
    from sts2_harness.journal import ArchiveError, evidence
    from sts2_harness.journal_models import MaterialSnapshot, PromptRead

    journal = Journal.create(
        tmp_path / "metadata.sqlite",
        Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="metadata",
            core_version="test",
            game_instance_id="game",
            run_id="run",
        ),
    )

    async def record() -> None:
        def provider(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=reply(text="Complete."), headers={"Content-Type": "text/event-stream"}
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(provider)) as live:
            model = RecordedLlm(live, journal, role=native.DecisionOwner.COMBAT, stream="combat:1")
            await model.complete((TextMessage(TextRole.USER, "Recorded input."),), ())

    asyncio.run(record())
    observation = native.Observation.model_validate_json(RUN)
    journal.begin(Observe())
    journal.finish(
        GameReply(outcome=Observed(observation=observation), observations=(observation,))
    )
    journal.end()
    material = journal.begin_material(PromptRead(name="synthetic"))
    journal.finish_material(
        material,
        PromptReadResult(
            prompt=MaterialSnapshot.of(
                MaterialText.create("prompt:synthetic", "Historical material."),
            )
        ),
    )
    journal.close()
    reader = Journal.open(journal.path)
    assert len(evidence(reader).records) == 3
    with sqlite3.connect(journal.path) as connection:
        connection.execute(mutation)
    with pytest.raises(ArchiveError, match="summary"):
        Journal.open(journal.path)
    for expand, errors_only in ((False, False), (False, True), (True, False)):
        with pytest.raises(ArchiveError, match="summary"):
            evidence(reader, expand=expand, errors_only=errors_only)
    reader.close()
