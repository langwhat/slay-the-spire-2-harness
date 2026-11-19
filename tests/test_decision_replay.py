import asyncio
import json
import re
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue, TypeAdapter
from test_combat import reply
from test_mod_client import CARD, RUN
from test_responses import configuration

from sts2_harness.capacity import CompactionDecision, CompactionValidation
from sts2_harness.client import models as native
from sts2_harness.config import Configuration, Driver, ModelConfiguration
from sts2_harness.context.models import RunActContent, TaskMessage
from sts2_harness.deck import deck_observation
from sts2_harness.fights import Fight, FightKind
from sts2_harness.game import Inspect, Inspected
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import BaselineSnapshot, NativeSnapshot, PreparationEvidence
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import ResponsesReply, TextMessage, TextRole
from sts2_harness.material_text import MaterialParts, MaterialText
from sts2_harness.model_journal import RecordedLlm
from sts2_harness.note_values import NotesState
from sts2_harness.prompts import PackagedPrompts
from sts2_harness.results import (
    ActionCompleted,
    PlanAccepted,
    PlanData,
    PlanPresentation,
    PlanPresentationStatus,
    PlanRejected,
    ReadCompleted,
    Reason,
    ReasonCode,
    Rejected,
    Suggestion,
)
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
    RouteState,
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
        viewer=False,
        events=(TaskEvent(observation=observation, state=HarnessState()),),
        decision_tool="run_act" if multiselect else "run_deck_choice",
        decision_offer="offer" if not multiselect else None,
        recorded_choices=(card.definition_id,),
        recorded_reason="Recorded.",
    )


@pytest.mark.parametrize("variant", [DecisionVariant.RUN, DecisionVariant.DECK])
def test_replay_cli_and_journal_use_shared_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: DecisionVariant
) -> None:
    bodies: list[dict[str, JsonValue]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        call = (
            ("choice", "run_act", '{"steps":[{"kind":"select","refs":["candidate"]}]}')
            if variant is DecisionVariant.RUN
            else (
                "choice",
                "run_deck_choice",
                '{"offer":"offer","choice":"take","card":"candidate","reason":"Useful."}',
            )
        )
        return httpx.Response(
            200,
            content=reply(call),
            headers={"Content-Type": "text/event-stream"},
        )

    class FakeLlm(Llm):
        def __init__(self, config: ModelConfiguration) -> None:
            super().__init__(config, transport=httpx.MockTransport(respond))

    config = configuration(
        tmp_path,
        LLM_REASONING_EFFORT="high",
    )

    def fake_configuration(*_args: object) -> Configuration:
        return Configuration(driver=Driver.BUILTIN, model=config)

    monkeypatch.setattr("sts2_harness_eval.decision_replay.Llm", FakeLlm)
    monkeypatch.setattr(
        "sts2_harness_eval.decision_replay.load_configuration",
        fake_configuration,
    )
    case = decision_case(tmp_path, multiselect=variant is DecisionVariant.RUN)
    case_path = tmp_path / "case.json"
    case_path.write_text(case.model_dump_json())
    assert (
        main(
            (
                "replay",
                "--cases",
                str(case_path),
                "--output",
                str(tmp_path / "results"),
                "--samples",
                "1",
                "--variant",
                variant.value,
            )
        )
        == 0
    )
    assert bodies[0]["reasoning"] == {"effort": "high"}
    results = json.loads((tmp_path / "results" / "results.json").read_text())
    assert results[0]["verdict"] == "passed"

    async def journal_sample() -> None:
        manifest = Manifest(
            materials=SessionMaterials(
                entry_documents_enabled=False, act_guidance={}, battle_guidance=()
            ),
            recording_id="replay-effort",
            core_version="test",
            game_instance_id="game",
            run_id="run",
            initial_notes=NotesState(),
        )
        with closing(Journal.create(tmp_path / "record.sqlite", manifest)) as journal:
            async with Llm(config, transport=httpx.MockTransport(respond)) as live:
                client = RecordedLlm(
                    live, journal, role=native.DecisionOwner.RUN, stream=variant.value
                )
                sample = await sample_case(case, client, variant=variant)
            assert sample.verdict is DecisionVerdict.PASSED
            records = journal.requests()
            recorded_reply = records[0].attempts[-1].reply
            assert recorded_reply is not None and recorded_reply.requested_effort == "high"
        assert bodies[1]["reasoning"] == {"effort": "high"}

    asyncio.run(journal_sample())


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
    assert focused.map == observation.map
    assert isinstance(observation.state, native.RunState)
    assert len(observation.state.offers) == 2


@pytest.mark.parametrize("minimal", (False, True))
def test_focused_reward_exposes_only_its_tools_and_mode_notes(
    tmp_path: Path, minimal: bool
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path).model_copy(update={"minimal": minimal})
        fights = (
            Fight(
                floor=1,
                kind=FightKind.NORMAL,
                hp_in=50,
                hp_out=46,
                damage=(),
                damage_one=(),
                incoming=(),
                hp_lost=(),
                potions=(),
            ),
        )
        case = case.model_copy(
            update={
                "events": tuple(
                    event.model_copy(
                        update={"state": event.state.model_copy(update={"fights": fights})}
                    )
                    if isinstance(event, TaskEvent)
                    else event
                    for event in case.events
                )
            }
        )
        async with Llm(configuration(tmp_path)) as client:
            rebuilt = await rebuild_case(case, client, variant=DecisionVariant.DECK)
            other_mode = await rebuild_case(
                case.model_copy(update={"minimal": not minimal}),
                client,
                variant=DecisionVariant.DECK,
            )
            run_role = await rebuild_case(case, client, variant=DecisionVariant.RUN)
        assert {tool.name for tool in rebuilt.prepared.tools} == {"run_deck_choice"}
        system = tuple(
            message.text
            for message in rebuilt.prepared.input
            if isinstance(message, TextMessage) and message.role is TextRole.SYSTEM
        )
        assert system == tuple(
            message.text
            for message in other_mode.prepared.input
            if isinstance(message, TextMessage) and message.role is TextRole.SYSTEM
        )
        assert PackagedPrompts().read("run-notes").body not in "\n".join(system)
        assert PackagedPrompts().read("state-format").body in "\n".join(system)
        task = next(
            message
            for message in reversed(rebuilt.prepared.input)
            if isinstance(message, TaskMessage)
        )
        assert isinstance(task.content, RunActContent)
        assert (task.content.observation.notes is MISSING) is minimal
        assert task.content.observation.fights == fights
        run_task = next(
            message
            for message in reversed(run_role.prepared.input)
            if isinstance(message, TaskMessage)
        )
        assert isinstance(run_task.content, RunActContent)
        assert run_task.content.observation.fights == fights

    asyncio.run(run())


@pytest.mark.parametrize("plan", (False, True))
def test_focused_replay_projects_remaining_route_without_the_map(
    tmp_path: Path, plan: bool
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        state = task.observation.state
        owned = task.observation.objects["owned"]
        assert isinstance(owned, native.CardEntry)
        observation = task.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={"progress": state.progress.model_copy(update={"position": "map:0:0"})}
                ),
                "objects": {
                    **task.observation.objects,
                    "owned": owned.model_copy(update={"cost": native.ResourceCosts(energy=2)}),
                },
                "map": native.PublicMap.model_validate_json(
                    '{"bosses":[],"nodes":['
                    '{"position":"map:0:0","room_type":"monster","children":["map:1:0"]},'
                    '{"position":"map:1:0","room_type":"restsite","children":["map:2:0"]},'
                    '{"position":"map:2:0","room_type":"shop","children":[]}],'
                    '"legal_destinations":null}'
                ),
            }
        )
        route = RouteState(position="map:0:0", edges=(("map:0:0", "map:1:0"),)) if plan else None
        case = case.model_copy(
            update={
                "events": (
                    task.model_copy(
                        update={"observation": observation, "state": HarnessState(route=route)}
                    ),
                )
            }
        )
        async with Llm(configuration(tmp_path)) as client:
            replay = await rebuild_case(case, client, variant=DecisionVariant.DECK)
        request = next(
            message for message in replay.prepared.input if isinstance(message, TaskMessage)
        )
        assert isinstance(request.content, RunActContent)
        projected = request.content.observation
        assert projected.map is MISSING
        assert isinstance(projected.progress, dict)
        assert projected.progress.get("route", MISSING) == (["R"] if plan else MISSING)
        assert isinstance(projected.deck_stats, dict)
        assert projected.deck_stats["circulating_avg_energy"] == 2.0

    asyncio.run(run())


@pytest.mark.parametrize(
    ("act", "acts", "act_prompt"),
    ((1, 3, "early"), (2, 3, "mid"), (3, 3, "final"), (None, 3, "mid"), (1, 1, "early")),
)
def test_deck_and_run_use_the_act_valuation_with_deck_only_choice_steps(
    tmp_path: Path, act: int | None, acts: int, act_prompt: str
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path)
        task = case.events[0]
        assert isinstance(task, TaskEvent)
        state = task.observation.state
        observation = task.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "progress": state.progress.model_copy(update={"acts": acts, "act": act})
                    }
                )
            }
        )
        case = case.model_copy(
            update={"events": (task.model_copy(update={"observation": observation}),)}
        )
        async with Llm(configuration(tmp_path)) as client:
            deck = await rebuild_case(case, client, variant=DecisionVariant.DECK)
            run = await rebuild_case(case, client, variant=DecisionVariant.RUN)
        deck_text = deck.prepared.input[0]
        run_text = run.prepared.input[0]
        assert isinstance(deck_text, TextMessage) and deck_text.role is TextRole.SYSTEM
        assert isinstance(run_text, TextMessage) and run_text.role is TextRole.SYSTEM
        assert isinstance(deck_text.value, MaterialParts)
        assert isinstance(run_text.value, MaterialParts)
        act_id = f"prompt:deck-act-{act_prompt}"
        deck_ids = tuple(
            part.id for part in deck_text.value.parts if isinstance(part, MaterialText)
        )
        run_parts = tuple(part for part in run_text.value.parts if isinstance(part, MaterialText))
        run_ids = tuple(part.id for part in run_parts)
        assert deck_ids == (
            "prompt:deck-tasks",
            "prompt:state-format",
            "prompt:deck",
            act_id,
            "prompt:deck-choice",
        )
        deck_index = run_ids.index("prompt:deck")
        assert run_ids[deck_index + 1] == act_id
        assert "prompt:deck-choice" not in run_ids
        deck_fields = {
            "circulating_avg_energy",
            "deck_stats.circulating_avg_energy",
            "progress.route",
        }
        for shared_part in run_parts[deck_index : deck_index + 2]:
            identifiers = re.findall(r"\b\w+(?:\.\w+)*\b", shared_part.body)
            assert deck_fields.isdisjoint(identifiers)

    asyncio.run(run())


def test_recorded_native_route_is_rebuilt_as_stretches_when_travel_is_unavailable(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        case = decision_case(tmp_path, multiselect=True)
        task = case.events[0]
        assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
        state = task.observation.state
        public_map = native.PublicMap.model_validate_json(
            '{"bosses":[],"nodes":['
            '{"position":"map:0:0","room_type":"ancient","children":["map:1:0"]},'
            '{"position":"map:1:0","room_type":"monster","children":["map:2:0"]},'
            '{"position":"map:2:0","room_type":"restsite","children":[]}],'
            '"legal_destinations":null}'
        )
        observation = task.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={"progress": state.progress.model_copy(update={"position": "map:0:0"})}
                ),
                "map": public_map,
            }
        )
        case = case.model_copy(
            update={
                "events": (
                    task.model_copy(
                        update={
                            "observation": observation,
                            "state": HarnessState(
                                route=RouteState(
                                    position="map:0:0",
                                    edges=(("map:0:0", "map:1:0"), ("map:1:0", "map:2:0")),
                                )
                            ),
                        }
                    ),
                )
            }
        )
        bodies: list[dict[str, JsonValue]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                content=reply(
                    ("choice", "run_act", '{"steps":[{"kind":"select","refs":["candidate"]}]}')
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            sample = await sample_case(case, client, variant=DecisionVariant.RUN)
            incomplete = case.model_copy(
                update={
                    "events": (
                        task.model_copy(
                            update={
                                "observation": observation,
                                "state": HarnessState(
                                    route=RouteState(
                                        position="map:0:0", edges=(("map:0:0", "map:1:0"),)
                                    )
                                ),
                            }
                        ),
                    )
                }
            )
            unsupported = await sample_case(incomplete, client, variant=DecisionVariant.RUN)
        assert sample.verdict is DecisionVerdict.PASSED
        assert unsupported.verdict is DecisionVerdict.UNSUPPORTED
        assert unsupported.error == "The recorded route cannot be represented as stretches."
        assert len(bodies) == 1
        items = bodies[0]["input"]
        assert isinstance(items, list) and isinstance(item := items[-1], dict)
        assert isinstance(content := item["content"], str)
        projected = json.loads(content)["observation"]["map"]
        assert projected["at"] == {"node": "map:0:0", "stretch": "s1", "room": 1}
        assert projected["next"] is None and projected["plan"] == ["s1"]
        assert projected["stretches"][0]["rooms"] == "AMR"

    asyncio.run(run())


@pytest.mark.parametrize("variant", [DecisionVariant.DECK])
def test_real_loop_resolves_reward_without_writing_notes_or_native_action(
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
                    (
                        "choice",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "offer": "offer",
                                "choice": "take",
                                "card": "candidate",
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
                }
                task = next(
                    message
                    for message in reversed(rebuilt.prepared.input)
                    if isinstance(message, TaskMessage)
                )
                assert isinstance(task.content, RunActContent)
                assert task.content.observation.map is MISSING
            result = await sample_case(case, client, variant=variant)
        assert result.verdict is DecisionVerdict.PASSED
        assert result.proposal is None and result.proposal_status is None
        assert len(result.requests) == len(result.request_bodies) == len(result.model_replies) == 1
        assert len(result.attempts) == 1
        assert result.attempts[0]["digest"] == result.requests[0].digest
        assert result.attempts[0]["native_output"] == result.model_replies[0]
        assert result.choices == case.labels.good
        assert result.reason == "A better turn."
        assert "PRIVATE-LABEL-DO-NOT-SEND" not in bodies[0]
        assert result.raw_calls[0]["name"] == "run_deck_choice"

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


def _route_reply(call_id: str, tool: str, arguments: str) -> AssistantEvent:
    native_reply = ResponsesReply.from_json(
        json.dumps(
            [
                {
                    "type": "function_call",
                    "id": f"{call_id}-fc",
                    "call_id": call_id,
                    "name": tool,
                    "arguments": arguments,
                }
            ]
        )
    )
    return AssistantEvent(
        reply=NativeSnapshot(protocol=native_reply.protocol, native_output=native_reply.to_json())
    )


def _recorded_route_case(tmp_path: Path) -> DecisionCase:
    case = decision_case(tmp_path, multiselect=True)
    task = case.events[0]
    assert isinstance(task, TaskEvent) and isinstance(task.observation.state, native.RunState)
    state = task.observation.state
    observation = task.observation.model_copy(
        update={
            "state": state.model_copy(
                update={
                    "screen": native.RunScreen.MAP,
                    "progress": state.progress.model_copy(update={"position": "map:0:0"}),
                    "selection": None,
                    "offers": (),
                    "actions": (
                        native.Action(
                            ref="travel", label="Travel", enabled=True, destination="map:1:0"
                        ),
                    ),
                }
            ),
            "map": native.PublicMap.model_validate_json(
                '{"bosses":[],"nodes":['
                '{"position":"map:0:0","room_type":"ancient",'
                '"children":["map:1:0","map:1:1"]},'
                '{"position":"map:1:0","room_type":"monster","children":["map:2:0"]},'
                '{"position":"map:1:1","room_type":"restsite","children":[]},'
                '{"position":"map:2:0","room_type":"boss","children":[]}],'
                '"legal_destinations":["map:1:0","map:1:1"]}'
            ),
        }
    )
    return case.model_copy(
        update={
            "decision_tool": "run_next",
            "events": (
                task.model_copy(update={"observation": observation}),
                _route_reply(
                    "plan", "run_plan", '{"reason":"Fight then Boss.","stretches":["s2"]}'
                ),
                ResultEvent(
                    call_id="plan",
                    outcome=ReadCompleted(),
                    action=False,
                    observation=None,
                    plan=PlanData(
                        plan=PlanAccepted(next="s2"),
                        presentation=PlanPresentation(status=PlanPresentationStatus.NOT_AVAILABLE),
                    ),
                ),
            ),
        }
    )


def test_legacy_route_history_is_unsupported_before_model_request(tmp_path: Path) -> None:
    async def run() -> None:
        case = _recorded_route_case(tmp_path)
        old_call = _route_reply(
            "plan",
            "run_plan",
            '{"reason":"Old route.","edges":[{"from":"map:0:0","to":"map:1:0"}]}',
        )
        result = case.events[2]
        assert isinstance(result, ResultEvent) and result.plan is not None
        case = case.model_copy(
            update={
                "events": (
                    case.events[0],
                    old_call,
                    result.model_copy(
                        update={
                            "plan": result.plan.model_copy(
                                update={"plan": PlanAccepted(next="map:1:0")}
                            )
                        }
                    ),
                )
            }
        )
        original = old_call.reply.native_output
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("next", "run_next", '{"reason":"Go."}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            sample = await sample_case(case, client)
            unfinished = await sample_case(
                case.model_copy(update={"events": case.events[:2]}), client
            )
        assert sample.verdict is DecisionVerdict.UNSUPPORTED
        assert sample.error is not None and "route" in sample.error.lower()
        assert unfinished.verdict is DecisionVerdict.UNSUPPORTED
        assert unfinished.error == "A recorded route tool call has no result."
        assert requests == [] and old_call.reply.native_output == original

    asyncio.run(run())


@pytest.mark.parametrize("code", (ReasonCode.BATCH_ABORTED, ReasonCode.INVALID_ARGUMENTS))
@pytest.mark.parametrize(
    "unsupported_arguments",
    (
        '{"reason":"Old route.","edges":[{"from":"map:0:0","to":"map:1:0"}]}',
        '{"reason":"Old.","reason":"Route.","edges":[]}',
        '{"reason":"Old route.","edges":[{"from":"map:0:0","to":"map:1:0"}]',
        '{"reason":"Go.","stretches":[',
        None,
    ),
)
def test_rejected_route_batches_preserve_only_current_schema_history(
    tmp_path: Path, code: ReasonCode, unsupported_arguments: str | None
) -> None:
    async def run() -> None:
        case = _recorded_route_case(tmp_path)
        task = case.events[0]
        assert isinstance(task, TaskEvent)
        arguments = unsupported_arguments or (
            '{"reason":"Use edges.","stretches":["s2"]}'
            if code is ReasonCode.BATCH_ABORTED
            else '{"reason":"Use edges.","stretches":[]}'
        )
        first_tool = "run_notes" if code is ReasonCode.BATCH_ABORTED else "run_next"
        native_reply = ResponsesReply.from_json(
            json.dumps(
                [
                    {
                        "type": "function_call",
                        "id": "first-fc",
                        "call_id": "first",
                        "name": first_tool,
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "id": "plan-fc",
                        "call_id": "plan",
                        "name": "run_plan",
                        "arguments": arguments,
                    },
                ]
            )
        )
        response = AssistantEvent(
            reply=NativeSnapshot(
                protocol=native_reply.protocol, native_output=native_reply.to_json()
            )
        )
        first_code = ReasonCode.QUERY_FAILED if code is ReasonCode.BATCH_ABORTED else code
        observation = task.observation if code is ReasonCode.INVALID_ARGUMENTS else None
        case = case.model_copy(
            update={
                "events": (
                    task,
                    response,
                    ResultEvent(
                        call_id="first",
                        outcome=Rejected(
                            reason=Reason(code=first_code, message="First call failed."),
                            suggested_actions=(Suggestion(message="Review."),),
                        ),
                        action=False,
                        observation=observation,
                    ),
                    ResultEvent(
                        call_id="plan",
                        outcome=Rejected(
                            reason=Reason(code=code, message="Plan was not executed."),
                            suggested_actions=(Suggestion(message="Review."),),
                        ),
                        action=False,
                        observation=observation,
                    ),
                )
            }
        )
        original = response.reply.native_output
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("next", "run_next", '{"reason":"Go."}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            sample = await sample_case(case, client)
        assert response.reply.native_output == original
        if unsupported_arguments is not None:
            assert sample.verdict is DecisionVerdict.UNSUPPORTED
            assert sample.error is not None and "incompatible schema" in sample.error
            assert requests == []
        else:
            assert sample.verdict is DecisionVerdict.DEVIATED
            assert sample.error == "No route plan is active." and len(requests) == 1
            body = json.loads(requests[0].content)
            calls = [item for item in body["input"] if item.get("type") == "function_call"]
            assert calls[-1]["arguments"] == arguments

    asyncio.run(run())


def test_accepted_recorded_stretches_restore_the_route_before_run_next(tmp_path: Path) -> None:
    async def run() -> None:
        case = _recorded_route_case(tmp_path)
        task, result = case.events[0], case.events[2]
        assert isinstance(task, TaskEvent) and isinstance(result, ResultEvent)
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("next", "run_next", '{"reason":"Go."}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            rebuilt = await rebuild_case(case, client)
            route = rebuilt.agent.route_plan
            assert route is not None
            assert [(edge.source, edge.destination) for edge in route.edges] == [
                ("map:0:0", "map:1:0"),
                ("map:1:0", "map:2:0"),
            ]
            sample = await sample_case(case, client)
            assert len(requests) == 1
            assert sample.error == "A different game action deviated from the labeled decision."
            body = json.loads(requests[0].content)
            calls = [item for item in body["input"] if item.get("type") == "function_call"]
            outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
            assert json.loads(calls[-1]["arguments"])["stretches"] == ["s2"]
            assert json.loads(outputs[-1]["output"])["data"]["plan"]["next"] == "s2"
            later = task.observation.model_copy(
                update={
                    "stamp": task.observation.stamp.model_copy(
                        update={
                            "observation_sequence": task.observation.stamp.observation_sequence + 1
                        }
                    )
                }
            )
            observed = case.model_copy(
                update={
                    "events": (*case.events[:2], result.model_copy(update={"observation": later}))
                }
            )
            assert (await rebuild_case(observed, client)).agent.route_plan == route
            assert task.observation.map is not MISSING
            unavailable = task.observation.model_copy(
                update={"map": task.observation.map.model_copy(update={"legal_destinations": None})}
            )
            blocked = case.model_copy(
                update={
                    "events": (
                        task.model_copy(update={"observation": unavailable}),
                        *case.events[1:],
                    )
                }
            )
            assert (await rebuild_case(blocked, client)).agent.route_plan == route

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    ("wrong_next", "conflicting_anchor", "missing_status", "unobserved_action", "bad_rejection"),
)
def test_unprovable_recorded_route_is_unsupported(tmp_path: Path, problem: str) -> None:
    async def run() -> None:
        case = _recorded_route_case(tmp_path)
        task, result = case.events[0], case.events[2]
        assert (
            isinstance(task, TaskEvent)
            and isinstance(result, ResultEvent)
            and result.plan is not None
        )
        if problem == "wrong_next":
            result = result.model_copy(
                update={"plan": result.plan.model_copy(update={"plan": PlanAccepted(next="s3")})}
            )
        elif problem == "conflicting_anchor":
            observation = task.observation
            conflict = observation.model_copy(
                update={
                    "state": observation.state.model_copy(
                        update={
                            "progress": observation.state.progress.model_copy(
                                update={"position": "map:1:1"}
                            )
                        }
                    )
                }
            )
            result = result.model_copy(update={"observation": conflict})
        elif problem == "missing_status":
            result = result.model_copy(update={"plan": None})
        elif problem == "bad_rejection":
            result = result.model_copy(
                update={
                    "outcome": Rejected(
                        reason=Reason(code=ReasonCode.DISCONNECTED, message="Disconnected."),
                        suggested_actions=(Suggestion(message="Check route."),),
                    ),
                    "plan": result.plan.model_copy(update={"plan": PlanRejected()}),
                }
            )
        events = (task, case.events[1], result)
        if problem == "unobserved_action":
            events = (
                task,
                _route_reply("action", "run_act", "{}"),
                ResultEvent(
                    call_id="action",
                    outcome=ActionCompleted(completed_steps=1),
                    action=True,
                    observation=None,
                ),
                *events[1:],
            )
        case = case.model_copy(update={"events": events})
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("next", "run_next", '{"reason":"Go."}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            sample = await sample_case(case, client)
        assert sample.verdict is DecisionVerdict.UNSUPPORTED and sample.error is not None
        assert requests == []

    asyncio.run(run())


def test_recorded_rejection_preserves_route_and_valid_failed_calls(tmp_path: Path) -> None:
    async def run() -> None:
        case = _recorded_route_case(tmp_path)
        task, result = case.events[0], case.events[2]
        assert (
            isinstance(task, TaskEvent)
            and isinstance(result, ResultEvent)
            and result.plan is not None
        )
        previous = task.model_copy(
            update={
                "state": task.state.model_copy(
                    update={
                        "route": RouteState(
                            position="map:0:0",
                            edges=(("map:0:0", "map:1:0"), ("map:1:0", "map:2:0")),
                        )
                    }
                )
            }
        )
        failed = Rejected(
            reason=Reason(code=ReasonCode.QUERY_FAILED, message="Unavailable."),
            suggested_actions=(Suggestion(message="Retry."),),
        )
        rejected = case.model_copy(
            update={
                "events": (
                    previous,
                    case.events[1],
                    result.model_copy(
                        update={
                            "outcome": failed,
                            "plan": result.plan.model_copy(update={"plan": PlanRejected()}),
                        }
                    ),
                )
            }
        )
        malformed = case.model_copy(
            update={
                "events": (
                    task,
                    _route_reply("plan", "run_plan", "{}"),
                    result.model_copy(
                        update={
                            "outcome": Rejected(
                                reason=Reason(
                                    code=ReasonCode.INVALID_ARGUMENTS, message="Invalid."
                                ),
                                suggested_actions=(Suggestion(message="Fix arguments."),),
                            ),
                            "plan": result.plan.model_copy(update={"plan": PlanRejected()}),
                        }
                    ),
                )
            }
        )
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=reply(("next", "run_next", '{"reason":"Go."}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(respond)) as client:
            rebuilt = await rebuild_case(rejected, client)
            assert rebuilt.agent.route_plan is not None
            assert [(edge.source, edge.destination) for edge in rebuilt.agent.route_plan.edges] == [
                ("map:0:0", "map:1:0"),
                ("map:1:0", "map:2:0"),
            ]
            sample = await sample_case(malformed, client)
        assert sample.verdict is DecisionVerdict.DEVIATED
        assert sample.error == "No route plan is active." and len(requests) == 1

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


def test_rebuilt_deck_keeps_public_map_without_other_offer_materials(tmp_path: Path) -> None:
    from sts2_harness.entry_materials import EntryMaterials
    from sts2_harness.journal import SessionMaterials
    from sts2_harness.knowledge import MaterialRole, SubjectId
    from sts2_harness.materials import DocumentSource, FileDocuments

    case = decision_case(tmp_path)
    task = case.events[0]
    assert isinstance(task, TaskEvent)
    observation = task.observation
    state = observation.state
    assert isinstance(state, native.RunState)
    other_card = native.CardEntry.model_validate_json(CARD).model_copy(
        update={"definition_id": "CARD.OTHER"}
    )
    other_offer = native.Offer(
        ref="other",
        kind=native.OfferKind.CARD,
        contents=("other-card",),
        actions=("other-action",),
        requires_selection=True,
    )
    observation = observation.model_copy(
        update={
            "state": state.model_copy(update={"offers": (*state.offers, other_offer)}),
            "objects": {**observation.objects, "other-card": other_card},
            "map": native.PublicMap(
                bosses=(native.NamedDefinition(name="Boss", definition_id="ENCOUNTER.BOSS"),),
                nodes=(),
                legal_destinations=None,
            ),
        }
    )
    case = case.model_copy(
        update={
            "entry_documents_enabled": True,
            "events": (task.model_copy(update={"observation": observation}),),
        }
    )
    docs: list[DocumentSource] = []
    for identity, category, marker in (
        ("CARD.OTHER", "card", "OTHER OFFER DOC"),
        ("ENCOUNTER.BOSS", "encounter", "PUBLIC BOSS DOC"),
    ):
        path = tmp_path / marker
        path.write_text(marker)
        docs.append(
            DocumentSource(
                identity,
                identity,
                identity,
                path,
                subjects=(SubjectId(category=category, definition_id=identity),),
                auto_roles=(MaterialRole.DECK,),
            )
        )
    provider = FileDocuments(tuple(docs))
    materials = EntryMaterials(
        documents=lambda context: provider,
        settings=SessionMaterials(
            entry_documents_enabled=True, act_guidance={}, battle_guidance=()
        ),
    )

    async def scenario() -> None:
        async with Llm(configuration(tmp_path)) as client:
            rebuilt = await rebuild_case(
                case, client, variant=DecisionVariant.DECK, materials=materials
            )
        encoded = json.dumps(rebuilt.prepared.body, ensure_ascii=False)
        assert "PUBLIC BOSS DOC" in encoded
        assert "OTHER OFFER DOC" not in encoded and "CARD.OTHER" not in encoded
        task_message = next(
            item for item in rebuilt.prepared.input if isinstance(item, TaskMessage)
        )
        assert "map" not in json.loads(task_message.text)["observation"]

    asyncio.run(scenario())
