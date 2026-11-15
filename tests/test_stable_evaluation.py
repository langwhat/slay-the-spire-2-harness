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
from test_responses import configuration

from sts2_harness.client import models as native
from sts2_harness.llm.client import Llm
from sts2_harness_eval.decision_cases import DecisionVerdict, HarnessState, TaskEvent
from sts2_harness_eval.frozen_catalog import FrozenCatalog, SnapshotIncomplete
from sts2_harness_eval.stable_decisions import EvaluationRole, sample_fresh
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
        with pytest.raises(SnapshotIncomplete):
            await snapshot.query(
                native.LookupQuery(mode=native.CatalogMode.LOOKUP, ids=("RELIC.UNKNOWN",))
            )
        with pytest.raises(SnapshotIncomplete):
            await snapshot.model_copy(update={"complete": False}).query(
                native.SearchQuery(mode=native.CatalogMode.SEARCH, text="strength")
            )

    asyncio.run(run())


def test_fresh_decision_uses_production_task_without_old_notes_or_report(tmp_path: Path) -> None:
    async def run() -> None:
        original = decision_case(tmp_path)
        task = original.events[0]
        assert isinstance(task, TaskEvent)
        case = original.model_copy(
            update={
                "recorded_reason": "OLD-REASON-SENTINEL",
                "events": (
                    task.model_copy(
                        update={"state": HarnessState(notes={"core": "OLD-NOTES-SENTINEL"})}
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


def test_fresh_run_continues_after_text_notes_and_a_new_catalog_query(tmp_path: Path) -> None:
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
                reply(text="I will inspect first."),
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
        assert len(bodies) == 4
        assert "Gain strength." in bodies[2]
        assert "Test note." in bodies[3]
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
    assert estimate(plan).requests is None
    assert estimate(plan).missing_means == ("deck/high",)


def test_old_ironclad_orb_slots_are_inferred_only_in_memory(tmp_path: Path) -> None:
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

    player["definition_id"] = "CHARACTER.UNKNOWN"
    case_path.write_text(json.dumps(original))
    unknown = prepare_case(plan, tmp_path)
    assert unknown.case is None and not unknown.inferred
    assert unknown.error is not None and "base_orb_slots" in unknown.error
