import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue, TypeAdapter
from test_combat import reply
from test_lifecycle import terminal
from test_mod_client import CARD, PREVIEW, RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.context.models import RunActContent
from sts2_harness.game import RunEndReason
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.journal_models import AutomaticSnapshot, ResultSnapshot, RunTaskSnapshot
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import AssistantMessage
from sts2_harness.note_values import NotesState
from sts2_harness.results import ActionCompleted, ActionUnknown, ReadCompleted
from sts2_harness.user_input import UserInputQueue
from sts2_harness.user_input_values import UserInput
from sts2_harness_eval._replay import ReplayRejected
from sts2_harness_eval.decision_cases import DecisionLabels, DecisionSource, TaskEvent
from sts2_harness_eval.decision_extract import ExtractionError, extract_case


class RewardGame:
    def __init__(self, *, minimal: bool, unknown: bool = False) -> None:
        observation = native.Observation.model_validate_json(RUN)
        assert isinstance(observation.state, native.RunState)
        card = native.CardEntry.model_validate_json(CARD)
        self.minimal = minimal
        self.unknown = unknown
        self.local_sent = False
        self.inputs: list[dict[str, JsonValue]] = []
        self.steps: list[native.NativeStep] = []
        self.observation = observation.model_copy(
            update={
                "objects": {
                    ref: card.model_copy(update={"definition_id": "CARD." + ref.upper()})
                    for ref in ("owned", "local", "regular", "guaranteed", "shop", "gift", "shared")
                },
                "state": observation.state.model_copy(
                    update={
                        "screen": native.RunScreen.REWARDS,
                        "deck": ("owned",),
                        "offers": tuple(
                            native.Offer(
                                ref=ref,
                                kind=native.OfferKind.CARD,
                                contents=(ref,),
                                actions=("open-" + ref,),
                                requires_selection=ref == "regular",
                            )
                            for ref in ("local", "regular", "guaranteed")
                        )
                        + (
                            native.Offer(
                                ref="gold",
                                kind=native.OfferKind.GOLD,
                                contents=("shared",),
                                actions=("gold",),
                            ),
                        ),
                        "actions": tuple(
                            native.Action(ref="open-" + ref, label=ref, enabled=True)
                            for ref in ("local", "regular", "guaranteed")
                        )
                        + (
                            native.Action(ref="gold", label="Gold", enabled=True),
                            native.Action(
                                ref="proceed",
                                label="Continue",
                                enabled=True,
                                operation=native.ActionOperation.PROCEED,
                            ),
                        ),
                    }
                ),
                "map": native.PublicMap(bosses=(), nodes=(), legal_destinations=None),
                "generated": {
                    ref: (
                        native.GeneratedCard(
                            source=native.NamedDefinition(
                                name=ref + " generation", definition_id="CARD." + ref.upper()
                            ),
                            card=native.CardDefinitionPreview.model_validate_json(PREVIEW),
                        ),
                    )
                    for ref in ("owned", "local", "shared")
                },
                "mechanisms": tuple(
                    native.MechanismView(
                        kind=native.MechanismKind.OTHER,
                        definition_id="CARD." + ref.upper(),
                        name=ref + " mechanism",
                        text=ref + " rules",
                        owner=ref,
                    )
                    for ref in ("owned", "local", "shared")
                ),
            }
        )

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            self.observation = self.observation.model_copy(
                update={
                    "stamp": self.observation.stamp.model_copy(
                        update={
                            "observation_sequence": self.observation.stamp.observation_sequence + 1
                        }
                    )
                }
            )
        obs = self.observation
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": obs.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "receipt": None,
                    "input_readiness": obs.input_readiness.value,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=obs
                ).to_json(),
            )
        assert request.url.path == "/action"
        step = native.StepRequest.model_validate_json(request.content)
        self.steps.append(step.step)
        state = obs.state
        assert isinstance(state, native.RunState)
        update: dict[str, object] = {}
        unknown = False
        if isinstance(step.step, native.Select):
            update = {"deck": (*state.deck, *step.step.refs), "selection": None}
            if state.screen is native.RunScreen.REWARDS:
                update.update(
                    offers=tuple(o for o in state.offers if o.ref != "regular"),
                    actions=tuple(a for a in state.actions if a.ref != "open-regular"),
                )
                unknown = self.unknown
            else:
                assert state.screen is native.RunScreen.EVENT
        else:
            assert isinstance(step.step, native.Interact)
            action = step.step.action
            if action == "open-regular":
                update = {
                    "selection": native.Selection(
                        prompt="Choose",
                        source=None,
                        min=0,
                        max=1,
                        candidates=("regular",),
                        selected=(),
                    )
                }
            elif action == "open-guaranteed":
                update = {
                    "deck": (*state.deck, "guaranteed"),
                    "offers": tuple(o for o in state.offers if o.ref != "guaranteed"),
                    "actions": tuple(a for a in state.actions if a.ref != "open-guaranteed"),
                }
            elif action == "gold":
                update = {
                    "offers": tuple(o for o in state.offers if o.ref != "gold"),
                    "actions": tuple(a for a in state.actions if a.ref != "gold"),
                }
            elif action == "proceed":
                update = {
                    "screen": native.RunScreen.SHOP,
                    "offers": (
                        native.Offer(
                            ref="shop",
                            kind=native.OfferKind.CARD,
                            contents=("shop",),
                            actions=("buy",),
                            price=50,
                        ),
                    ),
                    "actions": (
                        native.Action(ref="buy", label="Buy", enabled=True),
                        native.Action(
                            ref="close",
                            label="Leave",
                            enabled=True,
                            operation=native.ActionOperation.CLOSE_SHOP,
                        ),
                    ),
                }
            elif action == "buy":
                update = {
                    "deck": (*state.deck, "shop"),
                    "offers": (),
                    "actions": tuple(a for a in state.actions if a.ref == "close"),
                }
            elif action == "close":
                update = {
                    "screen": native.RunScreen.EVENT,
                    "offers": (),
                    "actions": (native.Action(ref="finish", label="Finish", enabled=True),),
                    "selection": native.Selection(
                        prompt="Event card",
                        source=None,
                        min=1,
                        max=1,
                        candidates=("gift",),
                        selected=(),
                    ),
                }
            else:
                assert action == "finish"
                self.observation = terminal(obs)
                return httpx.Response(
                    200,
                    json={
                        "operation_id": step.operation_id,
                        "status": "completed",
                        "boundary": "terminal",
                    },
                )
        changed = state.model_copy(update=update)
        readiness = (
            native.InputReadiness.SELECTION
            if changed.selection is not None
            else native.InputReadiness.READY
        )
        self.observation = obs.model_copy(update={"state": changed, "input_readiness": readiness})
        return httpx.Response(
            200,
            json={
                "operation_id": step.operation_id,
                "status": "unknown" if unknown else "completed",
                **(
                    {
                        "error": {
                            "code": "dispatch_failed",
                            "message": "Selection effect unconfirmed.",
                        }
                    }
                    if unknown
                    else {"boundary": readiness.value}
                ),
            },
        )

    def model(self, request: httpx.Request) -> httpx.Response:
        body = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
        self.inputs.append(body)
        tools = body["tools"]
        assert isinstance(tools, list)
        names: set[str] = set()
        for tool in tools:
            assert isinstance(tool, dict)
            name = tool["name"]
            assert isinstance(name, str)
            names.add(name)
        state = self.observation.state
        assert isinstance(state, native.RunState)
        index = len(self.inputs)
        calls: list[tuple[str, str, str]] = []
        if "run_deck_choice" in names:
            assert names == (
                {"run_deck_choice"} if self.minimal else {"run_deck_choice", "run_notes"}
            )
            offer = (
                "local"
                if not self.local_sent
                else "regular"
                if any(o.ref == "regular" for o in state.offers)
                else "guaranteed"
            )
            if offer == "local":
                self.local_sent = True
                if not self.minimal:
                    calls.append(
                        (
                            f"notes-{index}",
                            "run_notes",
                            '{"mode":"write","other":"Deck shared note."}',
                        )
                    )
            args = {
                "offer": offer,
                "choice": "skip" if offer == "local" else "take",
                "reason": "Measured " + offer,
            }
            if offer != "local":
                args.update(card=offer, **{"for": "draw"})
            calls.append((f"choice-{index}", "run_deck_choice", json.dumps(args)))
        elif state.screen is native.RunScreen.REWARDS:
            if not self.minimal:
                calls.append(
                    (f"notes-{index}", "run_notes", '{"mode":"write","other":"Run shared note."}')
                )
            calls.append(
                (f"gold-{index}", "run_act", '{"steps":[{"kind":"interact","action":"gold"}]}')
            )
        elif state.screen is native.RunScreen.SHOP:
            calls.append(
                (
                    f"shop-{index}",
                    "run_act",
                    '{"steps":[{"kind":"interact","action":"buy"},{"kind":"interact","action":"close"}]}',
                )
            )
        else:
            assert state.screen is native.RunScreen.EVENT
            calls.append(
                (
                    f"event-{index}",
                    "run_act",
                    '{"steps":[{"kind":"select","refs":["gift"]},{"kind":"interact","action":"finish"}]}',
                )
            )
        return httpx.Response(
            200, content=reply(*calls), headers={"Content-Type": "text/event-stream"}
        )


@pytest.mark.parametrize("minimal", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
def test_reward_roles_share_state_and_preserve_real_completion_evidence(
    tmp_path: Path,
    minimal: bool,
    unknown: bool,
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal, unknown=unknown)
        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="rewards",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL
            assert session.notes.read().other == ("" if minimal else "Run shared note.")
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            completions = journal.card_choices()
            assert len(completions) == 3
            assert [c.stream for c in completions] == ["deck:1", "deck:2", "deck:3"]
            assert (
                isinstance(completions[0].outcome, ReadCompleted) and completions[0].local_decline
            )
            assert completions[0].before == completions[0].after
            assert isinstance(completions[1].outcome, ActionUnknown) is unknown
            assert completions[2].before.state.deck == ("owned", "regular")
            assert completions[2].after is not None
            stats, _ = session.deck_stats.view(completions[2].after)
            assert stats["added"] == (1 if unknown else 2) and stats["skipped"] == 1
            requests = journal.requests()
            assert [r.stream for r in requests] == [
                "deck:1",
                "deck:2",
                "deck:3",
                "run:1",
                "run:1",
                "run:1",
            ]
            assert not any(
                isinstance(s, native.Interact) and s.action == "open-local" for s in game.steps
            )
            for record in requests[:3]:
                task = next(m for m in record.input if isinstance(m, RunTaskSnapshot))
                assert isinstance(task.content, RunActContent)
                assert task.content.observation is not None
                assert task.content.observation.map_topology is MISSING
                payload = json.loads(task.text)["observation"]
                assert "map_topology" not in payload and "dynamic_map" not in payload
                assert len(payload["choice"]["offers"]) == 1
            first_run = requests[3]
            body_input = game.inputs[3]["input"]
            assert isinstance(body_input, list)
            texts: list[str] = []
            for message in body_input:
                assert isinstance(message, dict)
                content = message.get("content")
                if isinstance(content, str):
                    texts.append(content)
            first_run_text = "\n".join(texts)
            assert "local mechanism" not in first_run_text
            assert "CARD.LOCAL" not in first_run_text
            assert "owned mechanism" in first_run_text
            assert "shared mechanism" in first_run_text
            assert '"generated"' in first_run_text
            reports = [m for m in first_run.input if isinstance(m, AutomaticSnapshot)]
            assert [m.content.automatic_progress.label for m in reports] == [
                "Skip",
                "CARD.REGULAR",
                "CARD.GUARANTEED",
            ]
            assert isinstance(reports[0].content.outcome, ActionCompleted)
            assert reports[0].content.outcome.completed_steps == 0
            assert isinstance(reports[1].content.outcome, ActionUnknown) is unknown
            delivered = reports[0].content.observation
            assert delivered is not None and isinstance(delivered.choice, dict)
            offers = delivered.choice["offers"]
            assert isinstance(offers, list) and len(offers) == 1
            assert isinstance(offers[0], dict) and offers[0]["ref"] == "gold"
            if not minimal:
                assert (
                    "Deck shared note."
                    in next(m for m in requests[2].input if isinstance(m, RunTaskSnapshot)).text
                )
            for record in requests[:3]:
                model_reply = record.attempts[-1].reply
                assert model_reply is not None
                call = next(
                    c
                    for c in AssistantMessage(model_reply.native_reply).tool_calls
                    if c.name == "run_deck_choice"
                )
                source = DecisionSource(
                    round="fixture",
                    seed="seed",
                    floor=1,
                    model_sequence=record.sequence,
                    archive=archive,
                )
                if unknown and record.stream == "deck:2":
                    with pytest.raises(ExtractionError, match="not confirmed complete"):
                        await extract_case(source, DecisionLabels(comment="fixture"))
                    continue
                if unknown and record.stream == "deck:3":
                    with pytest.raises(ReplayRejected):
                        await extract_case(source, DecisionLabels(comment="fixture"))
                    continue
                case = await extract_case(source, DecisionLabels(comment="fixture"))
                assert case.decision_offer == json.loads(call.arguments)["offer"]
                assert isinstance(case.events[0], TaskEvent)
            if not unknown:
                source = DecisionSource(
                    round="fixture",
                    seed="seed",
                    floor=1,
                    model_sequence=requests[2].sequence,
                    archive=archive,
                )
                with closing(sqlite3.connect(archive)) as connection:
                    row = connection.execute(
                        "SELECT * FROM card_choices WHERE stream='deck:3'"
                    ).fetchone()
                    assert row is not None
                    connection.execute("DELETE FROM card_choices WHERE stream='deck:3'")
                    connection.commit()
                    try:
                        with pytest.raises(ExtractionError, match="no exact typed completion"):
                            await extract_case(source, DecisionLabels(comment="fixture"))
                    finally:
                        connection.execute("INSERT INTO card_choices VALUES (?,?,?)", row)
                        connection.commit()

    asyncio.run(scenario())


def test_viewer_input_reaches_the_current_focused_role(tmp_path: Path) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=True)
        inputs = UserInputQueue()
        inputs.submit((UserInput("reward-input", "Current reward intervention."),))
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="viewer-reward",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
            user_input=inputs,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert inputs.delivered == ("reward-input",)
        assert "Current reward intervention." in json.dumps(game.inputs[0])
        assert "Current reward intervention." not in json.dumps(game.inputs[1:])

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_reward_report_survives_automatic_travel_and_act_entry(
    tmp_path: Path, minimal: bool
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal)
        state = game.observation.state
        assert isinstance(state, native.RunState)
        game.observation = game.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": state.offers[:1],
                        "actions": tuple(
                            a for a in state.actions if a.ref in ("open-local", "proceed")
                        ),
                    }
                )
            }
        )

        def mod(request: httpx.Request) -> httpx.Response:
            response = game.mod(request)
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content).step
                if isinstance(step, native.Interact) and step.action == "proceed":
                    current = game.observation
                    game.observation = current.model_copy(
                        update={
                            "location": current.location.model_copy(update={"node_id": "act2"}),
                            "state": current.state.model_copy(
                                update={
                                    "progress": current.state.progress.model_copy(update={"act": 2})
                                }
                            ),
                        }
                    )
            return response

        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="cross-act",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            requests = journal.requests()
            assert [record.stream for record in requests] == ["deck:1", "run:2", "run:2"]
            reports = [
                message for message in requests[1].input if isinstance(message, AutomaticSnapshot)
            ]
            assert any(
                report.content.automatic_progress.label == "Skip"
                and report.content.automatic_progress.reason == "Measured local"
                for report in reports
            )

    asyncio.run(scenario())


def test_local_decline_is_durable_when_no_later_model_request_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=True)
        state = game.observation.state
        assert isinstance(state, native.RunState)
        game.observation = game.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": state.offers[:1],
                        "actions": tuple(
                            a for a in state.actions if a.ref in ("open-local", "proceed")
                        ),
                    }
                )
            }
        )

        def mod(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content)
                assert isinstance(step.step, native.Interact) and step.step.action == "proceed"
                game.steps.append(step.step)
                game.observation = terminal(game.observation)
                return httpx.Response(
                    200,
                    json={
                        "operation_id": step.operation_id,
                        "status": "completed",
                        "boundary": "terminal",
                    },
                )
            return game.mod(request)

        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="local-only",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            assert len(journal.requests()) == len(journal.card_choices()) == 1
            choice = journal.card_choices()[0]
            assert choice.local_decline and isinstance(choice.outcome, ReadCompleted)
            assert choice.before == choice.after
            source = DecisionSource(
                round="fixture",
                seed="seed",
                floor=1,
                model_sequence=journal.requests()[0].sequence,
                archive=archive,
            )
            case = await extract_case(source, DecisionLabels(comment="fixture"))
            assert case.recorded_choices == ("skip",)
            assert [step.action for step in game.steps if isinstance(step, native.Interact)] == [
                "proceed"
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("minimal", [False, True])
def test_stopped_selection_keeps_focused_context_and_does_not_reopen_reward(
    tmp_path: Path,
    minimal: bool,
) -> None:
    async def scenario() -> None:
        game = RewardGame(minimal=minimal)
        rejected = False

        def mod(request: httpx.Request) -> httpx.Response:
            nonlocal rejected
            if request.url.path == "/action":
                step = native.StepRequest.model_validate_json(request.content)
                if (
                    isinstance(step.step, native.Select)
                    and step.step.refs == ("regular",)
                    and not rejected
                ):
                    rejected = True
                    game.steps.append(step.step)
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": step.operation_id,
                            "status": "failed",
                            "error": {
                                "code": "dispatch_failed",
                                "message": "Paused before selection.",
                            },
                        },
                    )
            return game.mod(request)

        archive = tmp_path / "archive.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="stopped",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=minimal,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )
        with closing(Journal.open(archive)) as journal:
            choices = journal.card_choices()
            assert len(choices) == 4
            assert [c.stream for c in choices] == ["deck:1", "deck:2", "deck:2", "deck:3"]
            assert choices[1].outcome.status.value == "stopped"
            assert choices[2].outcome.status.value == "completed"
            assert (
                sum(
                    isinstance(s, native.Interact) and s.action == "open-regular"
                    for s in game.steps
                )
                == 1
            )
            retried = [r for r in journal.requests() if r.stream == "deck:2"][1]
            assert "Paused before selection." in " ".join(
                m.text for m in retried.input if isinstance(m, ResultSnapshot)
            )
        (tmp_path / "actual-request-bodies.json").write_text(
            json.dumps(game.inputs, ensure_ascii=False, indent=2)
        )

    asyncio.run(scenario())
