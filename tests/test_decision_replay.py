import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue, TypeAdapter
from test_combat import reply
from test_mod_client import CARD, RUN
from test_responses import configuration

from sts2_harness.capacity import CompactionDecision, CompactionValidation
from sts2_harness.client import models as native
from sts2_harness.context.models import RunActContent, TaskMessage
from sts2_harness.deck import deck_observation
from sts2_harness.game import Inspect, Inspected
from sts2_harness.journal_models import BaselineSnapshot, NativeSnapshot, PreparationEvidence
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ResponsesReply, TextRole
from sts2_harness.results import Reason, ReasonCode, Rejected, Suggestion
from sts2_harness_eval.decision_cases import (
    AssistantEvent,
    DecisionCase,
    DecisionLabels,
    DecisionSource,
    DecisionVariant,
    DecisionVerdict,
    HarnessState,
    NativeRead,
    RequestSize,
    ResultEvent,
    TaskEvent,
    score,
)
from sts2_harness_eval.decision_extract import ExtractionError, unused_baseline_permutation
from sts2_harness_eval.decision_replay import (
    CaseGame,
    load_case,
    main,
    rebuild_case,
    recorded,
    sample_case,
    write_results,
)


def decision_case(tmp_path: Path, *, multiselect: bool = False) -> DecisionCase:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    card = native.CardEntry.model_validate_json(CARD)
    observation = observation.model_copy(
        update={
            "state": observation.state.model_copy(
                update={
                    "screen": native.RunScreen.REWARDS,
                    "deck": ("owned",),
                    "offers": (
                        native.Offer(
                            ref="offer",
                            kind=native.OfferKind.CARD,
                            contents=("candidate",),
                            actions=("open",),
                            requires_selection=True,
                        ),
                    ),
                    "actions": (native.Action(ref="open", label="Reward", enabled=True),),
                    "selection": native.Selection(
                        prompt="Choose",
                        source=None,
                        min=0,
                        max=1,
                        candidates=("candidate",),
                        selected=(),
                    )
                    if multiselect
                    else None,
                }
            ),
            "objects": {"owned": card, "candidate": card},
            "descriptions": {"owned": "Draw.", "candidate": "Draw."},
        }
    )
    return DecisionCase(
        source=DecisionSource(
            round="fixture",
            seed="seed",
            floor=1,
            model_sequence=1,
            archive=tmp_path / "source.sqlite",
        ),
        labels=DecisionLabels(good=(card.definition_id,), comment="PRIVATE-LABEL-DO-NOT-SEND"),
        entry_documents_enabled=False,
        candidate_links_enabled=False,
        viewer=False,
        events=(TaskEvent(observation=observation, state=HarnessState()),),
        decision_tool="run_act" if multiselect else "run_deck_choice",
        decision_offer="offer" if not multiselect else None,
        recorded_choices=(card.definition_id,),
        recorded_reason="Recorded.",
    )


def test_focused_reward_preserves_owned_state_without_other_rewards(tmp_path: Path) -> None:
    case = decision_case(tmp_path)
    task = case.events[0]
    assert isinstance(task, TaskEvent)
    observation = task.observation
    state = observation.state
    assert isinstance(state, native.RunState)
    other = native.Offer(
        ref="other-offer",
        kind=native.OfferKind.CARD,
        contents=("other-card",),
        actions=("other-open",),
        requires_selection=True,
    )
    observation = observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "offers": (*state.offers, other),
                    "actions": (
                        *state.actions,
                        native.Action(ref="other-open", label="Other reward", enabled=True),
                    ),
                    "selection": native.Selection(
                        prompt="Another choice",
                        source=None,
                        min=0,
                        max=1,
                        candidates=("other-card",),
                        selected=(),
                    ),
                }
            ),
            "objects": {**observation.objects, "other-card": observation.objects["candidate"]},
            "descriptions": {**observation.descriptions, "other-card": "Unrelated reward."},
        }
    )

    focused = deck_observation(observation, state.offers[0])

    assert isinstance(focused.state, native.RunState)
    assert focused.state.offers == state.offers
    assert focused.state.actions == state.actions
    assert focused.state.selection is None
    assert focused.state.deck == state.deck
    assert focused.state.player == state.player
    assert focused.objects == task.observation.objects
    assert focused.descriptions == task.observation.descriptions
    assert focused.map is MISSING
    assert isinstance(observation.state, native.RunState)
    assert len(observation.state.offers) == 2


@pytest.mark.parametrize("minimal", (False, True))
def test_focused_reward_exposes_only_its_tools_and_mode_notes(
    tmp_path: Path, minimal: bool
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path).model_copy(update={"minimal": minimal})
        async with Llm(configuration(tmp_path)) as client:
            rebuilt = await rebuild_case(case, client, variant=DecisionVariant.DECK)
        expected = {"run_deck_choice"} if minimal else {"run_deck_choice", "run_notes"}
        assert {tool.name for tool in rebuilt.prepared.tools} == expected
        task = next(
            message
            for message in reversed(rebuilt.prepared.input)
            if isinstance(message, TaskMessage)
        )
        assert isinstance(task.content, RunActContent)
        assert (task.content.observation.notes is MISSING) is minimal

    asyncio.run(run())


@pytest.mark.parametrize("variant", tuple(DecisionVariant))
def test_real_loop_allows_notes_then_stops_without_native_action(
    tmp_path: Path, variant: DecisionVariant
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path)
        bodies: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/v1/responses")
            bodies.append(request.content.decode())
            return httpx.Response(
                200,
                content=reply(
                    ("notes", "run_notes", '{"mode":"write","plan":"Better cycle."}'),
                    (
                        "choice",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "offer": "offer",
                                "choice": "take",
                                "card": "candidate",
                                "for": "draw",
                                "reason": "A better turn.",
                            }
                        ),
                    ),
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            rebuilt = await rebuild_case(case, client, variant=variant)
            if variant is DecisionVariant.DECK:
                assert {tool.name for tool in rebuilt.prepared.tools} == {
                    "run_deck_choice",
                    "run_notes",
                }
                task = next(
                    message
                    for message in reversed(rebuilt.prepared.input)
                    if isinstance(message, TaskMessage)
                )
                assert isinstance(task.content, RunActContent)
                assert task.content.observation.map_topology is MISSING
                assert task.content.observation.dynamic_map is MISSING
                assert task.content.observation.unknown_odds is MISSING
            result = await sample_case(case, client, variant=variant)
        assert result.verdict is DecisionVerdict.PASSED
        assert len(result.requests) == len(result.request_bodies) == len(result.model_replies) == 1
        assert len(result.attempts) == 1
        assert result.attempts[0]["digest"] == result.requests[0].digest
        assert result.attempts[0]["native_output"] == result.model_replies[0]
        assert result.choices == case.labels.good
        assert result.reason == "A better turn."
        assert "PRIVATE-LABEL-DO-NOT-SEND" not in bodies[0]
        assert result.raw_calls[0]["name"] == "run_notes"

    asyncio.run(run())


@pytest.mark.parametrize("wrong", ("offer", "selection"))
def test_wrong_native_target_or_illegal_selection_is_deviation(tmp_path: Path, wrong: str) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=wrong == "selection")
        arguments: dict[str, JsonValue] = (
            {"steps": [{"kind": "select", "refs": ["not-a-candidate"]}], "reason": "Wrong."}
            if wrong == "selection"
            else {
                "offer": "different",
                "choice": "take",
                "card": "candidate",
                "for": "draw",
                "reason": "Wrong.",
            }
        )

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(("choice", case.decision_tool, json.dumps(arguments))),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            result = await sample_case(case, client)
        assert result.verdict is DecisionVerdict.DEVIATED
        assert len(result.requests) == 1
        assert result.choices == ()

    asyncio.run(run())


@pytest.mark.parametrize("arguments", ("{", "[]", "null"))
def test_malformed_model_choice_is_deviation(tmp_path: Path, arguments: str) -> None:
    async def run() -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(("choice", "run_deck_choice", arguments)),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            result = await sample_case(decision_case(tmp_path), client)
        assert result.verdict is DecisionVerdict.DEVIATED
        assert len(result.requests) == 1
        assert result.error is not None

    asyncio.run(run())


def test_invalid_model_tool_exhaustion_is_deviation(tmp_path: Path) -> None:
    async def run() -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(("wrong", "nonexistent_tool", "{}")),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            result = await sample_case(decision_case(tmp_path), client)
        assert result.verdict is DecisionVerdict.DEVIATED
        assert len(result.requests) == 4
        assert result.error is not None

    asyncio.run(run())


def test_scoring_distinguishes_alternatives_multiselect_and_unknown() -> None:
    labels = DecisionLabels(good=("CARD.A", "CARD.B"), bad=("CARD.BAD",), comment="")
    assert score(labels, ("CARD.B",)) is DecisionVerdict.PASSED
    assert score(labels, ("CARD.BAD",)) is DecisionVerdict.FAILED
    assert score(labels, ("CARD.UNKNOWN",)) is DecisionVerdict.UNLABELED
    multiple = labels.model_copy(update={"multiselect": True})
    assert score(multiple, ("CARD.A", "CARD.B")) is DecisionVerdict.PASSED
    assert score(multiple, ("CARD.A",)) is DecisionVerdict.FAILED


def test_rebuild_retains_rejected_query_history(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path)
        native_reply = ResponsesReply.from_json(
            '[{"type":"function_call","id":"fc","call_id":"read",'
            '"name":"run_notes","arguments":"{}"}]'
        )
        rejected = Rejected(
            reason=Reason(code=ReasonCode.INVALID_ARGUMENTS, message="Invalid historical query."),
            suggested_actions=(Suggestion(message="Supply valid arguments."),),
        )
        case = case.model_copy(
            update={
                "events": (
                    *case.events,
                    AssistantEvent(
                        reply=NativeSnapshot(
                            protocol=native_reply.protocol,
                            native_output=native_reply.to_json(),
                        )
                    ),
                    ResultEvent(call_id="read", outcome=rejected, action=False, observation=None),
                )
            }
        )
        async with Llm(configuration(tmp_path)) as client:
            rebuilt = await rebuild_case(case, client)
        body = json.dumps(rebuilt.prepared.body)
        assert "Invalid historical query." in body
        assert "Supply valid arguments." in body
        assert "invalid_arguments" in body

    asyncio.run(run())


def baseline(definitions: list[dict[str, JsonValue]]) -> PreparationEvidence:
    content = TypeAdapter(dict[str, JsonValue]).validate_json(
        json.dumps({"context_baseline": {"observation": {"defs": definitions}}})
    )
    return PreparationEvidence(
        group_id=1,
        baseline=BaselineSnapshot(role=TextRole.USER, content=content, text=json.dumps(content)),
        input_before=(),
        estimate_before=10,
        decision=CompactionDecision.UNCHANGED,
        input_after=(),
        estimate_after=10,
        retained_complete_group_id=None,
        active_group_id=1,
        validation=CompactionValidation.NOT_NEEDED,
    )


def test_unused_baseline_normalization_never_accepts_changed_body_or_used_candidate() -> None:
    first = baseline([{"id": "A", "text": "a"}, {"id": "B", "text": "b"}])
    permutation = baseline([{"id": "B", "text": "b"}, {"id": "A", "text": "a"}])
    assert unused_baseline_permutation(first, permutation)
    changed = baseline([{"id": "B", "text": "changed"}, {"id": "A", "text": "a"}])
    assert not unused_baseline_permutation(first, changed)
    used = permutation.model_copy(update={"decision": CompactionDecision.COMPACTED})
    assert not unused_baseline_permutation(first, used)
    counted = permutation.model_copy(update={"estimate_before": 11})
    assert not unused_baseline_permutation(first, counted)


def test_report_preserves_unknown_and_partial_usage_coverage(tmp_path: Path) -> None:
    source = recorded(decision_case(tmp_path))
    size = RequestSize(
        system_characters=0,
        tools_characters=0,
        knowledge_characters=0,
        volatile_characters=0,
        encoded_bytes=1,
        estimated_input_tokens=1,
        digest="fixture",
    )
    missing = source.model_copy(
        update={
            "sample": 1,
            "requests": (size,),
            "usage": (
                {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                },
            ),
        }
    )
    partial = source.model_copy(
        update={
            "sample": 2,
            "requests": (size,),
            "attempts": (
                {
                    "input_tokens": 7,
                    "output_tokens": 2,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": None,
                },
                {},
            ),
        }
    )
    write_results(tmp_path, (missing, partial, source))
    report = (tmp_path / "report.md").read_text()
    assert "| 1 | 1 / 0 | unknown (0/1 known) / unknown (0/1 known) |" in report
    assert "| 2 | 1 / 2 | 7 (partial; 1/2 known) / 2 (partial; 1/2 known) |" in report
    assert "0 (partial; 1/2 known) / unknown (0/2 known)" in report
    assert "7 (partial; 1/3 known) input" in report
    assert "unknown (0/3 known) cache-write" in report
    assert "0 (no requests) / 0 (no requests)" in report
    saved = json.loads((tmp_path / "results.json").read_text())
    assert saved[0]["usage"][0]["input_tokens"] is None


def test_recorded_cli_scores_mixed_native_schemas_without_loading_native_state(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    current = decision_case(tmp_path)
    (cases / "current.json").write_text(current.model_dump_json())
    legacy = json.loads(current.model_dump_json())
    legacy["events"][0]["observation"]["obsolete_native_field"] = "old protocol"
    legacy_path = cases / "legacy.json"
    legacy_path.write_text(json.dumps(legacy))
    output = tmp_path / "results"
    assert main(("recorded", "--cases", str(cases), "--output", str(output))) == 0
    results = json.loads((output / "results.json").read_text())
    assert len(results) == 2
    assert all(result["verdict"] == "passed" for result in results)
    assert isinstance(load_case(cases / "current.json", replay=True), DecisionCase)
    assert not isinstance(load_case(legacy_path, replay=True), DecisionCase)


def test_fixture_never_relabels_historical_inspection_as_current(tmp_path: Path) -> None:
    async def run() -> None:
        case = decision_case(tmp_path)
        event = case.events[0]
        assert isinstance(event, TaskEvent)
        observation = event.observation
        earlier = observation.model_copy(
            update={
                "location": observation.location.model_copy(update={"node_id": "previous-node"}),
            }
        )
        query = native.CardQuery(area=native.InspectArea.DECK)
        read = NativeRead(
            request=Inspect(query=query),
            result=native.InspectCompleted(
                status=native.ResultStatus.COMPLETED,
                observation=earlier,
            ),
        )
        stale = CaseGame(case.model_copy(update={"native_reads": (read,)}), observation)
        with pytest.raises(ExtractionError, match="no raw native reply"):
            await stale.inspect(query)
        current_read = read.model_copy(
            update={
                "result": read.result.model_copy(
                    update={"observation": observation},
                )
            }
        )
        current = CaseGame(case.model_copy(update={"native_reads": (current_read,)}), observation)
        result = await current.inspect(query)
        assert isinstance(result.outcome, Inspected)
        assert result.outcome.result == current_read.result

    asyncio.run(run())
