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
from sts2_harness_eval.decision_cases import (
    BattleState,
    CorrectionEvent,
    DecisionVerdict,
    HarnessState,
    NativeRead,
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
        for text in ("Straße", "İ", "ı", "ſ", "K", "ﬀ", "\ud800", "\u0378"):
            unsafe = snapshot.model_copy(
                update={"definitions": (snapshot.definitions[0].model_copy(update={"name": text}),)}
            )
            with pytest.raises(SnapshotIncomplete):
                await unsafe.query(native.SearchQuery(mode=native.CatalogMode.SEARCH, text="力量"))
        definition = snapshot.definitions[0]
        unsafe_description = snapshot.model_copy(
            update={
                "definitions": (
                    definition.model_copy(
                        update={"native": definition.native.model_copy(update={"text": "Straße"})}
                    ),
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
    assert "No differing choices observed." in report(
        plan, (failed, trial("two", "skip", "Second reason"))
    )


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
                "state": observation.state.model_copy(update={"offers": (offer,)}),
                "objects": {**observation.objects, "candidate-2": second_card},
            }
        )
        case = original.model_copy(
            update={
                "events": (task.model_copy(update={"observation": updated}),),
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
                        "offer": "offer",
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
        assert len(first.attempts) == 1 and first.attempts[0].weighted_tokens == 162
        assert first.attempts[0].reasoning_tokens == 12
        assert second.sample is not None and second.sample.verdict is DecisionVerdict.PASSED
        assert second.sample.reason == "Second trial."

    asyncio.run(run())


def test_invalid_first_proposal_is_retained_without_execution(tmp_path: Path) -> None:
    from sts2_harness_eval.stable_suite import EvaluationPlan, Trial, report

    async def run() -> None:
        case = decision_case(tmp_path)
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"offer":"offer","choice":"take","reason":"Missing card."}'
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
        assert sample.reason == "Missing card."
        assert sample.proposal is not None
        assert sample.proposal.tool == "run_deck_choice"
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
        assert "run_deck_choice" in result and "Missing card." in result
        assert json.dumps(arguments, ensure_ascii=False) in result
        assert "Invalid action proposal" in result and "Scored: 0" in result

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
                "native_reads": (read,) if history_source == "native_read" else (),
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
        assert len(requests) == 2 and sample.verdict is DecisionVerdict.UNLABELED
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


def test_first_run_plan_is_captured_without_execution_or_a_second_request(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        catalog = FrozenCatalog.model_validate_json(
            '{"complete":false,"catalog":{"categories":[],"card_filters":'
            '{"pools":[],"types":[],"rarities":[],"energy_costs":[]}},"definitions":[]}'
        )
        arguments = '{"reason":"Reach the elite.","edges":[{"from":"0:0","to":"0:1"}]}'
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
        assert len(requests) == 1 and sample.verdict is DecisionVerdict.UNLABELED
        assert sample.reason == "Reach the elite."
        assert sample.proposal is not None
        assert sample.proposal.tool == "run_plan" and sample.proposal.arguments == arguments

    asyncio.run(run())
