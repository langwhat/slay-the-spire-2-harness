import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue
from test_combat import reply
from test_decision_replay import decision_case
from test_mod_client import COMBAT
from test_responses import configuration
from test_run import handoff

from sts2_harness.client import models as native
from sts2_harness.fights import Fight, FightKind
from sts2_harness.llm.client import Llm
from sts2_harness.tools import ToolName
from sts2_harness_eval.decision_cases import (
    BattleState,
    CorrectionEvent,
    DecisionVerdict,
    HarnessState,
    NativeRead,
    ProposalStatus,
    TaskEvent,
)
from sts2_harness_eval.frozen_catalog import FrozenCatalog, SnapshotIncomplete
from sts2_harness_eval.stable_decisions import EvaluationRole, FreshGame, sample_fresh
from sts2_harness_eval.traffic import AuditingTransport


def test_frozen_catalog_answers_new_queries_and_preserves_unknowns() -> None:
    snapshot = FrozenCatalog.model_validate_json(
        json.dumps(
            {
                "complete": True,
                "catalog": {
                    "categories": ["relic"],
                    "card_filters": {"pools": [], "types": [], "rarities": [], "energy_costs": []},
                },
                "definitions": [
                    {
                        "category": "relic",
                        "definition_id": "RELIC.ALPHA",
                        "name": "Alpha",
                        "native": {
                            "definition_id": "RELIC.ALPHA",
                            "name": "Alpha",
                            "text": "Gain strength.",
                            "rarity": "common",
                        },
                    },
                    {
                        "category": "relic",
                        "definition_id": "RELIC.BETA",
                        "name": "Beta",
                        "native": {
                            "definition_id": "RELIC.BETA",
                            "name": "Beta",
                            "text": "Gain strength and block.",
                            "rarity": "rare",
                        },
                    },
                ],
                "search_index": {
                    "source": "native capture",
                    "entries": [
                        {"definition_id": "RELIC.ALPHA", "name": "Alpha", "text": "Gain strength."},
                        {
                            "definition_id": "RELIC.BETA",
                            "name": "Beta",
                            "text": "Gain strength and block.",
                        },
                    ],
                },
            }
        )
    )

    async def run() -> None:
        found = await snapshot.query(
            native.LookupQuery(mode=native.CatalogMode.LOOKUP, ids=("RELIC.BETA", "RELIC.ALPHA"))
        )
        assert isinstance(found, native.CatalogCompleted)
        assert isinstance(found.data, native.LookupData)
        assert [entry.definition_id for entry in found.data.entries] == [
            "RELIC.BETA",
            "RELIC.ALPHA",
        ]
        search = await snapshot.query(
            native.SearchQuery(mode=native.CatalogMode.SEARCH, text="strength", limit=1)
        )
        assert isinstance(search, native.CatalogCompleted)
        assert isinstance(search.data, native.SearchData)
        assert search.data.total == 2 and search.data.next_offset == 1
        assert search.data.matches[0].definition_id == "RELIC.ALPHA"
        without_index = snapshot.model_copy(update={"search_index": None})
        with pytest.raises(SnapshotIncomplete, match="raw search index"):
            await without_index.query(
                native.SearchQuery(mode=native.CatalogMode.SEARCH, text="Alpha")
            )
        listing = await without_index.query(native.SearchQuery(mode=native.CatalogMode.SEARCH))
        assert isinstance(listing, native.CatalogCompleted)
        assert isinstance(listing.data, native.SearchData) and listing.data.total == 2
        mixed = await snapshot.query(
            native.LookupQuery(mode=native.CatalogMode.LOOKUP, ids=("RELIC.UNKNOWN", "RELIC.ALPHA"))
        )
        assert isinstance(mixed, native.CatalogCompleted)
        assert isinstance(mixed.data, native.LookupData)
        assert mixed.data.missing_ids == ("RELIC.UNKNOWN",)
        assert [entry.definition_id for entry in mixed.data.entries] == ["RELIC.ALPHA"]
        with pytest.raises(SnapshotIncomplete):
            await snapshot.model_copy(update={"complete": False}).query(
                native.LookupQuery(mode=native.CatalogMode.LOOKUP, ids=("RELIC.UNKNOWN",))
            )
        with pytest.raises(SnapshotIncomplete):
            await snapshot.model_copy(update={"complete": False}).query(
                native.SearchQuery(mode=native.CatalogMode.SEARCH, text="strength")
            )

    asyncio.run(run())


def test_raw_catalog_search_fields_are_complete_and_never_returned() -> None:
    from pydantic import ValidationError

    data: dict[str, JsonValue] = {
        "complete": True,
        "catalog": {
            "categories": ["relic"],
            "card_filters": {"pools": [], "types": [], "rarities": [], "energy_costs": []},
        },
        "definitions": [
            {
                "category": "relic",
                "definition_id": "RELIC.ALPHA",
                "name": "Alpha",
                "native": {
                    "definition_id": "RELIC.ALPHA",
                    "name": "Alpha",
                    "text": "获得1能量。",
                    "rarity": "common",
                },
            }
        ],
        "search_index": {
            "source": "native capture",
            "entries": [
                {
                    "definition_id": "RELIC.ALPHA",
                    "name": "[b]Alpha[/b]",
                    "text": "[img]energy_icon[/img]",
                }
            ],
        },
    }
    snapshot = FrozenCatalog.model_validate_json(json.dumps(data))

    async def run() -> None:
        result = await snapshot.query(
            native.SearchQuery(mode=native.CatalogMode.SEARCH, text="energy")
        )
        assert isinstance(result, native.CatalogCompleted)
        assert isinstance(result.data, native.SearchData) and result.data.total == 1
        assert result.data.matches[0].name == "Alpha"
        assert result.data.matches[0].description == "获得1能量。"
        assert "[img]" not in result.to_json() and "[b]" not in result.to_json()

    asyncio.run(run())
    index_data = data["search_index"]
    assert isinstance(index_data, dict)
    index_data["entries"] = []
    with pytest.raises(ValidationError, match="cover each definition"):
        FrozenCatalog.model_validate_json(json.dumps(data))


def test_model_search_result_contains_only_rendered_catalog_fields(tmp_path: Path) -> None:
    from pydantic import TypeAdapter

    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        observation = observation.model_copy(
            update={
                "state": observation.state.model_copy(update={"screen": native.RunScreen.SELECTION})
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        snapshot = FrozenCatalog.model_validate_json(
            '{"complete":true,"catalog":{"categories":["relic"],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},'
            '"definitions":[{"category":"relic","definition_id":"RELIC.ALPHA",'
            '"name":"Alpha","native":{"definition_id":"RELIC.ALPHA","name":"Alpha",'
            '"text":"获得1能量。","rarity":"common"}}],'
            '"search_index":{"source":"SOURCE-PROVENANCE-SENTINEL","entries":'
            '[{"definition_id":"RELIC.ALPHA","name":"[b]Alpha[/b]",'
            '"text":"RAW_ONLY_MARKER [img]energy_icon[/img]"}]}}'
        )
        replies = iter(
            (
                reply(("search", "run_knowledge", '{"mode":"search","text":"RAW_ONLY_MARKER"}')),
                reply(("action", "run_act", '{"steps":[{"kind":"select","refs":["candidate"]}]}')),
            )
        )
        requests: list[dict[str, JsonValue]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
            return httpx.Response(
                200, content=next(replies), headers={"Content-Type": "text/event-stream"}
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(
                case,
                client,
                catalog=snapshot,
                role=EvaluationRole.RUN,
            )
        assert sample.verdict is DecisionVerdict.PASSED and len(requests) == 2, sample.error
        messages = requests[1]["input"]
        assert isinstance(messages, list)
        outputs = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("type") == "function_call_output"
        ]
        assert len(outputs) == 1 and outputs[0]["call_id"] == "search"
        output = outputs[0]["output"]
        assert isinstance(output, str)
        parsed = TypeAdapter(dict[str, JsonValue]).validate_json(output)
        data = parsed["data"]
        assert isinstance(data, dict) and data["total"] == 1
        matches = data["matches"]
        assert isinstance(matches, list) and len(matches) == 1 and isinstance(matches[0], dict)
        assert matches[0]["name"] == "Alpha" and matches[0]["description"] == "获得1能量。"
        assert "RAW_ONLY_MARKER" not in output
        assert "SOURCE-PROVENANCE-SENTINEL" not in output
        assert "[img]" not in output and "[b]" not in output

    asyncio.run(run())


def test_frozen_catalog_search_rejects_unverified_unicode_comparisons() -> None:
    snapshot = FrozenCatalog.model_validate_json(
        json.dumps(
            {
                "complete": True,
                "catalog": {
                    "categories": ["relic"],
                    "card_filters": {"pools": [], "types": [], "rarities": [], "energy_costs": []},
                },
                "definitions": [
                    {
                        "category": "relic",
                        "definition_id": "RELIC.ALPHA",
                        "name": "力量 Alpha",
                        "native": {
                            "definition_id": "RELIC.ALPHA",
                            "name": "力量 Alpha",
                            "text": "获得3点力量。",
                            "rarity": "common",
                        },
                    },
                ],
                "search_index": {
                    "source": "native capture",
                    "entries": [
                        {
                            "definition_id": "RELIC.ALPHA",
                            "name": "力量 Alpha",
                            "text": "获得3点力量。",
                        }
                    ],
                },
            }
        )
    )

    async def run() -> None:
        good = await snapshot.query(
            native.SearchQuery(mode=native.CatalogMode.SEARCH, text="力量 alpha")
        )
        assert isinstance(good, native.CatalogCompleted)
        assert isinstance(good.data, native.SearchData)
        assert good.data.total == 1 and good.data.matches[0].definition_id == "RELIC.ALPHA"
        for text in ("Straße", "İ", "ı", "ſ", "K", "ﬀ", "力\u001c量", "力\u3000量", "\ud800"):
            with pytest.raises(SnapshotIncomplete):
                await snapshot.query(native.SearchQuery(mode=native.CatalogMode.SEARCH, text=text))
        index = snapshot.search_index
        assert index is not None
        for text in ("Straße", "İ", "ı", "ſ", "K", "ﬀ", "\ud800", "\u0378"):
            unsafe = snapshot.model_copy(
                update={
                    "search_index": index.model_copy(
                        update={"entries": (index.entries[0].model_copy(update={"name": text}),)}
                    )
                }
            )
            with pytest.raises(SnapshotIncomplete):
                await unsafe.query(native.SearchQuery(mode=native.CatalogMode.SEARCH, text="力量"))
        unsafe_description = snapshot.model_copy(
            update={
                "search_index": index.model_copy(
                    update={"entries": (index.entries[0].model_copy(update={"text": "Straße"}),)}
                )
            }
        )
        with pytest.raises(SnapshotIncomplete):
            await unsafe_description.query(
                native.SearchQuery(mode=native.CatalogMode.SEARCH, text="unrelated")
            )
        non_ascii_id = snapshot.model_copy(
            update={
                "definitions": (
                    snapshot.definitions[0].model_copy(update={"definition_id": "RELIC.😀"}),
                )
            }
        )
        with pytest.raises(SnapshotIncomplete):
            await non_ascii_id.query(native.SearchQuery(mode=native.CatalogMode.SEARCH))

    asyncio.run(run())


def test_fresh_decision_uses_production_task_without_old_notes_or_report(tmp_path: Path) -> None:
    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent)
        card = task.observation.objects["owned"]
        assert isinstance(card, native.CardEntry)
        previous = handoff("OLD-REVIEW-SENTINEL")
        report = previous.report.model_copy(
            update={
                "encounter": native.NamedDefinition(
                    name="OLD-REPORT-SENTINEL", definition_id="ENCOUNTER.PREVIOUS"
                )
            }
        )
        case = original.model_copy(
            update={
                "recorded_reason": "OLD-REASON-SENTINEL",
                "events": (
                    task.model_copy(
                        update={
                            "state": HarnessState(
                                notes={"core": "OLD-NOTES-SENTINEL"},
                                observed_plays={card.definition_id: 12345},
                                fights=(
                                    Fight(
                                        floor=123,
                                        kind=FightKind.ELITE,
                                        turns=456,
                                        hp_in=789,
                                        hp_out=654,
                                    ),
                                ),
                            ),
                            "handoff": BattleState(report=report, review=previous.review),
                            "previous_battle": BattleState(report=report, review=previous.review),
                        }
                    ),
                ),
            }
        )
        bodies: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content.decode())
            return httpx.Response(
                200,
                content=reply(
                    (
                        "choice",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "offer": "offer",
                                "choice": "take",
                                "card": "candidate",
                                "reason": "A useful card.",
                            }
                        ),
                    )
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        config = replace(configuration(tmp_path), extra_retries=0)
        async with Llm(config, transport=httpx.MockTransport(respond)) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.DECK)
        assert sample.verdict is DecisionVerdict.PASSED
        assert sample.reason == "A useful card."
        assert len(bodies) == 1
        assert '"task":"act"' in bodies[0].replace('\\"', '"')
        assert '"baseline":true' in bodies[0].replace('\\"', '"')
        assert "OLD-NOTES-SENTINEL" not in bodies[0]
        assert "OLD-REASON-SENTINEL" not in bodies[0]
        assert "OLD-REPORT-SENTINEL" not in bodies[0]
        assert "OLD-REVIEW-SENTINEL" not in bodies[0]
        assert '"hp_in":789' in bodies[0].replace('\\"', '"')
        assert '"observed_plays":12345' in bodies[0].replace('\\"', '"')

    asyncio.run(run())


def test_stream_records_terminal_usage_before_late_close() -> None:
    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            event = (
                b'data: {"type":"response.completed","response":{"usage":'
                b'{"input_tokens":100,"output_tokens":20,"input_tokens_details":'
                b'{"cached_tokens":40},"output_tokens_details":{"reasoning_tokens":12}}}}\n\n'
            )
            yield event[:38]
            yield event[38:]

        async def aclose(self) -> None:
            await asyncio.sleep(0.01)

    async def run() -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=SlowClose())

        audit = AuditingTransport(httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=audit) as client:
            async with client.stream(
                "POST", "https://model.invalid/v1/responses", json={}
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
        assert len(audit.attempts) == 1
        attempt = audit.attempts[0]
        assert attempt.completed_seconds is not None
        assert attempt.closed_seconds is not None
        assert attempt.closed_seconds > attempt.completed_seconds
        assert attempt.usage is not None
        assert attempt.usage.reasoning_tokens == 12
        assert attempt.usage.weighted_tokens == 162

    asyncio.run(run())


def test_fresh_run_continues_after_notes_and_a_new_catalog_query(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        state = observation.state.model_copy(
            update={"screen": native.RunScreen.SELECTION, "offers": (), "actions": ()}
        )
        case = case.model_copy(
            update={
                "events": (
                    task.model_copy(
                        update={"observation": observation.model_copy(update={"state": state})}
                    ),
                )
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            json.dumps(
                {
                    "complete": False,
                    "catalog": {
                        "categories": ["relic"],
                        "card_filters": {
                            "pools": [],
                            "types": [],
                            "rarities": [],
                            "energy_costs": [],
                        },
                    },
                    "definitions": [
                        {
                            "category": "relic",
                            "definition_id": "RELIC.ALPHA",
                            "name": "Alpha",
                            "native": {
                                "definition_id": "RELIC.ALPHA",
                                "name": "Alpha",
                                "text": "Gain strength.",
                                "rarity": "common",
                            },
                        }
                    ],
                }
            )
        )
        responses = iter(
            (
                reply(("query", "run_knowledge", '{"mode":"lookup","ids":["RELIC.ALPHA"]}')),
                reply(("notes", "run_notes", '{"mode":"write","other":"Test note."}')),
                reply(
                    (
                        "action",
                        "run_act",
                        '{"steps":[{"kind":"select","refs":["candidate"]}],'
                        '"reason":"Select the card."}',
                    )
                ),
            )
        )
        bodies: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content.decode())
            return httpx.Response(
                200, content=next(responses), headers={"Content-Type": "text/event-stream"}
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            result = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
        assert result.verdict is DecisionVerdict.PASSED, result.error
        assert result.reason == "Select the card."
        assert len(bodies) == 3
        assert "Gain strength." in bodies[1]
        assert "Test note." in bodies[2]
        assert all('"task":"act"' in body.replace('\\"', '"') for body in bodies)

    asyncio.run(run())


def test_plan_estimate_failure_usage_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sts2_harness_eval.stable_suite import EvaluationPlan, estimate, evaluate

    case = decision_case(tmp_path)
    tmp_path.joinpath("case.json").write_text(case.model_dump_json())
    tmp_path.joinpath("catalog.json").write_text(
        '{"complete":false,"catalog":{"categories":[],"card_filters":'
        '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
    )
    plan = EvaluationPlan.model_validate_json(
        json.dumps(
            {
                "configurations": [{"id": "high", "effort": "high", "prompt_version": "test"}],
                "cases": [
                    {
                        "id": "q1",
                        "role": "deck",
                        "case": "case.json",
                        "catalog": "catalog.json",
                        "configurations": ["high"],
                        "repetitions": 2,
                    }
                ],
                "historical_means": [
                    {
                        "role": "deck",
                        "effort": "high",
                        "requests_per_trial": 1.5,
                        "weighted_tokens_per_request": 100,
                        "source": "prior run",
                    }
                ],
            }
        )
    )
    assert estimate(plan).requests == 3
    assert estimate(plan).weighted_tokens == 300
    count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count == 1:
            event: dict[str, JsonValue] = {
                "type": "response.failed",
                "sequence_number": 1,
                "response": {
                    "id": "failure",
                    "object": "response",
                    "created_at": 1,
                    "status": "failed",
                    "model": "fake",
                    "output": [],
                    "error": {"code": "server_error", "message": "failed"},
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 40},
                        "output_tokens_details": {"reasoning_tokens": 12},
                    },
                },
            }
            content = ("data: " + json.dumps(event) + "\n\n").encode()
        else:
            content = reply(
                ("choice", "run_deck_choice", '{"offer":"offer","choice":"skip","reason":"Wait."}')
            )
        return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})

    async def run() -> None:
        results = await evaluate(
            plan,
            tmp_path,
            tmp_path / "results",
            configuration(tmp_path),
            concurrency=1,
            transport=lambda: httpx.MockTransport(respond),
        )
        assert len(results) == 2 and count == 2
        failed, success = results
        assert failed.attempts[0].terminal_event == "response.failed"
        assert failed.attempts[0].weighted_tokens == 162
        assert failed.attempts[0].reasoning_tokens == 12
        assert failed.attempts[0].completed_seconds is None
        assert success.attempts[0].weighted_tokens is None
        assert success.attempts[0].completed_seconds is not None
        report = tmp_path.joinpath("results", "report.md").read_text()
        assert "300.0 weighted tokens" in report
        assert "162 known weighted tokens (1 requests with unknown weighted usage)" in report
        assert "Wait." in report and "unlabeled" in report
        assert tmp_path.joinpath("results", "trial-0001.json").is_file()

    asyncio.run(run())
    assert "requests=3.0" in capsys.readouterr().out


def test_stream_without_completed_retains_unknown_usage() -> None:
    async def run() -> None:
        audit = AuditingTransport(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=b'data: {"type":"response.created"}\n\n'
                )
            )
        )
        async with httpx.AsyncClient(transport=audit) as client:
            await client.post("https://model.invalid", json={})
        attempt = audit.attempts[0]
        assert attempt.completed_seconds is None
        assert attempt.closed_seconds is not None
        assert attempt.usage is None

    asyncio.run(run())


def test_other_protocols_are_rejected_before_sending(tmp_path: Path) -> None:
    from sts2_harness.config import Protocol
    from sts2_harness_eval.stable_suite import EvaluationPlan, evaluate

    plan = EvaluationPlan.model_validate_json(
        json.dumps(
            {
                "configurations": [{"id": "one", "effort": "high", "prompt_version": "test"}],
                "cases": [
                    {
                        "id": "one",
                        "role": "deck",
                        "case": "missing.json",
                        "catalog": "missing.json",
                        "configurations": ["one"],
                        "repetitions": 1,
                    }
                ],
            }
        )
    )
    requests: list[httpx.Request] = []

    def transport() -> httpx.AsyncBaseTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        return httpx.MockTransport(respond)

    async def run() -> None:
        with pytest.raises(ValueError, match="Responses protocol"):
            await evaluate(
                plan,
                tmp_path,
                tmp_path / "out",
                replace(configuration(tmp_path), protocol=Protocol.MESSAGES),
                concurrency=1,
                transport=transport,
            )

    asyncio.run(run())
    assert not requests and not tmp_path.joinpath("out").exists()


def test_report_compares_different_choices_with_both_reasons(tmp_path: Path) -> None:
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, estimate, report

    case = decision_case(tmp_path)
    plan = EvaluationPlan.model_validate_json(
        json.dumps(
            {
                "configurations": [
                    {"id": "one", "effort": "high", "prompt_version": "a"},
                    {"id": "two", "effort": "high", "prompt_version": "b"},
                ],
                "cases": [
                    {
                        "id": "q1",
                        "role": "deck",
                        "case": "case.json",
                        "catalog": "catalog.json",
                        "configurations": ["one", "two"],
                        "repetitions": 1,
                    }
                ],
            }
        )
    )

    def trial(config: str, choice: str, reason: str) -> Trial:
        from sts2_harness_eval.decision_cases import DecisionSample

        return Trial(
            case_id="q1",
            role=EvaluationRole.DECK,
            configuration=config,
            repetition=1,
            prompt_version=config,
            prompt_digest="digest",
            input_hashes={},
            error=None,
            sample=DecisionSample(
                source=case.source,
                sample=1,
                verdict=DecisionVerdict.UNLABELED,
                choices=(choice,),
                reason=reason,
            ),
            attempts=(),
        )

    text = report(
        plan, (trial("one", "take", "First reason"), trial("two", "skip", "Second reason"))
    )
    assert "one #1 chose take: First reason; two #1 chose skip: Second reason" in text
    assert "Unlabeled and unsupported trials are not scored" in text
    assert "Scored: 0; passed: 0; failed: 0. Unlabeled: 2" in text
    assert estimate(plan).requests is None
    assert estimate(plan).missing_means == ("deck/high",)

    failed = trial("one", "", "Unavailable")
    assert failed.sample is not None
    failed = failed.model_copy(
        update={
            "sample": failed.sample.model_copy(
                update={"verdict": DecisionVerdict.UNSUPPORTED, "choices": ()}
            )
        }
    )
    unavailable = report(plan, (failed, trial("two", "skip", "Second reason")))
    assert "could not be compared" in unavailable


def test_report_compares_unscored_combat_proposals_and_reasons(tmp_path: Path) -> None:
    from sts2_harness.game import Inspect
    from sts2_harness_eval.decision_cases import DecisionSample
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, report

    async def run() -> None:
        case = decision_case(tmp_path)
        task = case.events[0]
        assert isinstance(task, TaskEvent)
        observation = native.Observation.model_validate_json(COMBAT)
        assert isinstance(observation.state, native.CombatState)
        observation = observation.model_copy(
            update={
                "input_readiness": native.InputReadiness.READY,
                "state": observation.state.model_copy(
                    update={"screen": native.CombatScreen.COMBAT, "selection": None}
                ),
            }
        )
        observation = native.Observation.model_validate_json(observation.model_dump_json())
        history = native.HistoryData(
            cards_played_this_combat=(), cards_played_this_turn=(), cards_exhausted_this_turn=()
        )
        read = NativeRead(
            request=Inspect(query=native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)),
            result=native.InspectCompleted(
                status=native.ResultStatus.COMPLETED, observation=observation, data=history
            ),
        )
        case = case.model_copy(
            update={
                "events": (task.model_copy(update={"observation": observation}),),
                "native_reads": (read,),
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = (
            '{"steps":[{"kind":"end_turn"}],"reason":"End now."}',
            '{"steps":[{"kind":"use_potion","potion":"potion"}],"reason":"Use potion."}',
        )

        async def sample_for(argument: str) -> DecisionSample:
            requests: list[httpx.Request] = []

            def respond(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(
                    200,
                    content=reply(("action", "combat_act", argument)),
                    headers={"Content-Type": "text/event-stream"},
                )

            async with Llm(
                replace(configuration(tmp_path), extra_retries=0),
                transport=httpx.MockTransport(respond),
            ) as client:
                sample = await sample_fresh(
                    case, client, catalog=catalog, role=EvaluationRole.COMBAT
                )
            assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
            assert sample.proposal is not None and not sample.choices
            assert sample.proposal_status is ProposalStatus.UNKNOWN
            return sample

        samples = [await sample_for(argument) for argument in arguments]
        plan = EvaluationPlan.model_validate_json(
            '{"configurations":[{"id":"one","effort":"high","prompt_version":"one"},'
            '{"id":"two","effort":"high","prompt_version":"two"}],'
            '"cases":[{"id":"q1","role":"combat","case":"case.json",'
            '"catalog":"catalog.json","configurations":["one","two"],"repetitions":1}]}'
        )
        trials = tuple(
            Trial(
                case_id="q1",
                role=EvaluationRole.COMBAT,
                configuration=config,
                repetition=1,
                prompt_version=config,
                prompt_digest="digest",
                input_hashes={},
                sample=sample,
                error=None,
                attempts=(),
            )
            for config, sample in zip(("one", "two"), samples, strict=True)
        )
        text = report(plan, trials)
        for sample in samples:
            assert sample.proposal is not None
            assert sample.proposal.to_json() in text and sample.reason in text
        assert text.count("Proposal legality unknown") >= 2 and text.count("unscored") >= 2
        missing = trials[0].model_copy(
            update={
                "sample": samples[0].model_copy(update={"proposal": None, "proposal_status": None})
            }
        )
        incomplete = report(plan, (missing, trials[1]))
        assert "could not be compared" in incomplete
        assert "declared" not in incomplete

    asyncio.run(run())


@pytest.mark.parametrize(
    "first_tool, first_arguments, second_tool, second_arguments, expected",
    (
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}],"reason":"First."}',
            ToolName.COMBAT_ACT,
            '{ "reason": "Second.", "steps": [ { "kind": "end_turn" } ] }',
            "same",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            ToolName.COMBAT_ACT,
            '{"reason":"Different reason.","steps":[{"kind":"end_turn"}]}',
            "same",
        ),
        (
            ToolName.RUN_DECK_CHOICE,
            '{"choice":"take","card":"c1","reason":"First."}',
            ToolName.RUN_DECK_CHOICE,
            '{"reason":"Second.","card":["c1"],"choice":"take"}',
            "same",
        ),
        (
            ToolName.RUN_NEXT,
            '{"reason":"First."}',
            ToolName.RUN_NEXT,
            '{"reason":"Second."}',
            "same",
        ),
        (
            ToolName.RUN_ACT,
            '{"steps":[{"kind":"interact","action":"one"}],"reason":"First."}',
            ToolName.RUN_ACT,
            '{"reason":"Second.","steps":[{"action":"one","kind":"interact"}]}',
            "same",
        ),
        (
            ToolName.RUN_PLAN,
            '{"reason":"First.","stretches":["s1"]}',
            ToolName.RUN_PLAN,
            '{"stretches":["s1"],"reason":"Second."}',
            "same",
        ),
        (
            ToolName.RUN_PLAN,
            '{"reason":"First.","stretches":["s1","s2"]}',
            ToolName.RUN_PLAN,
            '{"reason":"Second.","stretches":["s2","s1"]}',
            "different",
        ),
        (
            ToolName.RUN_PLAN,
            '{"reason":"First.","stretches":["s1","s2"]}',
            ToolName.RUN_PLAN,
            '{"reason":"Second.","stretches":["s1","s3"]}',
            "different",
        ),
        (
            ToolName.RUN_PLAN,
            '{"reason":"First.","stretches":["s1"],"stretches":["s2"]}',
            ToolName.RUN_PLAN,
            '{"reason":"Second.","stretches":["s1"]}',
            "incomparable",
        ),
        (
            ToolName.RUN_PLAN,
            '{"reason":"First.","stretches":[]}',
            ToolName.RUN_PLAN,
            '{"reason":"Second.","stretches":["s1"]}',
            "incomparable",
        ),
        (
            ToolName.RUN_PLAN,
            '{"reason":"First.","reason":"Other.","stretches":["s1"]}',
            ToolName.RUN_PLAN,
            '{"reason":"Second.","stretches":["s1"]}',
            "incomparable",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"},{"kind":"use_potion","potion":"p"}]}',
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"use_potion","potion":"p"},{"kind":"end_turn"}]}',
            "different",
        ),
        (
            ToolName.RUN_ACT,
            '{"steps":[{"kind":"interact","action":"a"},{"kind":"interact","action":"b"}]}',
            ToolName.RUN_ACT,
            '{"steps":[{"kind":"interact","action":"b"},{"kind":"interact","action":"a"}]}',
            "different",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"use_potion","potion":"p1"}]}',
            "different",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            ToolName.RUN_NEXT,
            '{"reason":"Move."}',
            "different",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn","kind":"end_turn"}]}',
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            "incomparable",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]',
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            "incomparable",
        ),
        (
            ToolName.COMBAT_ACT,
            '{"steps":[]}',
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            "incomparable",
        ),
        (
            ToolName.RUN_NOTES,
            '{"mode":"read"}',
            ToolName.COMBAT_ACT,
            '{"steps":[{"kind":"end_turn"}]}',
            "incomparable",
        ),
    ),
)
def test_report_compares_proposal_actions_with_strict_schema(
    tmp_path: Path,
    first_tool: ToolName,
    first_arguments: str,
    second_tool: ToolName,
    second_arguments: str,
    expected: str,
) -> None:
    from sts2_harness_eval.decision_cases import CapturedProposal, DecisionSample
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, report

    plan = EvaluationPlan.model_validate_json(
        '{"configurations":[{"id":"one","effort":"high","prompt_version":"one"},'
        '{"id":"two","effort":"high","prompt_version":"two"}],'
        '"cases":[{"id":"q1","role":"combat","case":"case.json",'
        '"catalog":"catalog.json","configurations":["one","two"],"repetitions":1}]}'
    )
    source = decision_case(tmp_path).source
    proposals = (
        CapturedProposal(tool=first_tool, arguments=first_arguments),
        CapturedProposal(tool=second_tool, arguments=second_arguments),
    )
    trials = tuple(
        Trial(
            case_id="q1",
            role=EvaluationRole.COMBAT,
            configuration=config,
            repetition=1,
            prompt_version=config,
            prompt_digest="digest",
            input_hashes={},
            sample=DecisionSample(
                source=source,
                sample=1,
                verdict=DecisionVerdict.UNSUPPORTED,
                choices=(),
                reason=reason,
                error="Proposal legality unknown.",
                proposal=proposal,
                proposal_status=ProposalStatus.UNKNOWN,
            ),
            error=None,
            attempts=(),
        )
        for config, reason, proposal in zip(
            ("one", "two"), ("First.", "Second."), proposals, strict=True
        )
    )
    text = report(plan, trials)
    assert all(proposal.to_json() in text for proposal in proposals)
    assert ("declared" in text) == (expected == "different")
    assert ("could not be compared" in text) == (expected == "incomparable")


def test_report_compares_ordered_stretches_after_production_acceptance(tmp_path: Path) -> None:
    from sts2_harness_eval.decision_cases import CapturedProposal, DecisionSample
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, report

    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        observation = observation.model_copy(
            update={
                "state": observation.state.model_copy(
                    update={
                        "progress": observation.state.progress.model_copy(
                            update={"position": "map:0:0"}
                        )
                    }
                ),
                "map": native.PublicMap(
                    bosses=(),
                    nodes=(
                        native.MapNode(
                            position="map:0:0", room_type="monster", children=("map:1:0",)
                        ),
                        native.MapNode(
                            position="map:1:0",
                            room_type="monster",
                            children=("map:2:0", "map:2:1"),
                        ),
                        native.MapNode(position="map:2:0", room_type="elite", children=()),
                        native.MapNode(position="map:2:1", room_type="elite", children=()),
                    ),
                    legal_destinations=("map:1:0",),
                ),
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )

        async def sample_for(arguments: str) -> DecisionSample:
            requests: list[httpx.Request] = []

            def respond(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(
                    200,
                    content=reply(("plan", "run_plan", arguments)),
                    headers={"Content-Type": "text/event-stream"},
                )

            async with Llm(
                replace(configuration(tmp_path), extra_retries=0),
                transport=httpx.MockTransport(respond),
            ) as client:
                sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
            assert len(requests) == 1 and sample.proposal is not None
            return sample

        first = await sample_for('{"reason":"First route.","stretches":["s1","s2"]}')
        reordered = await sample_for('{"stretches":["s1","s2"],"reason":"Same route."}')
        different = await sample_for('{"reason":"Other route.","stretches":["s1","s3"]}')
        invalid = await sample_for('{"reason":"Unknown node.","stretches":["s999"]}')
        duplicate = await sample_for('{"reason":"Bad key.","stretches":["s1"],"stretches":["s2"]}')
        assert all(
            sample.verdict is DecisionVerdict.UNLABELED and sample.choices
            for sample in (first, reordered, different)
        )
        assert all(
            sample.proposal_status is ProposalStatus.VALID
            for sample in (first, reordered, different)
        )
        assert (
            DecisionSample.model_validate_json(first.model_dump_json()).proposal_status
            is ProposalStatus.VALID
        )
        assert first.choices == reordered.choices
        assert invalid.error is not None and "Invalid action proposal" in invalid.error
        assert duplicate.error is not None and "Invalid action proposal" in duplicate.error
        assert invalid.proposal_status is ProposalStatus.INVALID
        assert duplicate.proposal_status is ProposalStatus.INVALID
        plan = EvaluationPlan.model_validate_json(
            '{"configurations":[{"id":"one","effort":"high","prompt_version":"one"},'
            '{"id":"two","effort":"high","prompt_version":"two"}],'
            '"cases":[{"id":"q1","role":"run","case":"case.json",'
            '"catalog":"catalog.json","configurations":["one","two"],"repetitions":1}]}'
        )

        def trial(config: str, sample: DecisionSample) -> Trial:
            return Trial(
                case_id="q1",
                role=EvaluationRole.RUN,
                configuration=config,
                repetition=1,
                prompt_version=config,
                prompt_digest="digest",
                input_hashes={},
                sample=sample,
                error=None,
                attempts=(),
            )

        same_text = report(plan, (trial("one", first), trial("two", reordered)))
        assert "No differing comparable" in same_text
        assert "First route." in same_text and "Same route." in same_text
        different_text = report(plan, (trial("one", first), trial("two", different)))
        assert "chose" in different_text and "First route." in different_text
        assert "Other route." in different_text
        raw_first = first.model_copy(
            update={
                "choices": (),
                "verdict": DecisionVerdict.UNSUPPORTED,
                "error": "unknown",
                "proposal_status": ProposalStatus.UNKNOWN,
                "route_metrics": None,
            }
        )
        raw_reordered = reordered.model_copy(
            update={
                "choices": (),
                "verdict": DecisionVerdict.UNSUPPORTED,
                "error": "unknown",
                "proposal_status": ProposalStatus.UNKNOWN,
                "route_metrics": None,
            }
        )
        raw_different = different.model_copy(
            update={
                "choices": (),
                "verdict": DecisionVerdict.UNSUPPORTED,
                "error": "unknown",
                "proposal_status": ProposalStatus.UNKNOWN,
                "route_metrics": None,
            }
        )
        raw_same = report(plan, (trial("one", raw_first), trial("two", raw_reordered)))
        assert "No differing comparable" in raw_same
        assert "Proposal status" in raw_same
        raw_diff = report(plan, (trial("one", raw_first), trial("two", raw_different)))
        assert "declared" in raw_diff and "First route." in raw_diff and "Other route." in raw_diff
        for rejected in (invalid, duplicate):
            text = report(plan, (trial("one", first), trial("two", rejected)))
            assert "could not be compared" in text
            assert rejected.proposal is not None and rejected.proposal.to_json() in text
            assert rejected.reason in text
            with_choices = rejected.model_copy(update={"choices": ("untrusted",)})
            text = report(plan, (trial("one", first), trial("two", with_choices)))
            assert "could not be compared" in text
            other_tool = first.model_copy(
                update={
                    "proposal": CapturedProposal(
                        tool=ToolName.RUN_NEXT, arguments='{"reason":"Continue."}'
                    ),
                    "choices": (),
                    "verdict": DecisionVerdict.UNSUPPORTED,
                    "error": "unknown",
                    "proposal_status": ProposalStatus.UNKNOWN,
                    "route_metrics": None,
                }
            )
            text = report(plan, (trial("one", other_tool), trial("two", rejected)))
            assert "could not be compared" in text

        reworded = invalid.model_copy(update={"error": "A differently worded diagnostic."})
        text = report(plan, (trial("one", first), trial("two", reworded)))
        assert "could not be compared" in text and "differently worded diagnostic" in text
        misleading = raw_reordered.model_copy(
            update={"error": "Invalid action proposal: unrelated text"}
        )
        text = report(plan, (trial("one", raw_first), trial("two", misleading)))
        assert "No differing comparable" in text and "could not be compared" not in text

    asyncio.run(run())


def test_route_metrics_use_ordered_stretches_and_all_native_continuations(tmp_path: Path) -> None:
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, report

    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        state = observation.state
        assert isinstance(state, native.RunState)
        public_map = native.PublicMap.model_validate_json(
            '{"bosses":[],"nodes":['
            '{"position":"map:0:0","room_type":"ancient",'
            '"children":["map:1:0","map:1:1"]},'
            '{"position":"map:1:0","room_type":"unknown","children":["map:2:0"]},'
            '{"position":"map:1:1","room_type":"monster","children":["map:2:1"]},'
            '{"position":"map:2:0","room_type":"elite","children":["map:3:0"]},'
            '{"position":"map:2:1","room_type":"restsite","children":["map:3:0"]},'
            '{"position":"map:3:0","room_type":"restsite","children":["map:4:0"]},'
            '{"position":"map:4:0","room_type":"boss","children":[]}],'
            '"legal_destinations":["map:1:0","map:1:1"]}'
        )
        observation = observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "progress": state.progress.model_copy(update={"position": "map:0:0"}),
                        "player": state.player.model_copy(update={"hp": 25, "max_hp": 80}),
                    }
                ),
                "map": public_map,
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        requests: list[httpx.Request] = []
        responses = iter(
            (
                '{"reason":"Go.","stretches":["s2","s4"]}',
                '{"reason":"Rest first.","stretches":["s3","s4"]}',
                '{"reason":"Skip ahead.","stretches":["map:2:0","s4"]}',
                '{"reason":"Stop at first Boss.","stretches":["s2","s4"]}',
            )
        )

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("plan", "run_plan", next(responses))),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
            safe = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
            special_case = case.model_copy(
                update={
                    "events": (
                        task.model_copy(
                            update={
                                "observation": observation.model_copy(
                                    update={
                                        "map": public_map.model_copy(
                                            update={
                                                "legal_destinations": ("map:2:0",),
                                                "nodes": tuple(
                                                    node.model_copy(
                                                        update={
                                                            "room_type": "elite"
                                                            if node.position == "map:1:0"
                                                            else "unknown"
                                                        }
                                                    )
                                                    if node.position in ("map:1:0", "map:2:0")
                                                    else node
                                                    for node in public_map.nodes
                                                ),
                                            }
                                        )
                                    }
                                )
                            }
                        ),
                    )
                }
            )
            special = await sample_fresh(
                special_case, client, catalog=catalog, role=EvaluationRole.RUN
            )
            two_boss_map = public_map.model_copy(
                update={
                    "nodes": tuple(
                        node.model_copy(update={"children": ("map:5:0",)})
                        if node.position == "map:4:0"
                        else node
                        for node in public_map.nodes
                    )
                    + (
                        native.MapNode(
                            position="map:4:1", room_type="monster", children=("map:5:0",)
                        ),
                        native.MapNode(position="map:5:0", room_type="boss", children=()),
                    )
                }
            )
            two_boss_case = case.model_copy(
                update={
                    "events": (
                        task.model_copy(
                            update={
                                "observation": observation.model_copy(update={"map": two_boss_map})
                            }
                        ),
                    )
                }
            )
            first_boss = await sample_fresh(
                two_boss_case, client, catalog=catalog, role=EvaluationRole.RUN
            )
        assert len(requests) == 4 and sample.verdict is DecisionVerdict.UNLABELED
        assert sample.route_metrics is not None
        assert sample.route_metrics.model_dump() == {
            "full_route": True,
            "planned_elites": 1,
            "hp": 25,
            "max_hp": 80,
            "low_hp": True,
            "forced_elite_before_recovery": True,
            "bad_low_hp_route": True,
        }
        assert safe.route_metrics is not None
        assert safe.route_metrics.planned_elites == 0
        assert safe.route_metrics.forced_elite_before_recovery is False
        assert safe.route_metrics.bad_low_hp_route is False
        assert special.route_metrics is not None
        assert special.route_metrics.planned_elites == 0
        assert special.route_metrics.forced_elite_before_recovery is False
        assert first_boss.route_metrics is not None
        assert first_boss.route_metrics.full_route is False
        plan = EvaluationPlan.model_validate_json(
            '{"configurations":[{"id":"one","effort":"high","prompt_version":"one"}],'
            '"cases":[{"id":"q1","role":"run","case":"case.json",'
            '"catalog":"catalog.json","configurations":["one"],"repetitions":1}]}'
        )
        trial = Trial(
            case_id="q1",
            role=EvaluationRole.RUN,
            configuration="one",
            repetition=1,
            prompt_version="one",
            prompt_digest="digest",
            input_hashes={},
            sample=sample,
            error=None,
            attempts=(),
        )
        text = report(plan, (trial,))
        assert "complete route; 1 planned Elites; HP 25/80" in text
        assert "forced Elite before recovery yes; low-HP forced Elite yes" in text

    asyncio.run(run())


def test_proposal_and_status_must_be_supplied_together(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from sts2_harness_eval.decision_cases import CapturedProposal, DecisionSample
    from sts2_harness_eval.decision_replay import recorded

    case = decision_case(tmp_path)
    sample = recorded(case)
    assert sample.proposal is None and sample.proposal_status is None
    restored = DecisionSample.model_validate_json(sample.to_json())
    assert restored.proposal is None and restored.proposal_status is None
    with pytest.raises(ValidationError, match="proposal and its status"):
        DecisionSample(
            source=case.source,
            sample=1,
            verdict=DecisionVerdict.UNSUPPORTED,
            choices=(),
            reason="",
            proposal_status=ProposalStatus.VALID,
        )
    with pytest.raises(ValidationError, match="proposal and its status"):
        DecisionSample(
            source=case.source,
            sample=1,
            verdict=DecisionVerdict.UNSUPPORTED,
            choices=(),
            reason="",
            proposal=CapturedProposal(tool=ToolName.RUN_NEXT, arguments='{"reason":"Continue."}'),
        )

    proposal_data = sample.model_dump(mode="json")
    proposal_data["proposal"] = {"tool": ToolName.RUN_NEXT.value, "arguments": "{}"}
    with pytest.raises(ValidationError, match="proposal and its status"):
        DecisionSample.model_validate_json(json.dumps(proposal_data))
    proposal_data.pop("proposal_status")
    with pytest.raises(ValidationError, match="proposal and its status"):
        DecisionSample.model_validate_json(json.dumps(proposal_data))


def test_old_character_orb_slots_are_inferred_only_in_memory(tmp_path: Path) -> None:
    from sts2_harness_eval.stable_suite import CasePlan, prepare_case

    case_path = tmp_path / "case.json"
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        '{"complete":false,"catalog":{"categories":[],"card_filters":'
        '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
    )
    original: dict[str, JsonValue] = json.loads(decision_case(tmp_path).model_dump_json())
    events = original["events"]
    assert isinstance(events, list) and isinstance(events[0], dict)
    observation = events[0]["observation"]
    assert isinstance(observation, dict) and isinstance(observation["state"], dict)
    player = observation["state"]["player"]
    assert isinstance(player, dict)
    player["definition_id"] = "CHARACTER.IRONCLAD"
    player.pop("base_orb_slots")
    case_path.write_text(json.dumps(original))
    plan = CasePlan(
        id="old",
        role=EvaluationRole.DECK,
        case=Path("case.json"),
        catalog=Path("catalog.json"),
        configurations=("test",),
        repetitions=1,
    )
    prepared = prepare_case(plan, tmp_path)
    assert prepared.case is not None
    assert len(prepared.inferred) == 1 and prepared.inferred[0].value == 0
    assert "CHARACTER.IRONCLAD" in prepared.inferred[0].basis
    assert "base_orb_slots" not in case_path.read_text()
    task = prepared.case.events[0]
    assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
    assert task.observation.state.player.base_orb_slots == 0

    for identity, expected in (("CHARACTER.DEFECT", 3), ("CHARACTER.SILENT", 0)):
        player["definition_id"] = identity
        case_path.write_text(json.dumps(original))
        known = prepare_case(plan, tmp_path)
        assert known.case is not None and len(known.inferred) == 1
        assert known.inferred[0].value == expected and identity in known.inferred[0].basis

    player["definition_id"] = "CHARACTER.UNKNOWN"
    case_path.write_text(json.dumps(original))
    unknown = prepare_case(plan, tmp_path)
    assert unknown.case is None and not unknown.inferred
    assert unknown.error is not None and "base_orb_slots" in unknown.error


def test_fresh_deck_scores_multiple_chosen_cards(tmp_path: Path) -> None:
    from sts2_harness_eval.decision_cases import DecisionLabels

    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        first_card = observation.objects["candidate"]
        assert isinstance(first_card, native.CardEntry)
        second_card = first_card.model_copy(update={"definition_id": "CARD.SECOND"})
        offer = observation.state.offers[0].model_copy(
            update={"contents": ("candidate", "candidate-2")}
        )
        updated = observation.model_copy(
            update={
                "state": observation.state.model_copy(
                    update={
                        "screen": native.RunScreen.SELECTION,
                        "offers": (offer,),
                        "selection": native.Selection(
                            prompt="Choose two",
                            source=None,
                            min=2,
                            max=2,
                            candidates=("candidate", "candidate-2"),
                            selected=(),
                        ),
                    }
                ),
                "objects": {**observation.objects, "candidate-2": second_card},
            }
        )
        case = original.model_copy(
            update={
                "events": (task.model_copy(update={"observation": updated}),),
                "decision_offer": None,
                "labels": DecisionLabels(
                    good=(first_card.definition_id, "CARD.SECOND"), comment="both", multiselect=True
                ),
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        response = reply(
            (
                "choice",
                "run_deck_choice",
                json.dumps(
                    {
                        "choice": "take",
                        "card": ["candidate", "candidate-2"],
                        "reason": "Take both.",
                    }
                ),
            )
        )
        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=response, headers={"Content-Type": "text/event-stream"}
                )
            ),
        ) as client:
            result = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.DECK)
        assert result.verdict is DecisionVerdict.PASSED, result.error
        assert result.choices == (first_card.definition_id, "CARD.SECOND")

    asyncio.run(run())


@pytest.mark.parametrize(
    "control, readiness, expected",
    (
        (native.StateControl.READY, native.InputReadiness.READY, DecisionVerdict.PASSED),
        (native.StateControl.READY, native.InputReadiness.SELECTION, DecisionVerdict.PASSED),
        (native.StateControl.BUSY, native.InputReadiness.BUSY, DecisionVerdict.UNSUPPORTED),
        (
            native.StateControl.UNAVAILABLE,
            native.InputReadiness.UNAVAILABLE,
            DecisionVerdict.UNSUPPORTED,
        ),
    ),
)
def test_deck_choice_requires_ready_control_and_input(
    tmp_path: Path,
    control: native.StateControl,
    readiness: native.InputReadiness,
    expected: DecisionVerdict,
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        observation = observation.model_copy(
            update={
                "input_readiness": readiness,
                "state": observation.state.model_copy(
                    update={"control": control, "screen": native.RunScreen.SELECTION}
                ),
            }
        )
        observation = native.Observation.model_validate_json(observation.model_dump_json())
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"choice":"take","card":"candidate","reason":"Choose it."}'
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("choice", "run_deck_choice", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.DECK)
        assert len(requests) == 1 and sample.verdict is expected
        assert sample.proposal is not None and sample.proposal.arguments == arguments
        assert sample.reason == "Choose it."
        if expected is DecisionVerdict.UNSUPPORTED:
            assert sample.error is not None and "not ready" in sample.error
            assert not sample.choices

    asyncio.run(run())


def test_frozen_documents_preserve_material_identity_and_query_coverage() -> None:
    from pydantic import ValidationError

    from sts2_harness.knowledge import Document, SkillRead, SubjectId
    from sts2_harness.material_text import MaterialText
    from sts2_harness_eval.frozen_documents import FrozenDocuments

    subject = SubjectId(category="card", definition_id="CARD.KNOWN")
    document = Document(
        id="doc",
        name=MaterialText.create("name-source", "Name"),
        text=MaterialText.create("text-source", "Known guidance"),
        resources=(),
        subjects=(subject,),
    )
    snapshot = FrozenDocuments(
        complete_subjects=False, covered_subjects=(subject,), documents=(document,)
    )
    encoded = snapshot.snapshot_json()
    restored = FrozenDocuments.model_validate_json(encoded)
    assert restored.documents[0].text.id == "text-source"
    assert restored.documents[0].text.version == document.text.version
    assert restored.documents[0].text.body == "Known guidance"

    async def run() -> None:
        assert await restored.lookup((subject,)) == (document,)
        assert (await restored.read(SkillRead("doc"))).text.body == "Known guidance"
        with pytest.raises(SnapshotIncomplete):
            await restored.lookup((SubjectId(category="card", definition_id="CARD.UNKNOWN"),))

    asyncio.run(run())
    corrupt = encoded.replace(document.text.version, "forged")
    with pytest.raises(ValidationError, match="version does not match"):
        FrozenDocuments.model_validate_json(corrupt)


@pytest.mark.parametrize(
    "first_reply, expected_error",
    [
        (reply(text="I need more information."), "no tool"),
        (reply(("unknown", "run_missing", "{}")), "rejected or failed"),
        (reply(("bad-query", "run_knowledge", '{"mode":"lookup","ids":[]}')), "rejected or failed"),
        (
            reply(("missing", "run_knowledge", '{"mode":"search","text":"strength"}')),
            "complete native catalog",
        ),
    ],
)
def test_invalid_preparatory_response_ends_only_its_trial_and_keeps_usage(
    tmp_path: Path, first_reply: bytes, expected_error: str
) -> None:
    from pydantic import TypeAdapter

    from sts2_harness_eval.stable_suite import EvaluationPlan, evaluate

    case = decision_case(tmp_path, multiselect=True)
    task = case.events[0]
    assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
    observation = task.observation
    assert isinstance(observation.state, native.RunState)
    state = observation.state.model_copy(
        update={"screen": native.RunScreen.SELECTION, "offers": (), "actions": ()}
    )
    case = case.model_copy(
        update={
            "events": (
                task.model_copy(
                    update={"observation": observation.model_copy(update={"state": state})}
                ),
            )
        }
    )
    tmp_path.joinpath("case.json").write_text(case.model_dump_json())
    tmp_path.joinpath("catalog.json").write_text(
        '{"complete":false,"catalog":{"categories":[],"card_filters":'
        '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
    )
    plan = EvaluationPlan.model_validate_json(
        json.dumps(
            {
                "configurations": [{"id": "test", "effort": "high", "prompt_version": "test"}],
                "cases": [
                    {
                        "id": "case",
                        "role": "run",
                        "case": "case.json",
                        "catalog": "catalog.json",
                        "configurations": ["test"],
                        "repetitions": 2,
                    }
                ],
            }
        )
    )
    event = TypeAdapter(dict[str, JsonValue]).validate_json(first_reply.removeprefix(b"data: "))
    response = event["response"]
    assert isinstance(response, dict)
    response["usage"] = {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 40},
        "output_tokens_details": {"reasoning_tokens": 12},
    }
    first_with_usage = ("data: " + json.dumps(event) + "\n\n").encode()
    action = '{"steps":[{"kind":"select","refs":["candidate"]}],"reason":"Second trial."}'
    responses = iter((first_with_usage, reply(("action", "run_act", action))))
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, content=next(responses), headers={"Content-Type": "text/event-stream"}
        )

    async def run() -> None:
        results = await evaluate(
            plan,
            tmp_path,
            tmp_path / "out",
            configuration(tmp_path),
            concurrency=1,
            transport=lambda: httpx.MockTransport(respond),
        )
        assert len(requests) == 2 and len(results) == 2
        first, second = results
        assert first.sample is not None and first.sample.verdict is DecisionVerdict.UNSUPPORTED
        assert first.sample.error is not None and expected_error in first.sample.error
        assert first.sample.proposal is None and first.sample.proposal_status is None
        assert len(first.attempts) == 1 and first.attempts[0].weighted_tokens == 162
        assert first.attempts[0].reasoning_tokens == 12
        assert second.sample is not None and second.sample.verdict is DecisionVerdict.PASSED
        assert second.sample.proposal_status is ProposalStatus.VALID
        assert second.sample.reason == "Second trial."

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments, reason",
    (
        ('{"offer":"offer","choice":"take","reason":"Missing card."}', "Missing card."),
        (
            '{"offer":"offer","choice":"take","card":"candidate","card":"candidate",'
            '"reason":"Duplicate card."}',
            "Duplicate card.",
        ),
        (
            '{"offer":"offer","choice":"take","card":"candidate",'
            '"reason":"First.","reason":"Second."}',
            "",
        ),
        (
            '{"offer":"offer","choice":"take","card":"candidate",'
            '"extra":{"reason":"First.","reason":"Second."},"reason":"Top level."}',
            "Top level.",
        ),
    ),
)
def test_invalid_first_proposal_is_retained_without_execution(
    tmp_path: Path, arguments: str, reason: str
) -> None:
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, report

    async def run() -> None:
        case = decision_case(tmp_path)
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        count = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal count
            count += 1
            return httpx.Response(
                200,
                content=reply(("bad", "run_deck_choice", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.DECK)
        assert count == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "Invalid action proposal" in sample.error
        assert sample.raw_calls[0]["arguments"] == arguments
        assert sample.reason == reason
        assert sample.proposal is not None
        assert sample.proposal_status is ProposalStatus.INVALID
        assert sample.proposal.tool is ToolName.RUN_DECK_CHOICE
        assert sample.proposal.arguments == arguments
        plan = EvaluationPlan.model_validate_json(
            '{"configurations":[{"id":"one","effort":"high","prompt_version":"one"}],'
            '"cases":[{"id":"q1","role":"deck","case":"case.json",'
            '"catalog":"catalog.json","configurations":["one"],"repetitions":1}]}'
        )
        trial = Trial(
            case_id="q1",
            role=EvaluationRole.DECK,
            configuration="one",
            repetition=1,
            prompt_version="one",
            prompt_digest="digest",
            input_hashes={},
            error=None,
            sample=sample,
            attempts=(),
        )
        result = report(plan, (trial,))
        assert "run_deck_choice" in result and reason in result
        assert json.dumps(arguments, ensure_ascii=False) in result
        assert "Invalid action proposal" in result and "Scored: 0" in result

    asyncio.run(run())


@pytest.mark.parametrize("role", (EvaluationRole.DECK, EvaluationRole.RUN))
def test_schema_valid_but_illegal_choice_is_not_scored(
    tmp_path: Path, role: EvaluationRole
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=role is EvaluationRole.RUN)
        if role is EvaluationRole.RUN:
            task = case.events[0]
            assert isinstance(task, TaskEvent) and isinstance(
                task.observation.state, native.RunState
            )
            observation = task.observation
            assert isinstance(observation.state, native.RunState)
            observation = observation.model_copy(
                update={
                    "state": observation.state.model_copy(
                        update={"screen": native.RunScreen.SELECTION}
                    )
                }
            )
            case = case.model_copy(
                update={"events": (task.model_copy(update={"observation": observation}),)}
            )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        if role is EvaluationRole.DECK:
            name = "run_deck_choice"
            arguments = '{"offer":"WRONG-OFFER","choice":"take","card":"candidate","reason":"Try."}'
        else:
            name = "run_act"
            arguments = '{"steps":[{"kind":"select","refs":["owned"]}],"reason":"Try."}'
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("action", name, arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=role)
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "Invalid action proposal" in sample.error
        assert sample.proposal is not None and sample.proposal.arguments == arguments
        assert sample.reason == "Try." and not sample.choices

    asyncio.run(run())


@pytest.mark.parametrize("destination", (None, "0:1"))
def test_map_non_navigation_interaction_is_not_rejected_as_travel(
    tmp_path: Path, destination: str | None
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        state = observation.state
        assert isinstance(state, native.RunState)
        action = native.Action(ref="action", label="Interact", enabled=True, subject="candidate")
        if destination is not None:
            action = action.model_copy(update={"destination": destination})
        observation = observation.model_copy(
            update={
                "input_readiness": native.InputReadiness.READY,
                "state": state.model_copy(
                    update={
                        "screen": native.RunScreen.MAP,
                        "selection": None,
                        "offers": (),
                        "actions": (action,),
                    }
                ),
            }
        )
        observation = native.Observation.model_validate_json(observation.model_dump_json())
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"steps":[{"kind":"interact","action":"action"}],"reason":"Interact."}'
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("action", "run_act", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
        assert len(requests) == 1 and sample.proposal is not None
        assert sample.proposal.arguments == arguments and sample.reason == "Interact."
        if destination is None:
            assert sample.verdict is DecisionVerdict.PASSED and sample.error is None
        else:
            assert sample.verdict is DecisionVerdict.UNSUPPORTED
            assert sample.error is not None and "Invalid action proposal" in sample.error

    asyncio.run(run())


@pytest.mark.parametrize("role", (EvaluationRole.RUN, EvaluationRole.COMBAT))
@pytest.mark.parametrize(
    "selection_order", ("first_invalid", "first_valid", "later_invalid", "selection_required")
)
def test_only_initial_selection_is_checked_against_frozen_state(
    tmp_path: Path, role: EvaluationRole, selection_order: str
) -> None:
    from sts2_harness.game import Inspect

    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent)
        observation = (
            task.observation
            if role is EvaluationRole.RUN
            else native.Observation.model_validate_json(COMBAT)
        )
        selection = native.Selection(
            prompt="Choose", source=None, min=1, max=1, candidates=("candidate",), selected=()
        )
        state = observation.state.model_copy(update={"selection": selection})
        if isinstance(state, native.RunState):
            state = state.model_copy(update={"screen": native.RunScreen.SELECTION})
        observation = observation.model_copy(
            update={
                "input_readiness": (
                    native.InputReadiness.READY
                    if selection_order == "later_invalid"
                    else native.InputReadiness.SELECTION
                ),
                "state": state,
            }
        )
        observation = native.Observation.model_validate_json(observation.model_dump_json())
        reads: tuple[NativeRead, ...] = ()
        if role is EvaluationRole.COMBAT:
            history = native.HistoryData(
                cards_played_this_combat=(), cards_played_this_turn=(), cards_exhausted_this_turn=()
            )
            reads = (
                NativeRead(
                    request=Inspect(query=native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)),
                    result=native.InspectCompleted(
                        status=native.ResultStatus.COMPLETED, observation=observation, data=history
                    ),
                ),
            )
        case = case.model_copy(
            update={
                "events": (task.model_copy(update={"observation": observation}),),
                "native_reads": reads,
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        choice = "candidate" if selection_order == "first_valid" else "NOT-A-CANDIDATE"
        select: dict[str, JsonValue] = {"kind": "select", "refs": [choice]}
        other: dict[str, JsonValue] = (
            {"kind": "interact", "action": "open"}
            if role is EvaluationRole.RUN
            else {"kind": "end_turn"}
        )
        steps = (
            [other, select]
            if selection_order in ("later_invalid", "selection_required")
            else [select, other]
        )
        arguments = json.dumps({"steps": steps, "reason": "First and later."})
        name = "run_act" if role is EvaluationRole.RUN else "combat_act"
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("action", name, arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=role)
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        expected = (
            "Invalid action proposal"
            if selection_order in ("first_invalid", "selection_required")
            else "legality unknown"
        )
        assert sample.error is not None and expected in sample.error
        assert sample.proposal is not None and sample.proposal.arguments == arguments
        assert sample.reason == "First and later." and not sample.choices

    asyncio.run(run())


@pytest.mark.parametrize("role", (EvaluationRole.RUN, EvaluationRole.COMBAT))
def test_missing_selection_respects_combat_auto_selection_uncertainty(
    tmp_path: Path, role: EvaluationRole
) -> None:
    from sts2_harness.game import Inspect

    async def run() -> None:
        case = decision_case(tmp_path)
        task = case.events[0]
        assert isinstance(task, TaskEvent)
        observation = (
            task.observation
            if role is EvaluationRole.RUN
            else native.Observation.model_validate_json(COMBAT)
        )
        state = observation.state.model_copy(update={"selection": None})
        observation = observation.model_copy(
            update={"input_readiness": native.InputReadiness.READY, "state": state}
        )
        observation = native.Observation.model_validate_json(observation.model_dump_json())
        reads: tuple[NativeRead, ...] = ()
        if role is EvaluationRole.COMBAT:
            history = native.HistoryData(
                cards_played_this_combat=(), cards_played_this_turn=(), cards_exhausted_this_turn=()
            )
            reads = (
                NativeRead(
                    request=Inspect(query=native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)),
                    result=native.InspectCompleted(
                        status=native.ResultStatus.COMPLETED, observation=observation, data=history
                    ),
                ),
            )
        case = case.model_copy(
            update={
                "events": (task.model_copy(update={"observation": observation}),),
                "native_reads": reads,
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"steps":[{"kind":"select","refs":["c1"]}],"reason":"Select."}'
        name = "run_act" if role is EvaluationRole.RUN else "combat_act"
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("action", name, arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=role)
        expected = (
            "Proposal legality unknown"
            if role is EvaluationRole.COMBAT
            else "Invalid action proposal"
        )
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and expected in sample.error
        assert sample.proposal is not None and sample.proposal.arguments == arguments
        assert sample.reason == "Select." and not sample.choices

    asyncio.run(run())


def test_failed_catalog_query_ends_the_trial_without_a_correction(tmp_path: Path) -> None:
    from sts2_harness.client.http import ClientFailureKind, ClientUnavailable

    class UnavailableCatalog(FrozenCatalog):
        async def query(
            self, request: native.CatalogRequest
        ) -> native.CatalogCompleted | native.CatalogRejected:
            raise ClientUnavailable(ClientFailureKind.TIMEOUT)

    async def run() -> None:
        catalog = UnavailableCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("query", "run_knowledge", '{"mode":"catalog"}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(
                decision_case(tmp_path, multiselect=True),
                client,
                catalog=catalog,
                role=EvaluationRole.RUN,
            )
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "rejected or failed" in sample.error
        assert sample.proposal is None and sample.proposal_status is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "query, error",
    (
        ('{"mode":"lookup","ids":[]}', "rejected or failed"),
        ('{"mode":"search","text":"strength"}', "complete native catalog"),
    ),
)
def test_failed_query_retains_later_proposal_without_executing_it(
    tmp_path: Path, query: str, error: str
) -> None:
    from pydantic import TypeAdapter

    from sts2_harness_eval.decision_cases import DecisionSample

    async def run() -> None:
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"reason":"After the failed query."}'
        payload = reply(("query", "run_knowledge", query), ("later", "run_next", arguments))
        event = TypeAdapter(dict[str, JsonValue]).validate_json(payload.removeprefix(b"data: "))
        response = event["response"]
        assert isinstance(response, dict)
        response["usage"] = {"input_tokens": 100, "output_tokens": 20}
        payload = ("data: " + json.dumps(event) + "\n\n").encode()
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=payload,
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(
                decision_case(tmp_path, multiselect=True),
                client,
                catalog=catalog,
                role=EvaluationRole.RUN,
            )
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and error in sample.error
        assert sample.proposal is not None and sample.proposal.tool is ToolName.RUN_NEXT
        assert sample.proposal_status is ProposalStatus.UNKNOWN
        assert (
            DecisionSample.model_validate_json(sample.model_dump_json()).proposal_status
            is ProposalStatus.UNKNOWN
        )
        assert sample.proposal.arguments == arguments and sample.reason == "After the failed query."
        assert len(sample.raw_calls) == 2 and not sample.choices
        assert sample.usage[0]["input_tokens"] == 100 and sample.usage[0]["output_tokens"] == 20

    asyncio.run(run())


def test_rejected_action_layout_keeps_unchecked_proposal_unknown(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        observation = observation.model_copy(
            update={
                "state": observation.state.model_copy(update={"screen": native.RunScreen.SELECTION})
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"steps":[{"kind":"select","refs":["candidate"]}],"reason":"Take."}'
        responses = iter(
            (
                reply(
                    ("action", "run_act", arguments),
                    ("later", "run_notes", '{"mode":"read"}'),
                ),
                reply(("action", "run_act", arguments)),
            )
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=next(responses),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
            assert len(requests) == 1
            control = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
        assert len(requests) == 2 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.proposal is not None and sample.proposal.arguments == arguments
        assert sample.proposal_status is ProposalStatus.UNKNOWN and not sample.choices
        assert sample.error is not None and "action layout" in sample.error
        assert control.proposal is not None and control.proposal.arguments == arguments
        assert control.proposal_status is ProposalStatus.VALID
        assert control.verdict is DecisionVerdict.PASSED

    asyncio.run(run())


def test_production_combat_still_corrects_text_only_reply(tmp_path: Path) -> None:
    from test_combat import BattleHTTP

    from sts2_harness.decisions import CombatHandoff

    battle = BattleHTTP(
        [
            reply(text="I need a moment."),
            reply(("action", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            reply(text="Reviewed."),
        ]
    )
    assert isinstance(asyncio.run(battle.run(tmp_path)), CombatHandoff)
    assert len(battle.inputs) == 3 and len(battle.actions) == 1


@pytest.mark.parametrize("history_source", ("facts", "native_read"))
def test_combat_task_and_history_query_use_the_same_frozen_history(
    tmp_path: Path, history_source: str
) -> None:
    from pydantic import TypeAdapter

    from sts2_harness.game import Inspect

    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent)
        observation = native.Observation.model_validate_json(COMBAT)
        assert isinstance(observation.state, native.CombatState)
        observation = observation.model_copy(
            update={
                "input_readiness": native.InputReadiness.READY,
                "state": observation.state.model_copy(
                    update={"screen": native.CombatScreen.COMBAT, "selection": None}
                ),
            }
        )
        observation = native.Observation.model_validate_json(observation.model_dump_json())
        history = native.HistoryData(
            cards_played_this_combat=(
                native.NamedDefinition(name="Strike", definition_id="CARD.STRIKE"),
                native.NamedDefinition(name="Defend", definition_id="CARD.DEFEND"),
            ),
            cards_played_this_turn=(
                native.NamedDefinition(name="Defend", definition_id="CARD.DEFEND"),
            ),
            cards_exhausted_this_turn=(
                native.NamedDefinition(name="Burn", definition_id="CARD.BURN"),
            ),
        )
        query = native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)
        read = NativeRead(
            request=Inspect(query=query),
            result=native.InspectCompleted(
                status=native.ResultStatus.COMPLETED, observation=observation, data=history
            ),
        )
        case = original.model_copy(
            update={
                "events": (
                    CorrectionEvent(message="OLD-CONVERSATION-SENTINEL"),
                    task.model_copy(
                        update={
                            "observation": observation,
                            "state": HarnessState(notes={"core": "OLD-NOTES-SENTINEL"}),
                        }
                    ),
                ),
                "combat_history": history if history_source == "facts" else None,
                "native_reads": (read,),
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        first_read = await FreshGame(case, observation, catalog).inspect_result(query)
        assert isinstance(first_read, native.InspectCompleted) and first_read.data == history
        requests: list[dict[str, JsonValue]] = []
        responses = iter(
            (
                reply(("history", "combat_inspect", '{"area":"combat_history"}')),
                reply(("action", "combat_act", '{"steps":[{"kind":"end_turn"}]}')),
            )
        )

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
            return httpx.Response(
                200, content=next(responses), headers={"Content-Type": "text/event-stream"}
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.COMBAT)
        assert len(requests) == 2 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "legality cannot be verified" in sample.error
        first = requests[0]["input"]
        assert isinstance(first, list)
        user = [item for item in first if isinstance(item, dict) and item.get("role") == "user"]
        assert len(user) == 1 and isinstance(user[0]["content"], str)
        task_body = TypeAdapter(dict[str, JsonValue]).validate_json(user[0]["content"])
        task_observation = task_body["observation"]
        assert isinstance(task_observation, dict) and task_observation["baseline"] is True
        assert task_observation["combat_history"] == json.loads(history.to_json())
        assert "OLD-CONVERSATION-SENTINEL" not in json.dumps(requests)
        assert "OLD-NOTES-SENTINEL" not in json.dumps(requests)
        second = requests[1]["input"]
        assert isinstance(second, list)
        outputs = [
            item
            for item in second
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        assert len(outputs) == 1 and outputs[0]["call_id"] == "history"
        assert isinstance(outputs[0]["output"], str)
        result = TypeAdapter(dict[str, JsonValue]).validate_json(outputs[0]["output"])
        assert result["data"] == json.loads(history.to_json())

    asyncio.run(run())


@pytest.mark.parametrize("inconsistent", (False, True))
def test_missing_or_inconsistent_combat_history_is_unknown_without_a_request(
    tmp_path: Path, inconsistent: bool
) -> None:
    from sts2_harness.game import Inspect

    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent)
        observation = native.Observation.model_validate_json(COMBAT)
        history = native.HistoryData(
            cards_played_this_combat=(), cards_played_this_turn=(), cards_exhausted_this_turn=()
        )
        different = history.model_copy(
            update={"cards_played_this_combat": (native.NamedDefinition(name="Recorded"),)}
        )
        read = NativeRead(
            request=Inspect(query=native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)),
            result=native.InspectCompleted(
                status=native.ResultStatus.COMPLETED, observation=observation, data=different
            ),
        )
        case = original.model_copy(
            update={
                "events": (task.model_copy(update={"observation": observation}),),
                "combat_history": history if inconsistent else None,
                "native_reads": (read,) if inconsistent else (),
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.COMBAT)
        assert sample.verdict is DecisionVerdict.UNSUPPORTED and sample.proposal is None
        assert sample.error is not None and "combat history" in sample.error
        assert not requests

    asyncio.run(run())


@pytest.mark.parametrize("reverse", (False, True))
def test_conflicting_current_history_reads_are_unknown_without_a_request(
    tmp_path: Path, reverse: bool
) -> None:
    from sts2_harness.game import Inspect

    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent)
        observation = native.Observation.model_validate_json(COMBAT)
        query = native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)
        reads = tuple(
            NativeRead(
                request=Inspect(query=query),
                result=native.InspectCompleted(
                    status=native.ResultStatus.COMPLETED,
                    observation=observation,
                    data=native.HistoryData(
                        cards_played_this_combat=(native.NamedDefinition(name=name),),
                        cards_played_this_turn=(),
                        cards_exhausted_this_turn=(),
                    ),
                ),
            )
            for name in ("First", "Second")
        )
        case = original.model_copy(
            update={
                "events": (task.model_copy(update={"observation": observation}),),
                "native_reads": reads[::-1] if reverse else reads,
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.COMBAT)
        assert sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "inconsistent" in sample.error
        assert not requests

    asyncio.run(run())


@pytest.mark.parametrize("has_stale_read", (False, True))
def test_combat_history_requires_an_exact_inspect_anchor(
    tmp_path: Path, has_stale_read: bool
) -> None:
    from sts2_harness.game import Inspect

    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent)
        observation = native.Observation.model_validate_json(COMBAT)
        history = native.HistoryData(
            cards_played_this_combat=(), cards_played_this_turn=(), cards_exhausted_this_turn=()
        )
        stale_observation = observation.model_copy(
            update={
                "stamp": observation.stamp.model_copy(
                    update={"observation_sequence": observation.stamp.observation_sequence + 1}
                )
            }
        )
        read = NativeRead(
            request=Inspect(query=native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)),
            result=native.InspectCompleted(
                status=native.ResultStatus.COMPLETED, observation=stale_observation, data=history
            ),
        )
        case = original.model_copy(
            update={
                "events": (task.model_copy(update={"observation": observation}),),
                "combat_history": history,
                "native_reads": (read,) if has_stale_read else (),
            }
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.COMBAT)
        assert sample.verdict is DecisionVerdict.UNSUPPORTED and sample.proposal is None
        assert sample.error is not None and "exactly anchored" in sample.error
        assert not requests

    asyncio.run(run())


def test_other_history_anchor_does_not_conflict_with_exact_read(tmp_path: Path) -> None:
    from sts2_harness.game import Inspect

    async def run() -> None:
        case = decision_case(tmp_path)
        observation = native.Observation.model_validate_json(COMBAT)
        history = native.HistoryData(
            cards_played_this_combat=(), cards_played_this_turn=(), cards_exhausted_this_turn=()
        )
        different = history.model_copy(
            update={"cards_played_this_combat": (native.NamedDefinition(name="Other"),)}
        )
        stale = observation.model_copy(
            update={
                "stamp": observation.stamp.model_copy(
                    update={"observation_sequence": observation.stamp.observation_sequence + 1}
                )
            }
        )
        query = native.MapQuery(area=native.InspectArea.COMBAT_HISTORY)
        reads = tuple(
            NativeRead(
                request=Inspect(query=query),
                result=native.InspectCompleted(
                    status=native.ResultStatus.COMPLETED, observation=anchor, data=data
                ),
            )
            for anchor, data in ((observation, history), (stale, different))
        )
        case = case.model_copy(update={"native_reads": reads, "combat_history": history})
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        result = await FreshGame(case, observation, catalog).inspect_result(query)
        assert isinstance(result, native.InspectCompleted) and result.data == history

    asyncio.run(run())


def test_run_next_without_a_plan_is_captured_as_invalid(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"reason":"Continue."}'
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("next", "run_next", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(
                decision_case(tmp_path, multiselect=True),
                client,
                catalog=catalog,
                role=EvaluationRole.RUN,
            )
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        from sts2_harness.results import ReasonCode

        assert sample.error is not None and ReasonCode.NO_PLAN.value in sample.error
        assert sample.proposal is not None and sample.proposal.tool is ToolName.RUN_NEXT
        assert sample.proposal.arguments == arguments and sample.reason == "Continue."

    asyncio.run(run())


@pytest.mark.parametrize(
    "destination, expected",
    (("s2", DecisionVerdict.UNSUPPORTED), ("s1", DecisionVerdict.UNLABELED)),
)
def test_run_plan_uses_frozen_map_to_distinguish_invalid_from_accepted(
    tmp_path: Path, destination: str, expected: DecisionVerdict
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        observation = task.observation
        assert isinstance(observation.state, native.RunState)
        observation = observation.model_copy(
            update={
                "state": observation.state.model_copy(
                    update={
                        "progress": observation.state.progress.model_copy(
                            update={"position": "0:0"}
                        )
                    }
                ),
                "map": native.PublicMap(
                    bosses=(),
                    nodes=(
                        native.MapNode(position="0:0", room_type="monster", children=("1:0",)),
                        native.MapNode(position="1:0", room_type="elite", children=()),
                    ),
                    legal_destinations=("1:0",),
                ),
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = json.dumps({"reason": "Plan ahead.", "stretches": [destination]})
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("plan", "run_plan", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
        assert len(requests) == 1 and sample.verdict is expected
        if expected is DecisionVerdict.UNSUPPORTED:
            assert sample.error is not None and "Invalid action proposal" in sample.error
            assert not sample.choices
        else:
            assert sample.error is None and destination in sample.choices[0]
        assert sample.proposal is not None and sample.proposal.tool is ToolName.RUN_PLAN
        assert sample.proposal.arguments == arguments and sample.reason == "Plan ahead."

    asyncio.run(run())


def test_first_run_plan_is_captured_without_execution_or_a_second_request(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"reason":"Reach the elite.","stretches":["s1"]}'
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("plan", "run_plan", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "unknown" in sample.error
        assert sample.reason == "Reach the elite."
        assert sample.proposal is not None
        assert sample.proposal.tool is ToolName.RUN_PLAN and sample.proposal.arguments == arguments

    asyncio.run(run())


def test_run_plan_position_absent_from_frozen_map_is_unknown(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        state = task.observation.state
        observation = task.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={"progress": state.progress.model_copy(update={"position": "map:9:9"})}
                ),
                "map": native.PublicMap(
                    bosses=(),
                    nodes=(native.MapNode(position="map:1:0", room_type="elite", children=()),),
                    legal_destinations=("map:1:0",),
                ),
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("plan", "run_plan", '{"reason":"Try.","stretches":["s1"]}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            replace(configuration(tmp_path), extra_retries=0),
            transport=httpx.MockTransport(respond),
        ) as client:
            sample = await sample_fresh(case, client, catalog=catalog, role=EvaluationRole.RUN)
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.proposal_status is ProposalStatus.UNKNOWN
        assert sample.route_metrics is None and not sample.choices
        assert sample.error is not None and "unknown_position" in sample.error

    asyncio.run(run())
