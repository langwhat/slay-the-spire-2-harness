import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue, TypeAdapter
from test_combat import reply
from test_deck_choice import RewardGame
from test_lifecycle import terminal
from test_mod_client import CARD, COMBAT, RUN
from test_responses import configuration

from sts2_harness.client import ClientFailureKind, ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.combat import CombatAgent
from sts2_harness.config import Configuration, Driver, Protocol
from sts2_harness.decisions import ActTask
from sts2_harness.deck import deck_observation
from sts2_harness.deck_role import DeckAgent, deck_offers, validate_deck_choice
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import DecisionInterrupted, ToolRecoveryBudget
from sts2_harness.game import LiveGame, OperationKind, RunEndReason
from sts2_harness.interactive import InteractiveOptions, run_interactive
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.llm.messages import AssistantMessage, ToolCall
from sts2_harness.note_values import NotesState
from sts2_harness.notes import Notes
from sts2_harness.results import (
    ActionCompleted,
    ActionToolResult,
    QueryToolResult,
    ReasonCode,
    Rejected,
)
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog, ToolName
from sts2_harness.tools.reason_language import REASON_LANGUAGE_PLACEHOLDER
from sts2_harness_eval import ModelAnchor, Reached, reach
from sts2_harness_eval.decision_cases import DecisionLabels, DecisionSource
from sts2_harness_eval.decision_extract import extract_case
from sts2_harness_eval.decision_replay import DecisionVariant, rebuild_case


class AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> None:
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._handler(request)


def _multi_group_reward_observation(
    *, matching_cards: bool = False, game_language: str = "zhs"
) -> native.Observation:
    base = native.Observation.model_validate_json(RUN)
    assert isinstance(base.state, native.RunState)
    card = native.CardEntry.model_validate_json(CARD)
    first_card = card.model_copy(update={"definition_id": "CARD.ALPHA", "name": "猛击"})
    second_card = (
        first_card
        if matching_cards
        else card.model_copy(update={"definition_id": "CARD.BETA", "name": "顺劈斩"})
    )
    second_ref = "card-a" if matching_cards else "card-b"
    objects: dict[str, native.ObjectEntry] = {
        "owned": card.model_copy(update={"definition_id": "CARD.OWNED", "name": "打击"}),
        "card-a": first_card,
    }
    if second_ref not in objects:
        objects[second_ref] = second_card
    return base.model_copy(
        update={
            "game_language": game_language,
            "objects": objects,
            "descriptions": {ref: "Deal 8 damage." for ref in objects},
            "state": base.state.model_copy(
                update={
                    "screen": native.RunScreen.REWARDS,
                    "deck": ("owned",),
                    "offers": (
                        native.Offer(
                            ref="reward-a",
                            kind=native.OfferKind.CARD,
                            contents=("card-a",),
                            actions=("open-a",),
                            requires_selection=True,
                        ),
                        native.Offer(
                            ref="reward-b",
                            kind=native.OfferKind.CARD,
                            contents=(second_ref,),
                            actions=("open-b",),
                            requires_selection=True,
                        ),
                    ),
                    "actions": (
                        native.Action(ref="open-a", label="Card A", enabled=True),
                        native.Action(ref="open-b", label="Card B", enabled=True),
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
        }
    )


def _open_reward_overlay(observation: native.Observation, offer_ref: str) -> native.Observation:
    state = observation.state
    assert isinstance(state, native.RunState)
    target = next(offer for offer in state.offers if offer.ref == offer_ref)
    overlay = native.Offer(
        ref=f"overlay:{offer_ref}",
        kind=native.OfferKind.CARD,
        contents=target.contents,
        actions=tuple(f"pick:{ref}" for ref in target.contents),
        parent_offer_ref=offer_ref,
        requires_selection=True,
    )
    disabled_actions = tuple(
        action.model_copy(update={"enabled": False}) for action in state.actions
    )
    overlay_actions = tuple(
        native.Action(ref=f"pick:{ref}", label=ref, enabled=True, subject=ref)
        for ref in target.contents
    )
    return observation.model_copy(
        update={
            "input_readiness": native.InputReadiness.SELECTION,
            "state": state.model_copy(
                update={
                    "offers": (overlay, *state.offers),
                    "actions": (*overlay_actions, *disabled_actions),
                    "selection": native.Selection(
                        prompt="Choose a Card",
                        source=None,
                        min=0,
                        max=1,
                        candidates=target.contents,
                        selected=(),
                        offer_ref=offer_ref,
                    ),
                }
            ),
        }
    )


def _close_reward_overlay(observation: native.Observation) -> native.Observation:
    state = observation.state
    assert isinstance(state, native.RunState)
    logical_offers = tuple(offer for offer in state.offers if offer.parent_offer_ref is MISSING)
    restored_actions = tuple(
        action.model_copy(update={"enabled": True})
        for action in state.actions
        if not action.ref.startswith("pick:")
    )
    return observation.model_copy(
        update={
            "input_readiness": native.InputReadiness.READY,
            "state": state.model_copy(
                update={
                    "offers": logical_offers,
                    "actions": restored_actions,
                    "selection": None,
                }
            ),
        }
    )


def test_mod_client_reward_open_and_close_validate_contract() -> None:
    seen: list[tuple[str, dict[str, JsonValue]]] = []
    mode = "ok"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
        seen.append((request.url.path, payload))
        if mode == "duplicate":
            return httpx.Response(200, content=b'{"status":"opened","status":"opened"}')
        if mode == "rejected":
            return httpx.Response(
                400,
                json={
                    "status": "rejected",
                    "error": {
                        "code": "invalid_request",
                        "message": "Invalid reward request.",
                        "fields": ["offer"],
                    },
                },
            )
        if request.url.path == "/rewards/open":
            return httpx.Response(200, json={"status": "opened"})
        return httpx.Response(200, json={"status": "closed"})

    async def scenario() -> None:
        nonlocal mode
        route = native.RouteKey(
            game_instance_id="game", run_id="run", node_id="node", owner=native.DecisionOwner.RUN
        )
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            opened = await client.open_card_reward(route, "offer:1")
            assert isinstance(opened, native.RewardOpenReply)
            assert opened.status is native.RewardOpenStatus.OPENED

            closed = await client.close_card_reward(route, "offer:1")
            assert isinstance(closed, native.RewardCloseReply)
            assert closed.status is native.RewardCloseStatus.CLOSED

            mode = "rejected"
            rejected = await client.open_card_reward(route, "offer:1")
            assert isinstance(rejected, native.InvalidRequestReply)

            mode = "duplicate"
            with pytest.raises(ClientUnavailable) as error:
                await client.open_card_reward(route, "offer:1")
            assert error.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(scenario())
    assert seen[0][0] == "/rewards/open"
    assert seen[0][1]["offer"] == "offer:1"
    assert seen[1][0] == "/rewards/close"


@pytest.mark.parametrize("choose_skip", [False, True])
def test_card_reward_visual_open_runs_concurrently_and_executes_once(
    tmp_path: Path, choose_skip: bool
) -> None:
    async def scenario() -> None:
        obs = _multi_group_reward_observation()
        state = obs.state
        assert isinstance(state, native.RunState)
        # Single selectable card reward group.
        current = obs.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": state.offers[:1],
                        "actions": tuple(a for a in state.actions if a.ref != "open-b"),
                    }
                )
            }
        )
        open_started = asyncio.Event()
        release_open = asyncio.Event()
        model_received_before_open_finished = False
        open_calls: list[str] = []
        steps: list[native.NativeStep] = []

        async def mod(request: httpx.Request) -> httpx.Response:
            nonlocal current
            if request.url.path == "/rewards/open":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                open_calls.append(body.offer)
                open_started.set()
                await release_open.wait()
                current = _open_reward_overlay(current, body.offer)
                return httpx.Response(200, json={"status": "opened"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": current.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "receipt": None,
                        "input_readiness": current.input_readiness.value,
                    },
                )
            if request.url.path == "/state":
                current = current.model_copy(
                    update={
                        "stamp": current.stamp.model_copy(
                            update={"observation_sequence": current.stamp.observation_sequence + 1}
                        )
                    }
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current
                    ).to_json(),
                )
            assert request.url.path == "/action"
            wire = native.StepRequest.model_validate_json(request.content)
            steps.append(wire.step)
            st = current.state
            assert isinstance(st, native.RunState)
            if isinstance(wire.step, native.Select):
                assert st.selection is not None and st.selection.offer_ref == "reward-a"
                current = _close_reward_overlay(current)
                st2 = current.state
                assert isinstance(st2, native.RunState)
                if wire.step.refs:
                    current = current.model_copy(
                        update={
                            "state": st2.model_copy(
                                update={
                                    "deck": (*st2.deck, *wire.step.refs),
                                    "offers": tuple(o for o in st2.offers if o.ref != "reward-a"),
                                    "actions": tuple(a for a in st2.actions if a.ref != "open-a"),
                                }
                            )
                        }
                    )
                return httpx.Response(
                    200,
                    json={
                        "operation_id": wire.operation_id,
                        "status": "completed",
                        "boundary": "ready",
                    },
                )
            assert isinstance(wire.step, native.Interact) and wire.step.action == "proceed"
            current = terminal(current)
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )

        async def model(request: httpx.Request) -> httpx.Response:
            nonlocal model_received_before_open_finished
            # Model request arrived while /rewards/open has not finished yet.
            assert not release_open.is_set()
            model_received_before_open_finished = True
            await open_started.wait()
            release_open.set()
            choice: dict[str, object] = (
                {"offer": "reward-a", "choice": "skip", "reason": "牌库够厚了，先跳过。"}
                if choose_skip
                else {
                    "offer": "reward-a",
                    "choice": "take",
                    "card": "card-a",
                    "reason": "拿猛击补易伤。",
                }
            )
            return httpx.Response(
                200,
                content=reply(("c1", "run_deck_choice", json.dumps({"choices": [choice]}))),
                headers={"Content-Type": "text/event-stream"},
            )

        archive = tmp_path / "concurrent.sqlite"
        async with live_session(
            ModClient("http://mod", transport=AsyncTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="concurrent-open",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=AsyncTransport(model)),
            present_card_rewards=True,
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL

        assert model_received_before_open_finished is True
        assert open_calls == ["reward-a"]
        # Visually opened reward executes Select ONCE without ever sending Interact(open-a).
        assert steps == [
            native.Select(kind=native.StepKind.SELECT, refs=() if choose_skip else ("card-a",)),
            native.Interact(kind=native.StepKind.INTERACT, action="proceed"),
        ]
        with closing(Journal.open(archive)) as journal:
            preview_ops = [
                op
                for op in journal.operations()
                if op.request.kind is OperationKind.OPEN_CARD_REWARD
            ]
            assert len(preview_ops) == 1
            completions = journal.card_choices()
            assert len(completions) == 1
            assert completions[0].offer_ref == "reward-a"
            assert completions[0].batch_index == 1
            assert isinstance(completions[0].outcome, ActionCompleted)
            assert completions[0].outcome.completed_steps == 1
            source = DecisionSource(
                round="fixture",
                seed="seed",
                floor=1,
                model_sequence=journal.requests()[0].sequence,
                archive=archive,
            )
            case = await extract_case(source, DecisionLabels(comment="concurrent"))
            assert case.recorded_choices == (("skip",) if choose_skip else ("CARD.ALPHA",))

    asyncio.run(scenario())


@pytest.mark.parametrize("matching_cards", [False, True])
def test_model_choosing_b_before_previewed_a_closes_preview_and_preserves_order_and_reasons(
    tmp_path: Path, matching_cards: bool
) -> None:
    async def scenario() -> None:
        current = _multi_group_reward_observation(matching_cards=matching_cards)
        open_calls: list[str] = []
        close_calls: list[str] = []
        displayed_events: list[str] = []
        steps: list[native.NativeStep] = []

        async def mod(request: httpx.Request) -> httpx.Response:
            nonlocal current
            if request.url.path == "/display":
                payload = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
                assert isinstance(payload["reason"], str)
                displayed_events.append(payload["reason"])
                return httpx.Response(200, json={"status": "shown"})
            if request.url.path == "/rewards/open":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                open_calls.append(body.offer)
                current = _open_reward_overlay(current, body.offer)
                return httpx.Response(200, json={"status": "opened"})
            if request.url.path == "/rewards/close":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                close_calls.append(body.offer)
                st = current.state
                assert isinstance(st, native.RunState)
                assert st.selection is not None and st.selection.offer_ref == body.offer
                current = _close_reward_overlay(current)
                return httpx.Response(200, json={"status": "closed"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": current.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "receipt": None,
                        "input_readiness": current.input_readiness.value,
                    },
                )
            if request.url.path == "/state":
                current = current.model_copy(
                    update={
                        "stamp": current.stamp.model_copy(
                            update={"observation_sequence": current.stamp.observation_sequence + 1}
                        )
                    }
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current
                    ).to_json(),
                )
            assert request.url.path == "/action"
            wire = native.StepRequest.model_validate_json(request.content)
            steps.append(wire.step)
            st = current.state
            assert isinstance(st, native.RunState)
            if isinstance(wire.step, native.Interact):
                if wire.step.action == "open-a":
                    current = _open_reward_overlay(current, "reward-a")
                    boundary = "selection"
                elif wire.step.action == "open-b":
                    current = _open_reward_overlay(current, "reward-b")
                    boundary = "selection"
                else:
                    assert wire.step.action == "proceed"
                    current = terminal(current)
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": wire.operation_id,
                            "status": "completed",
                            "boundary": "terminal",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "operation_id": wire.operation_id,
                        "status": "completed",
                        "boundary": boundary,
                    },
                )
            assert isinstance(wire.step, native.Select)
            assert st.selection is not None and st.selection.offer_ref is not MISSING
            active_ref = st.selection.offer_ref
            current = _close_reward_overlay(current)
            st2 = current.state
            assert isinstance(st2, native.RunState)
            if wire.step.refs:
                action_to_drop = "open-a" if active_ref == "reward-a" else "open-b"
                current = current.model_copy(
                    update={
                        "state": st2.model_copy(
                            update={
                                "deck": (*st2.deck, *wire.step.refs),
                                "offers": tuple(o for o in st2.offers if o.ref != active_ref),
                                "actions": tuple(a for a in st2.actions if a.ref != action_to_drop),
                            }
                        )
                    }
                )
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        second_card_ref = "card-a" if matching_cards else "card-b"
        model_choices = [
            {
                "offer": "reward-b",
                "choice": "take",
                "card": second_card_ref,
                "reason": "先拿第二组补伤害。",
            },
            {
                "offer": "reward-a",
                "choice": "skip",
                "reason": "第一组先跳过，不稀释过牌。",
            },
        ]

        async def model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(
                    ("c-order", "run_deck_choice", json.dumps({"choices": model_choices}))
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        archive = tmp_path / "order.sqlite"
        async with live_session(
            ModClient("http://mod", transport=AsyncTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="b-before-a",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=AsyncTransport(model)),
            display_reasons=True,
            present_card_rewards=True,
        ) as session:
            ended = await session.run()
            assert ended.reason is RunEndReason.TERMINAL

        assert open_calls == ["reward-a"]
        assert close_calls == ["reward-a"]
        # Execution strictly follows the model's order: B first, then A.
        assert steps == [
            native.Interact(kind=native.StepKind.INTERACT, action="open-b"),
            native.Select(kind=native.StepKind.SELECT, refs=(second_card_ref,)),
            native.Interact(kind=native.StepKind.INTERACT, action="open-a"),
            native.Select(kind=native.StepKind.SELECT, refs=()),
            native.Interact(kind=native.StepKind.INTERACT, action="proceed"),
        ]
        assert displayed_events[:2] == [
            "先拿第二组补伤害。",
            "第一组先跳过，不稀释过牌。",
        ]
        with closing(Journal.open(archive)) as journal:
            completions = journal.card_choices()
            assert [(c.batch_index, c.offer_ref, c.choice.value) for c in completions] == [
                (1, "reward-b", "take"),
                (2, "reward-a", "skip"),
            ]
            source = DecisionSource(
                round="fixture",
                seed="seed",
                floor=1,
                model_sequence=journal.requests()[0].sequence,
                archive=archive,
            )
            case = await extract_case(source, DecisionLabels(comment="b-before-a"))
            expected_b_id = "CARD.ALPHA" if matching_cards else "CARD.BETA"
            assert [(g.offer, g.choices) for g in case.recorded_reward_groups] == [
                ("reward-b", (expected_b_id,)),
                ("reward-a", ("skip",)),
            ]

    asyncio.run(scenario())


def test_direct_grant_existing_active_and_standalone_selection_skip_visual_open(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        # 1. Direct-grant only (requires_selection=False): never calls /rewards/open.
        game = RewardGame(minimal=True)
        state = game.observation.state
        assert isinstance(state, native.RunState)
        game.observation = game.observation.model_copy(
            update={
                "state": state.model_copy(
                    update={
                        "offers": tuple(
                            o for o in state.offers if o.ref in ("local", "guaranteed")
                        ),
                        "actions": tuple(
                            a
                            for a in state.actions
                            if a.ref in ("open-local", "open-guaranteed", "proceed")
                        ),
                    }
                )
            }
        )
        open_calls: list[str] = []

        def mod(request: httpx.Request) -> httpx.Response:
            if request.url.path in ("/rewards/open", "/rewards/close"):
                open_calls.append(request.url.path)
                return httpx.Response(200, json={"status": "skipped"})
            return game.mod(request)

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="direct-only",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "direct.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
            present_card_rewards=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert open_calls == []

        # 2. Standalone / mod-intercepted selection (RunScreen.SELECTION): no /rewards/open.
        sel_game = RewardGame(minimal=True)
        sel_state = sel_game.observation.state
        assert isinstance(sel_state, native.RunState)
        sel_game.observation = sel_game.observation.model_copy(
            update={
                "input_readiness": native.InputReadiness.SELECTION,
                "state": sel_state.model_copy(
                    update={
                        "screen": native.RunScreen.SELECTION,
                        "offers": (),
                        "actions": (native.Action(ref="finish", label="Finish", enabled=True),),
                        "selection": native.Selection(
                            prompt="Choose a Card",
                            source=None,
                            min=0,
                            max=1,
                            candidates=("gift",),
                            selected=(),
                        ),
                    }
                ),
            }
        )
        sel_game.event_handoff = True
        sel_open_calls: list[str] = []

        def sel_mod(request: httpx.Request) -> httpx.Response:
            if request.url.path in ("/rewards/open", "/rewards/close"):
                sel_open_calls.append(request.url.path)
                return httpx.Response(200, json={"status": "already_open"})
            return sel_game.mod(request)

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(sel_mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="standalone-sel",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "standalone.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(sel_game.model)),
            present_card_rewards=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert sel_open_calls == []

        # 3. Existing active card reward group: no /rewards/open and PENDING_CHOICE still applies.
        active_obs = _open_reward_overlay(_multi_group_reward_observation(), "reward-b")
        route = native.RouteKey.model_validate(active_obs.location.model_dump())
        bad_order = ToolCall(
            "call-1",
            ToolName.RUN_DECK_CHOICE.value,
            json.dumps(
                {
                    "choices": [
                        {"offer": "reward-a", "choice": "skip", "reason": "Skip A."},
                        {"offer": "reward-b", "choice": "skip", "reason": "Skip B."},
                    ]
                }
            ),
        )
        delivered_active = deck_observation(active_obs, deck_offers(active_obs))
        rejected = validate_deck_choice(bad_order, ActTask(route), delivered_active, active_obs)
        assert isinstance(rejected, QueryToolResult)
        assert isinstance(rejected.outcome, Rejected)
        assert rejected.outcome.reason.code is ReasonCode.PENDING_CHOICE

        active_open_calls: list[str] = []

        class RecordingPreview:
            @property
            def card_reward_preview_enabled(self) -> bool:
                return True

            def start_open_card_reward(self, route: native.RouteKey, offer: str) -> None:
                active_open_calls.append(offer)

            async def cancel_open_card_reward(self) -> None:
                return None

            async def open_card_reward(
                self, route: native.RouteKey, offer: str
            ) -> native.RewardOpenReply:
                active_open_calls.append(offer)
                return native.RewardOpenReply(status=native.RewardOpenStatus.OPENED)

            async def close_card_reward(
                self, route: native.RouteKey, offer: str
            ) -> native.RewardCloseReply:
                return native.RewardCloseReply(status=native.RewardCloseStatus.CLOSED)

        async def observe_active() -> native.Observation:
            return active_obs

        async def unused_inspect(query: native.InspectQuery) -> native.InspectReply:
            raise AssertionError(query)

        async def unused_catalog(request: native.CatalogRequest) -> native.CatalogReply:
            raise AssertionError(request)

        def active_model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(
                    (
                        "c-active",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "choices": [
                                    {"offer": "reward-b", "choice": "skip", "reason": "Skip B."},
                                    {"offer": "reward-a", "choice": "skip", "reason": "Skip A."},
                                ]
                            }
                        ),
                    )
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(
            configuration(tmp_path), transport=httpx.MockTransport(active_model)
        ) as model:
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=observe_active,
                inspect=unused_inspect,
                catalog=unused_catalog,
                notes=Notes(""),
                focused_deck=True,
            )
            agent = DeckAgent(
                lambda: model,
                tools,
                ToolRecoveryBudget(),
                reward_preview=RecordingPreview(),
            )
            await agent.decide(ActTask(route), active_obs)
        assert active_open_calls == []

    asyncio.run(scenario())


def test_visual_open_fallback_on_timeout_and_interactive_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        # Timeout BEFORE opening falls back cleanly to Interact+Select.
        game = RewardGame(minimal=True)
        open_attempts = 0

        def timeout_mod(request: httpx.Request) -> httpx.Response:
            nonlocal open_attempts
            if request.url.path == "/rewards/open":
                open_attempts += 1
                raise httpx.ReadTimeout("preview timed out", request=request)
            return game.mod(request)

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(timeout_mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="timeout-fallback",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "timeout.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(game.model)),
            present_card_rewards=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert open_attempts == 1
        assert [step.action for step in game.steps if isinstance(step, native.Interact)][:2] == [
            "open-regular",
            "open-guaranteed",
        ]

        # Fallback when /rewards/open times out AFTER Mod already opened the screen:
        # DeckAgent still accepts the open preview and sends Select once without Interact(open).
        obs_after = _multi_group_reward_observation()
        st_after = obs_after.state
        assert isinstance(st_after, native.RunState)
        current_after = obs_after.model_copy(
            update={
                "state": st_after.model_copy(
                    update={
                        "offers": st_after.offers[:1],
                        "actions": tuple(a for a in st_after.actions if a.ref != "open-b"),
                    }
                )
            }
        )
        after_steps: list[native.NativeStep] = []

        def timeout_after_open_mod(request: httpx.Request) -> httpx.Response:
            nonlocal current_after
            if request.url.path == "/rewards/open":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                current_after = _open_reward_overlay(current_after, body.offer)
                raise httpx.ReadTimeout("reply lost after open", request=request)
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": current_after.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "receipt": None,
                        "input_readiness": current_after.input_readiness.value,
                    },
                )
            if request.url.path == "/state":
                current_after = current_after.model_copy(
                    update={
                        "stamp": current_after.stamp.model_copy(
                            update={
                                "observation_sequence": current_after.stamp.observation_sequence + 1
                            }
                        )
                    }
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current_after
                    ).to_json(),
                )
            assert request.url.path == "/action"
            wire = native.StepRequest.model_validate_json(request.content)
            after_steps.append(wire.step)
            if isinstance(wire.step, native.Select):
                current_after = _close_reward_overlay(current_after)
                st2 = current_after.state
                assert isinstance(st2, native.RunState)
                current_after = current_after.model_copy(
                    update={
                        "state": st2.model_copy(
                            update={
                                "deck": (*st2.deck, *wire.step.refs),
                                "offers": (),
                                "actions": tuple(a for a in st2.actions if a.ref == "proceed"),
                            }
                        )
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "operation_id": wire.operation_id,
                        "status": "completed",
                        "boundary": "ready",
                    },
                )
            current_after = terminal(current_after)
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )

        def after_open_model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(
                    (
                        "c-after",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "choices": [
                                    {
                                        "offer": "reward-a",
                                        "choice": "take",
                                        "card": "card-a",
                                        "reason": "拿猛击。",
                                    }
                                ]
                            }
                        ),
                    )
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(timeout_after_open_mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="timeout-after-open",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            tmp_path / "timeout-after-open.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(after_open_model)),
            present_card_rewards=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL
        assert after_steps == [
            native.Select(kind=native.StepKind.SELECT, refs=("card-a",)),
            native.Interact(kind=native.StepKind.INTERACT, action="proceed"),
        ]

        # run_interactive enables present_card_rewards=True for viewer runs.
        obs = _multi_group_reward_observation()
        st = obs.state
        assert isinstance(st, native.RunState)
        current = obs.model_copy(
            update={
                "state": st.model_copy(
                    update={
                        "offers": st.offers[:1],
                        "actions": tuple(a for a in st.actions if a.ref != "open-b"),
                    }
                )
            }
        )
        stop = asyncio.Event()
        control_mode = "paused"
        interactive_opens: list[str] = []

        def interactive_mod(request: httpx.Request) -> httpx.Response:
            nonlocal current, control_mode
            if request.url.path == "/control":
                control_mode = json.loads(request.content)["mode"]
                return httpx.Response(200, json={"mode": control_mode})
            if request.url.path == "/display":
                return httpx.Response(200, json={"status": "shown"})
            if request.url.path == "/rewards/open":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                interactive_opens.append(body.offer)
                current = _open_reward_overlay(current, body.offer)
                return httpx.Response(200, json={"status": "opened"})
            if request.url.path == "/status":
                # Auto-resume after initial pause confirmation.
                if control_mode == "paused":
                    control_mode = "running"
                return httpx.Response(
                    200,
                    json={
                        "location": current.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": control_mode,
                        "receipt": None,
                        "input_readiness": current.input_readiness.value,
                    },
                )
            if request.url.path == "/state":
                current = current.model_copy(
                    update={
                        "stamp": current.stamp.model_copy(
                            update={"observation_sequence": current.stamp.observation_sequence + 1}
                        )
                    }
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current
                    ).to_json(),
                )
            assert request.url.path == "/action"
            wire = native.StepRequest.model_validate_json(request.content)
            if isinstance(wire.step, native.Select):
                current = _close_reward_overlay(current)
                st2 = current.state
                assert isinstance(st2, native.RunState)
                current = current.model_copy(
                    update={
                        "state": st2.model_copy(
                            update={
                                "deck": (*st2.deck, *wire.step.refs),
                                "offers": (),
                                "actions": tuple(a for a in st2.actions if a.ref == "proceed"),
                            }
                        )
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "operation_id": wire.operation_id,
                        "status": "completed",
                        "boundary": "ready",
                    },
                )
            current = terminal(current)
            stop.set()
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )

        def interactive_model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(
                    (
                        "c-int",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "choices": [
                                    {
                                        "offer": "reward-a",
                                        "choice": "take",
                                        "card": "card-a",
                                        "reason": "拿猛击。",
                                    }
                                ]
                            }
                        ),
                    )
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        model_config = configuration(tmp_path)
        real_init = Llm.__init__

        def fake_llm_init(self: Llm, config: object, **kwargs: object) -> None:
            real_init(self, model_config, transport=httpx.MockTransport(interactive_model))

        monkeypatch.setattr(Llm, "__init__", fake_llm_init)

        async def immediate(_seconds: float) -> None:
            return None

        await run_interactive(
            InteractiveOptions("http://127.0.0.1:17877", tmp_path / "live"),
            Configuration(Driver.BUILTIN, model_config, minimal=True),
            stop_requested=stop,
            client_factory=lambda: ModClient(
                "http://127.0.0.1:17877", transport=httpx.MockTransport(interactive_mod)
            ),
            wait=immediate,
        )
        assert interactive_opens == ["reward-a"]

    asyncio.run(scenario())


def _extract_wire_tools(
    protocol: Protocol, body: dict[str, JsonValue]
) -> dict[str, dict[str, JsonValue]]:
    tools = body["tools"]
    assert isinstance(tools, list)
    result: dict[str, dict[str, JsonValue]] = {}
    for tool in tools:
        assert isinstance(tool, dict)
        if protocol is Protocol.CHAT_COMPLETIONS:
            fn = tool["function"]
            assert isinstance(fn, dict)
            name, schema = fn["name"], fn["parameters"]
        elif protocol is Protocol.MESSAGES:
            name, schema = tool["name"], tool["input_schema"]
        else:
            name, schema = tool["name"], tool["parameters"]
        assert isinstance(name, str) and isinstance(schema, dict)
        result[name] = schema
    return result


def _reason_descriptions(schema: dict[str, JsonValue]) -> list[str]:
    found: list[str] = []

    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            reason = node.get("reason")
            if isinstance(reason, dict):
                description = reason.get("description")
                if isinstance(description, str):
                    found.append(description)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


@pytest.mark.parametrize(
    ("game_language", "expected_phrase", "expect_chinese_style"),
    [
        ("zhs", "Simplified Chinese", True),
        ("zht", "Traditional Chinese", True),
        ("eng", "English", False),
        ("jpn", "Japanese", False),
        ("", "the game's displayed language", False),
        ("future-locale", "the game's displayed language", False),
    ],
)
@pytest.mark.parametrize("protocol", list(Protocol))
def test_live_game_language_resolves_reason_placeholder_across_roles_and_protocols(
    tmp_path: Path,
    game_language: str,
    expected_phrase: str,
    expect_chinese_style: bool,
    protocol: Protocol,
) -> None:
    async def scenario() -> None:
        run_obs = native.Observation.model_validate_json(RUN).model_copy(
            update={"game_language": game_language}
        )
        combat_obs = native.Observation.model_validate_json(COMBAT).model_copy(
            update={"game_language": game_language}
        )
        deck_obs = _multi_group_reward_observation(game_language=game_language)

        captured: list[dict[str, JsonValue]] = []

        def capture_model(request: httpx.Request) -> httpx.Response:
            captured.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
            return httpx.Response(413, json={"error": {"message": "capture-stop"}})

        async def unused_inspect(query: native.InspectQuery) -> native.InspectReply:
            raise AssertionError(query)

        async def unused_catalog(request: native.CatalogRequest) -> native.CatalogReply:
            raise AssertionError(request)

        async with Llm(
            configuration(tmp_path, LLM_PROTOCOL=protocol.value),
            transport=httpx.MockTransport(capture_model),
        ) as llm:
            # 1. RunAgent (run_act, run_plan, run_next)
            async def observe_run() -> native.Observation:
                return run_obs

            run_tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=observe_run,
                inspect=unused_inspect,
                catalog=unused_catalog,
                notes=Notes(""),
            )
            run_agent = RunAgent(lambda: llm, run_tools, ToolRecoveryBudget())
            run_route = native.RouteKey.model_validate(run_obs.location.model_dump())
            with pytest.raises(DecisionInterrupted):
                await run_agent.decide(ActTask(run_route), run_obs)

            # 2. DeckAgent (run_deck_choice take + skip)
            async def observe_deck() -> native.Observation:
                return deck_obs

            deck_tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=observe_deck,
                inspect=unused_inspect,
                catalog=unused_catalog,
                notes=Notes(""),
                focused_deck=True,
            )
            deck_agent = DeckAgent(lambda: llm, deck_tools, ToolRecoveryBudget())
            deck_route = native.RouteKey.model_validate(deck_obs.location.model_dump())
            with pytest.raises(DecisionInterrupted):
                await deck_agent.decide(ActTask(deck_route), deck_obs)

            # 3. CombatAgent (combat_act)
            async def observe_combat() -> native.Observation:
                return combat_obs

            combat_tools = ToolCatalog(
                native.DecisionOwner.COMBAT,
                observe=observe_combat,
                inspect=unused_inspect,
                catalog=unused_catalog,
            )
            combat_agent = CombatAgent(
                llm, combat_tools, ToolRecoveryBudget(), opening_notes=Notes("").read()
            )
            combat_route = native.RouteKey.model_validate(combat_obs.location.model_dump())
            with pytest.raises(DecisionInterrupted):
                await combat_agent.decide(ActTask(combat_route), combat_obs)

        assert len(captured) == 3
        expected_tools = (
            ("run_act", "run_plan", "run_next"),
            ("run_deck_choice",),
            ("combat_act",),
        )
        for body, names in zip(captured, expected_tools, strict=True):
            wire_text = json.dumps(body, ensure_ascii=False)
            assert REASON_LANGUAGE_PLACEHOLDER not in wire_text
            wire_tools = _extract_wire_tools(protocol, body)
            for name in names:
                descriptions = _reason_descriptions(wire_tools[name])
                assert len(descriptions) == (2 if name == "run_deck_choice" else 1)
                for text in descriptions:
                    assert expected_phrase in text
                    assert "CARD.*" in text
                    assert "temporary refs" in text
                    assert "parenthesized IDs" in text
                    assert ("conversational" in text) is expect_chinese_style
            # Non-reason tools/fields stay in English and keep native definition IDs / refs.
            if "run_notes" in wire_tools:
                notes_wire = json.dumps(wire_tools["run_notes"], ensure_ascii=False)
                assert "Simplified Chinese" not in notes_wire
                assert "Traditional Chinese" not in notes_wire

    asyncio.run(scenario())


def test_mid_run_language_switch_and_journal_replay_reproducibility(tmp_path: Path) -> None:
    async def scenario() -> None:
        # Mid-run game_language switch updates tool schema on the very next request.
        obs_eng = native.Observation.model_validate_json(RUN).model_copy(
            update={"game_language": "eng"}
        )
        obs_zhs = obs_eng.model_copy(update={"game_language": "zhs"})
        current = obs_eng
        bodies: list[dict[str, JsonValue]] = []

        async def observe() -> native.Observation:
            return current

        async def unused_inspect(query: native.InspectQuery) -> native.InspectReply:
            raise AssertionError(query)

        async def unused_catalog(request: native.CatalogRequest) -> native.CatalogReply:
            raise AssertionError(request)

        def switch_model(request: httpx.Request) -> httpx.Response:
            bodies.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
            return httpx.Response(
                200,
                content=reply(("act", "run_act", '{"steps":[{"kind":"interact","action":"a1"}]}')),
                headers={"Content-Type": "text/event-stream"},
            )

        async with Llm(configuration(tmp_path), transport=httpx.MockTransport(switch_model)) as llm:
            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=observe,
                inspect=unused_inspect,
                catalog=unused_catalog,
                notes=Notes(""),
            )
            agent = RunAgent(lambda: llm, tools, ToolRecoveryBudget())
            route = native.RouteKey.model_validate(obs_eng.location.model_dump())
            await agent.decide(ActTask(route), obs_eng)
            await agent.accept(ActionToolResult(ActionCompleted(completed_steps=1), obs_zhs))
            current = obs_zhs
            await agent.decide(ActTask(route), obs_zhs)

        first_reason = _reason_descriptions(
            _extract_wire_tools(Protocol.RESPONSES, bodies[0])["run_act"]
        )[0]
        second_reason = _reason_descriptions(
            _extract_wire_tools(Protocol.RESPONSES, bodies[1])["run_act"]
        )[0]
        assert "English" in first_reason and "conversational" not in first_reason
        assert "Simplified Chinese" in second_reason and "conversational" in second_reason

        # Journal records the resolved schema and rebuild_case reproduces it identically.
        game = RewardGame(minimal=True)
        game.observation = game.observation.model_copy(update={"game_language": "jpn"})
        archive = tmp_path / "jpn.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(game.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="jpn-repro",
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

        with closing(Journal.open(archive)) as journal:
            first_deck = journal.requests()[0]
            assert len(first_deck.tools) == 1
            recorded_params = first_deck.tools[0].parameters
            recorded_reasons = _reason_descriptions(recorded_params)
            assert len(recorded_reasons) == 2
            assert all(
                "Japanese" in text
                and "conversational" not in text
                and REASON_LANGUAGE_PLACEHOLDER not in text
                for text in recorded_reasons
            )
            source = DecisionSource(
                round="fixture",
                seed="seed",
                floor=1,
                model_sequence=first_deck.sequence,
                archive=archive,
            )
            case = await extract_case(source, DecisionLabels(comment="jpn-repro"))
            async with Llm(configuration(tmp_path)) as offline_llm:
                rebuilt = await rebuild_case(case, offline_llm, variant=DecisionVariant.DECK)
            rebuilt_params = rebuilt.prepared.tools[0].input_schema()
            assert rebuilt_params == recorded_params

    asyncio.run(scenario())


@pytest.mark.parametrize("choose_b_first", [False, True])
def test_archive_to_replay_reproduces_preview_open_and_close_authority(
    tmp_path: Path, choose_b_first: bool
) -> None:
    async def scenario() -> None:
        current = _multi_group_reward_observation()
        next_node_base = native.Observation.model_validate_json(RUN)
        assert isinstance(next_node_base.state, native.RunState)
        next_node = next_node_base.model_copy(
            update={
                "game_language": "zhs",
                "location": next_node_base.location.model_copy(update={"node_id": "node-after"}),
                "state": next_node_base.state.model_copy(
                    update={
                        "screen": native.RunScreen.EVENT,
                        "actions": (
                            native.Action(ref="event-continue", label="Leave", enabled=True),
                        ),
                    }
                ),
            }
        )

        def mod(request: httpx.Request) -> httpx.Response:
            nonlocal current
            if request.url.path == "/rewards/open":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                current = _open_reward_overlay(current, body.offer)
                return httpx.Response(200, json={"status": "opened"})
            if request.url.path == "/rewards/close":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                st = current.state
                assert isinstance(st, native.RunState)
                assert st.selection is not None and st.selection.offer_ref == body.offer
                current = _close_reward_overlay(current)
                return httpx.Response(200, json={"status": "closed"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": current.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "receipt": None,
                        "input_readiness": current.input_readiness.value,
                    },
                )
            if request.url.path == "/state":
                current = current.model_copy(
                    update={
                        "stamp": current.stamp.model_copy(
                            update={"observation_sequence": current.stamp.observation_sequence + 1}
                        )
                    }
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current
                    ).to_json(),
                )
            assert request.url.path == "/action"
            wire = native.StepRequest.model_validate_json(request.content)
            st = current.state
            assert isinstance(st, native.RunState)
            if isinstance(wire.step, native.Interact):
                if wire.step.action == "open-a":
                    current = _open_reward_overlay(current, "reward-a")
                    boundary = "selection"
                elif wire.step.action == "open-b":
                    current = _open_reward_overlay(current, "reward-b")
                    boundary = "selection"
                elif wire.step.action == "proceed":
                    current = next_node
                    boundary = "ready"
                else:
                    assert wire.step.action == "event-continue"
                    current = terminal(current)
                    boundary = "terminal"
                return httpx.Response(
                    200,
                    json={
                        "operation_id": wire.operation_id,
                        "status": "completed",
                        "boundary": boundary,
                    },
                )
            assert isinstance(wire.step, native.Select)
            assert st.selection is not None and st.selection.offer_ref is not MISSING
            active_ref = st.selection.offer_ref
            current = _close_reward_overlay(current)
            st2 = current.state
            assert isinstance(st2, native.RunState)
            if wire.step.refs:
                drop_action = "open-a" if active_ref == "reward-a" else "open-b"
                current = current.model_copy(
                    update={
                        "state": st2.model_copy(
                            update={
                                "deck": (*st2.deck, *wire.step.refs),
                                "offers": tuple(o for o in st2.offers if o.ref != active_ref),
                                "actions": tuple(a for a in st2.actions if a.ref != drop_action),
                            }
                        )
                    }
                )
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "ready",
                },
            )

        choices = (
            [
                {"offer": "reward-b", "choice": "take", "card": "card-b", "reason": "先拿顺劈斩。"},
                {"offer": "reward-a", "choice": "skip", "reason": "跳过猛击。"},
            ]
            if choose_b_first
            else [
                {"offer": "reward-a", "choice": "take", "card": "card-a", "reason": "先拿猛击。"},
                {"offer": "reward-b", "choice": "skip", "reason": "跳过顺劈斩。"},
            ]
        )
        replies = iter(
            (
                reply(("c-deck", "run_deck_choice", json.dumps({"choices": choices}))),
                reply(
                    (
                        "c-run",
                        "run_act",
                        json.dumps(
                            {
                                "steps": [{"kind": "interact", "action": "event-continue"}],
                                "reason": "直接离开事件。",
                            }
                        ),
                    )
                ),
            )
        )

        def model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=next(replies), headers={"Content-Type": "text/event-stream"}
            )

        archive = tmp_path / f"replay-{choose_b_first}.sqlite"
        recording_id = f"preview-replay-{choose_b_first}"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id=recording_id,
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
            present_card_rewards=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL

        with closing(Journal.open(archive)) as journal:
            ops = journal.operations()
            open_ops = [op for op in ops if op.request.kind is OperationKind.OPEN_CARD_REWARD]
            close_ops = [op for op in ops if op.request.kind is OperationKind.CLOSE_CARD_REWARD]
            assert len(open_ops) == 1
            assert len(close_ops) == (1 if choose_b_first else 0)
            model_records = journal.requests()
            assert len(model_records) == 2
            deck_record, run_record = model_records

        for record in (deck_record, run_record):
            anchor = ModelAnchor(recording_id, record.sequence)
            async with reach(archive, anchor) as result:
                assert result == Reached(
                    anchor, record.confirmed_operation_cursor, record.semantic_digest
                )

    asyncio.run(scenario())


def test_display_reason_sanitizes_internal_ids_and_refs_while_preserving_raw_journal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        base = _multi_group_reward_observation(game_language="zhs")
        base_state = base.state
        assert isinstance(base_state, native.RunState)
        card = native.CardEntry.model_validate_json(CARD)
        jest_zhs = card.model_copy(update={"definition_id": "CARD.JEST", "name": "戏法"})
        obs_zhs = base.model_copy(
            update={
                "objects": {
                    **base.objects,
                    "card:13:323": jest_zhs,
                },
                "descriptions": {
                    **base.descriptions,
                    "card:13:323": "抽1张牌。",
                },
                "state": base_state.model_copy(
                    update={
                        "offers": (
                            native.Offer(
                                ref="offer:1:1",
                                kind=native.OfferKind.CARD,
                                contents=("card:13:323",),
                                actions=("open-a",),
                                requires_selection=True,
                            ),
                        ),
                        "actions": tuple(a for a in base_state.actions if a.ref != "open-b"),
                    }
                ),
            }
        )

        # 1. Direct Execution.display_reason behavioral checks across formats and locales.
        shown: list[str] = []

        async def record_display(text: str) -> native.DisplayReply:
            shown.append(text)
            return native.DisplayReply(status=native.DisplayStatus.SHOWN)

        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ok"})),
        ) as client:
            execution = Execution(
                LiveGame(client, namespace="ns", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                display=record_display,
            )
            await execution.display_reason(
                "拿 [[CARD.JEST]] (card:13:323) 补过牌，先不拿 [[CARD.UNKNOWN]] (card:99:999)。",
                observation=obs_zhs,
            )
            await execution.display_reason("先打戏法 (card:13:323)，再打猛击（CARD.ALPHA）。")
            await execution.display_reason("用 card:13:323 配合 CARD.ALPHA 打爆发。")
            await execution.display_reason("选戏法 [[CARD.JEST]]。")

            obs_eng = obs_zhs.model_copy(
                update={
                    "game_language": "eng",
                    "objects": {
                        "card:13:323": jest_zhs.model_copy(update={"name": "Jest"}),
                    },
                }
            )
            await execution.display_reason(
                "Play [[CARD.JEST]] (card:13:323) for cheap cycle.",
                observation=obs_eng,
            )

        assert shown == [
            "拿 戏法 补过牌，先不拿。",
            "先打戏法，再打猛击。",
            "用 戏法 配合 猛击 打爆发。",
            "选戏法。",
            "Play Jest for cheap cycle.",
        ]
        for line in shown:
            assert "CARD." not in line
            assert "card:" not in line
            assert "[[" not in line
            assert "()" not in line

        # 2. End-to-end live_session: viewer receives sanitized text; Journal preserves raw args.
        current = obs_zhs
        displayed_live: list[str] = []
        raw_reason = "拿 [[CARD.JEST]] (card:13:323) 补过牌，不选 CARD.UNKNOWN (card:99:999)。"

        def mod(request: httpx.Request) -> httpx.Response:
            nonlocal current
            if request.url.path == "/display":
                payload = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
                assert isinstance(payload["reason"], str)
                displayed_live.append(payload["reason"])
                return httpx.Response(200, json={"status": "shown"})
            if request.url.path == "/rewards/open":
                body = native.RewardOpenRequest.model_validate_json(request.content)
                current = _open_reward_overlay(current, body.offer)
                return httpx.Response(200, json={"status": "opened"})
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": current.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "receipt": None,
                        "input_readiness": current.input_readiness.value,
                    },
                )
            if request.url.path == "/state":
                current = current.model_copy(
                    update={
                        "stamp": current.stamp.model_copy(
                            update={"observation_sequence": current.stamp.observation_sequence + 1}
                        )
                    }
                )
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=current
                    ).to_json(),
                )
            assert request.url.path == "/action"
            wire = native.StepRequest.model_validate_json(request.content)
            if isinstance(wire.step, native.Select):
                current = _close_reward_overlay(current)
                st2 = current.state
                assert isinstance(st2, native.RunState)
                current = current.model_copy(
                    update={
                        "state": st2.model_copy(
                            update={
                                "deck": (*st2.deck, *wire.step.refs),
                                "offers": (),
                                "actions": tuple(a for a in st2.actions if a.ref == "proceed"),
                            }
                        )
                    }
                )
                return httpx.Response(
                    200,
                    json={
                        "operation_id": wire.operation_id,
                        "status": "completed",
                        "boundary": "ready",
                    },
                )
            current = terminal(current)
            return httpx.Response(
                200,
                json={
                    "operation_id": wire.operation_id,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )

        def model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply(
                    (
                        "c-sanitize",
                        "run_deck_choice",
                        json.dumps(
                            {
                                "choices": [
                                    {
                                        "offer": "offer:1:1",
                                        "choice": "take",
                                        "card": "card:13:323",
                                        "reason": raw_reason,
                                    }
                                ]
                            }
                        ),
                    )
                ),
                headers={"Content-Type": "text/event-stream"},
            )

        archive = tmp_path / "sanitize.sqlite"
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="sanitize-live",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(),
                minimal=True,
            ),
            archive,
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
            display_reasons=True,
            present_card_rewards=True,
        ) as session:
            assert (await session.run()).reason is RunEndReason.TERMINAL

        assert displayed_live[0] == "拿 戏法 补过牌，不选。"
        assert all("CARD." not in line and "card:" not in line for line in displayed_live)
        with closing(Journal.open(archive)) as journal:
            recorded_reply = journal.requests()[0].attempts[-1].reply
            assert recorded_reply is not None
            raw_call_args = AssistantMessage(recorded_reply.native_reply).tool_calls[0].arguments
            parsed = TypeAdapter(dict[str, JsonValue]).validate_json(raw_call_args)
            choices_raw = parsed["choices"]
            assert isinstance(choices_raw, list) and isinstance(choices_raw[0], dict)
            assert choices_raw[0]["reason"] == raw_reason

    asyncio.run(scenario())
