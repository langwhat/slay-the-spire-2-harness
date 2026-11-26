import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue
from test_combat import reply
from test_lifecycle import terminal
from test_mod_client import CARD, RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.context.models import RunAutomaticContent
from sts2_harness.journal import Journal, Manifest, SessionMaterials
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm
from sts2_harness.note_values import NotesState, NotesView


def shop(*, opened: bool, potion: bool) -> native.Observation:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    state = observation.state.model_copy(
        update={
            "screen": native.RunScreen.SHOP,
            "progress": observation.state.progress.model_copy(update={"room_type": "shop"}),
            "potions": ("bottle",) if potion else (),
            "actions": tuple(
                native.Action(ref=ref, label=label, enabled=enabled, operation=operation)
                for ref, label, enabled, operation in (
                    ("open", "打开商店", not opened, native.ActionOperation.OPEN_SHOP),
                    ("close", "关闭商店", opened, native.ActionOperation.CLOSE_SHOP),
                    ("leave", "离开商店", not opened, native.ActionOperation.LEAVE_SHOP),
                )
            ),
        }
    )
    return observation.model_copy(
        update={
            "state": state,
            "objects": {
                "bottle": native.PotionEntry(
                    kind=native.ObjectKind.POTION,
                    definition_id="POTION.FOUL_POTION",
                    name="臭药水",
                    usable=True,
                    needs_target=False,
                    target_type="targetednocreature",
                    usage=None,
                )
            }
            if potion
            else {},
        }
    )


class AutoHTTP:
    def __init__(self, states: list[native.Observation], responses: list[bytes]) -> None:
        self.states = iter(states)
        self.current = next(self.states)
        self.responses = iter(responses)
        self.actions: list[native.StepRequest] = []
        self.inputs: list[dict[str, JsonValue]] = []

    def mod(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": self.current.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "running",
                    "input_readiness": self.current.input_readiness.value,
                    "receipt": None,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=self.current
                ).to_json(),
            )
        assert request.url.path == "/action"
        submitted = native.StepRequest.model_validate_json(request.content)
        self.actions.append(submitted)
        self.current = next(self.states)
        return httpx.Response(
            200,
            json={
                "operation_id": submitted.operation_id,
                "status": "completed",
                "boundary": "ready",
            },
        )

    def model(self, request: httpx.Request) -> httpx.Response:
        self.inputs.append(json.loads(request.content))
        return httpx.Response(
            200, content=next(self.responses), headers={"Content-Type": "text/event-stream"}
        )


@pytest.mark.parametrize(
    "potion,batch,event", [(True, True, False), (False, False, False), (True, True, True)]
)
def test_shop_entry_auto_preserves_first_notes_and_leaves_departure_to_strategy(
    tmp_path: Path, potion: bool, batch: bool, event: bool
) -> None:
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics

    console = StringIO()
    diagnostics = Diagnostics("auto", console=console)
    from sts2_harness.context.models import RunActContent
    from sts2_harness.journal_models import AutomaticSnapshot, RunTaskSnapshot

    opened, closed = shop(opened=True, potion=False), shop(opened=False, potion=False)
    states = ([shop(opened=False, potion=True)] if potion else []) + [
        closed,
        opened,
        closed,
        terminal(closed),
    ]
    if event:
        event_states: list[native.Observation] = []
        for observation in states:
            state = observation.state
            if isinstance(state, native.RunState):
                event_states.append(
                    observation.model_copy(
                        update={
                            "state": state.model_copy(
                                update={
                                    "screen": native.RunScreen.SHOP
                                    if state.actions[1].enabled
                                    else native.RunScreen.EVENT,
                                    "event": native.EventState(
                                        ref="merchant-event",
                                        name="Synthetic event",
                                        ancient=False,
                                        definition_id="EVENT.SHOP",
                                    ),
                                    "progress": state.progress.model_copy(
                                        update={"room_type": "event"}
                                    ),
                                }
                            )
                        }
                    )
                )
            else:
                event_states.append(observation)
        states = event_states
    responses = (
        [
            reply(
                (
                    "depart",
                    "run_act",
                    '{"steps":[{"kind":"interact","action":"close"},{"kind":"interact","action":"leave"}]}',
                )
            )
        ]
        if batch
        else [
            reply(("close", "run_act", '{"steps":[{"kind":"interact","action":"close"}]}')),
            reply(("leave", "run_act", '{"steps":[{"kind":"interact","action":"leave"}]}')),
        ]
    )
    http = AutoHTTP(states, responses)

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(http.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="auto",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes=NotesState(other="Current plan."),
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
            diagnostics=diagnostics,
        ) as session:
            await session.run()
        archive = Journal.open(tmp_path / "archive.sqlite")
        first = archive.requests()[0]
        automatic = [message for message in first.input if isinstance(message, AutomaticSnapshot)]
        assert [message.content.automatic_progress.label for message in automatic] == (
            ["臭药水 (POTION.FOUL_POTION)", "打开商店"] if potion else ["打开商店"]
        )
        task = first.input[-1]
        assert isinstance(task, RunTaskSnapshot)
        assert isinstance(task.content, RunActContent)
        assert isinstance(task.content.observation.notes, NotesView)
        assert task.content.observation.notes.other == "Current plan."
        assert "updates" not in task.content.observation.model_dump(mode="json")
        assert all(
            isinstance(message.content, RunAutomaticContent) and message.content.observation is None
            for message in automatic
        )
        assert task.content.observation.baseline is True
        assert len(http.inputs) == (1 if batch else 2)
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == ["open", "close", "leave"]
        assert len(http.actions) == (4 if potion else 3)
        if potion:
            step = http.actions[0].step
            assert (
                isinstance(step, native.UsePotion)
                and step.potion == "bottle"
                and step.target is MISSING
            )
        events = [json.loads(line) for line in console.getvalue().splitlines()]
        automatic_events = [line for line in events if line["event"] == "auto_finished"]
        assert [line["label"] for line in automatic_events] == (
            ["臭药水 (POTION.FOUL_POTION)", "打开商店"] if potion else ["打开商店"]
        )
        assert all(line["outcome"] == "completed" for line in automatic_events)
        assert len([line for line in events if line["event"] == "tool_finished"]) == (
            1 if batch else 2
        )
        diagnostics.close()
        archive.close()

    asyncio.run(scenario())


def scene(
    screen: native.RunScreen, node: str, actions: tuple[native.Action, ...]
) -> native.Observation:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    return observation.model_copy(
        update={
            "location": observation.location.model_copy(update={"node_id": node}),
            "state": observation.state.model_copy(update={"screen": screen, "actions": actions}),
        }
    )


def reward_scene(
    screen: native.RunScreen,
    room_type: str,
    items: tuple[str, ...],
    *,
    empty_slots: int = 1,
    other_choice: bool = False,
) -> native.Observation:
    proceed = native.Action(
        ref="continue", label="前进", enabled=True, operation=native.ActionOperation.PROCEED
    )
    actions = tuple(
        native.Action(ref=f"claim-{item}", label=item, enabled=True, subject=item) for item in items
    )
    if other_choice:
        actions += (native.Action(ref="alternative", label="另一选项", enabled=True),)
    observation = scene(screen, room_type, (*actions, proceed))
    state = observation.state
    assert isinstance(state, native.RunState)
    objects: dict[str, native.ObjectEntry] = {}
    offers: list[native.Offer] = []
    for item in items:
        if screen is native.RunScreen.TREASURE:
            objects[item] = native.RelicEntry(
                kind=native.ObjectKind.RELIC, name=item, definition_id=f"RELIC.{item}"
            )
            continue
        potion = f"potion-{item}"
        objects[item] = native.OfferEntry(kind=native.ObjectKind.OFFER, name=item)
        objects[potion] = native.PotionEntry(
            kind=native.ObjectKind.POTION,
            name=item,
            definition_id=f"POTION.{item}",
            usable=None,
            needs_target=None,
            target_type=None,
            usage=None,
        )
        offers.append(
            native.Offer(
                ref=item,
                kind=native.OfferKind.POTION,
                contents=(potion,),
                actions=(f"claim-{item}",),
            )
        )
    return observation.model_copy(
        update={
            "objects": objects,
            "state": state.model_copy(
                update={
                    "progress": state.progress.model_copy(update={"room_type": room_type}),
                    "player": state.player.model_copy(
                        update={"potion_slot_count": 2, "empty_potion_slot_count": empty_slots}
                    ),
                    "offers": tuple(offers),
                }
            ),
        }
    )


@pytest.mark.parametrize(
    "room,screen,items,slots,other_choice,auto_actions",
    [
        ("treasure", native.RunScreen.TREASURE, ("relic",), 1, False, 2),
        ("treasure", native.RunScreen.TREASURE, ("relic", "other"), 1, False, 0),
        ("event", native.RunScreen.REWARDS, ("potion", "other"), 2, False, 3),
        ("event", native.RunScreen.REWARDS, ("potion",), 0, False, 0),
        ("event", native.RunScreen.REWARDS, ("potion", "other"), 1, False, 0),
        ("event", native.RunScreen.REWARDS, ("potion",), 1, True, 0),
        ("restsite", native.RunScreen.REWARDS, ("potion",), 1, False, 0),
    ],
)
def test_native_reward_claims_continue_only_without_a_choice(
    tmp_path: Path,
    room: str,
    screen: native.RunScreen,
    items: tuple[str, ...],
    slots: int,
    other_choice: bool,
    auto_actions: int,
) -> None:
    initial = reward_scene(screen, room, items, empty_slots=slots, other_choice=other_choice)
    if auto_actions:
        states = [initial]
        for offset in range(1, len(items) + 1):
            empty_slots = slots if screen is native.RunScreen.TREASURE else slots - offset
            next_screen = (
                native.RunScreen.EVENT
                if screen is native.RunScreen.REWARDS and offset == len(items)
                else screen
            )
            states.append(reward_scene(next_screen, room, items[offset:], empty_slots=empty_slots))
        responses = []
        expected = [*(f"claim-{item}" for item in items), "continue"]
    else:
        states = [initial]
        responses = [
            reply(("choose", "run_act", '{"steps":[{"kind":"interact","action":"continue"}]}'))
        ]
        expected = ["continue"]
    http = AutoHTTP([*states, terminal(states[-1])], responses)

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(http.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="native-reward-auto",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
        ) as session:
            await session.run()
        assert len(http.inputs) == (0 if auto_actions else 1)
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("card_visible_at_routing", [True, False])
def test_event_potion_is_claimed_after_deck_declines_the_other_reward(
    tmp_path: Path, card_visible_at_routing: bool
) -> None:
    def with_card(observation: native.Observation) -> native.Observation:
        state = observation.state
        assert isinstance(state, native.RunState)
        offer = native.Offer(
            ref="card-reward",
            kind=native.OfferKind.CARD,
            contents=("card",),
            actions=("open-card",),
            requires_selection=False,
        )
        action = native.Action(ref="open-card", label="卡牌", enabled=True, subject=offer.ref)
        return observation.model_copy(
            update={
                "objects": {
                    **observation.objects,
                    "card": native.CardEntry.model_validate_json(CARD),
                },
                "state": state.model_copy(
                    update={"offers": (*state.offers, offer), "actions": (*state.actions, action)}
                ),
            }
        )

    potion = reward_scene(native.RunScreen.REWARDS, "event", ("potion",))
    before = with_card(potion)
    after = with_card(reward_scene(native.RunScreen.REWARDS, "event", (), empty_slots=0))
    decline = '{"choices":[{"offer":"card-reward","choice":"skip","reason":"Skip this card."}]}'
    http = AutoHTTP(
        [before, after, terminal(after)], [reply(("decline", "run_deck_choice", decline))]
    )
    first_observation = True

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal first_observation
        if request.url.path == "/state" and first_observation and not card_visible_at_routing:
            first_observation = False
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=potion
                ).to_json(),
            )
        return http.mod(request)

    def model(request: httpx.Request) -> httpx.Response:
        assert [tool["name"] for tool in json.loads(request.content)["tools"]] == [
            "run_deck_choice"
        ]
        return http.model(request)

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="mixed-event-reward",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
        ) as session:
            await session.run()
        assert len(http.inputs) == 1
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == ["claim-potion", "continue"]

    asyncio.run(scenario())


def test_mechanical_rules_use_verified_languages_and_keep_event_choices_for_strategy(
    tmp_path: Path,
) -> None:
    from sts2_harness.journal_models import AutomaticSnapshot

    language = "zhs"
    travel = scene(
        native.RunScreen.MAP,
        "map",
        (
            native.Action(ref="travel", label="Travel label", enabled=True, destination="map:1:0"),
            native.Action(ref="blocked", label="Blocked", enabled=False, destination="map:1:1"),
        ),
    )
    travel = travel.model_copy(
        update={"map": native.PublicMap(bosses=(), nodes=(), legal_destinations=("map:1:0",))}
    )
    chest = scene(
        native.RunScreen.TREASURE,
        "chest",
        (
            native.Action(
                ref="chest",
                label="Chest label",
                enabled=True,
                operation=native.ActionOperation.OPEN_CHEST,
            ),
        ),
    )
    finished = scene(
        native.RunScreen.REST,
        "rest",
        (
            native.Action(ref="used-choice", label="Disabled rest choice", enabled=False),
            native.Action(
                ref="continue",
                label="Continue label",
                enabled=True,
                operation=native.ActionOperation.PROCEED,
            ),
        ),
    )
    choice = scene(
        native.RunScreen.EVENT,
        "event",
        (
            native.Action(ref="choice", label="Unique semantic event choice", enabled=True),
            native.Action(
                ref="skip",
                label="Proceed alongside choice",
                enabled=True,
                operation=native.ActionOperation.PROCEED,
            ),
        ),
    )
    states = [
        state.model_copy(update={"game_language": language})
        for state in (travel, chest, finished, choice, terminal(choice))
    ]
    http = AutoHTTP(
        states, [reply(("choose", "run_act", '{"steps":[{"kind":"interact","action":"choice"}]}'))]
    )

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(http.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="rules",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
        ) as session:
            await session.run()
        archive = Journal.open(tmp_path / "archive.sqlite")
        assert len(archive.requests()) == 1
        automatic = [
            message
            for message in archive.requests()[0].input
            if isinstance(message, AutomaticSnapshot)
        ]
        assert [message.content.automatic_progress.label for message in automatic] == [
            "Travel label",
            "Chest label",
            "Continue label",
        ]
        assert automatic[0].content.automatic_progress.reason == "当前只有一个可达的地图去向。"
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == ["travel", "chest", "continue", "choice"]
        archive.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["failed", "unknown", "nonprogress", "locale"])
@pytest.mark.parametrize("minimal", [False, True])
def test_unsuccessful_auto_requires_strategy_even_after_interrupted_model_request(
    tmp_path: Path, outcome: str, minimal: bool
) -> None:
    from sts2_harness.journal_models import AutomaticSnapshot
    from sts2_harness.results import ActionCompleted, ActionStopped, ActionUnknown

    before = scene(
        native.RunScreen.TREASURE,
        "chest",
        (
            native.Action(
                ref="chest",
                label="打开宝箱",
                enabled=True,
                operation=native.ActionOperation.OPEN_CHEST,
            ),
        ),
    )
    after = before.model_copy(
        update={"stamp": before.stamp.model_copy(update={"observation_sequence": 20})}
    )
    if outcome == "locale":
        assert isinstance(after.state, native.RunState)
        after = after.model_copy(
            update={
                "game_language": "eng",
                "state": after.state.model_copy(
                    update={
                        "actions": (
                            after.state.actions[0].model_copy(update={"label": "Open chest"}),
                        )
                    }
                ),
            }
        )
    http = AutoHTTP(
        [before, after, terminal(after)],
        [reply(("choose", "run_act", '{"steps":[{"kind":"interact","action":"chest"}]}'))],
    )
    paused = False
    attempts = 0

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal paused
        if request.url.path == "/control":
            paused = True
            return httpx.Response(200, json={"mode": "paused"})
        if request.url.path == "/status" and paused:
            return httpx.Response(
                200,
                json={
                    "location": http.current.location.model_dump(mode="json"),
                    "presentation": None,
                    "control_intent": "paused",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        response = http.mod(request)
        if (
            request.url.path == "/action"
            and len(http.actions) == 1
            and outcome in ("failed", "unknown")
        ):
            return httpx.Response(
                200,
                json={
                    "operation_id": http.actions[0].operation_id,
                    "status": outcome,
                    "error": {
                        "code": "dispatch_failed" if outcome == "failed" else "execution_uncertain",
                        "message": "Synthetic uncertain or unsuccessful execution.",
                    },
                },
            )
        return response

    def model(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                400,
                json={
                    "error": {"type": "invalid_request_error", "message": "Synthetic interruption."}
                },
            )
        return http.model(request)

    async def wait(seconds: float) -> None:
        nonlocal paused
        assert len(http.actions) == 1
        paused = False

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="force",
                minimal=minimal,
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(model)),
            wait=wait,
        ) as session:
            await session.run()
        assert attempts == 2 and len(http.actions) == 2
        archive = Journal.open(tmp_path / "archive.sqlite")
        first, resumed = archive.requests()
        automatic = next(
            message for message in resumed.input if isinstance(message, AutomaticSnapshot)
        )
        assert automatic.content.automatic_progress.label == "打开宝箱"
        assert automatic.content.automatic_progress.reason == "打开宝箱查看其中的物品。"
        assert isinstance(
            automatic.content.outcome,
            ActionStopped
            if outcome == "failed"
            else ActionUnknown
            if outcome == "unknown"
            else ActionCompleted,
        )
        if outcome == "unknown":
            assert isinstance(automatic.content.outcome, ActionUnknown)
            assert automatic.content.outcome.reason.message == (
                "Synthetic uncertain or unsuccessful execution."
            )
        assert first.failure is not None
        archive.close()

    asyncio.run(scenario())


def glory_epilogue_page(
    action_ref: str,
    label: str,
    *,
    option_ref: str = "option:53:1",
    sequence: int = 2429,
    floor: int = 49,
    act: int = 3,
    acts: int = 3,
    act_definition_id: str | MISSING = "ACT.GLORY",
    bosses_left: int | MISSING = 0,
    room_type: str = "event",
    screen: native.RunScreen = native.RunScreen.EVENT,
    event_definition_id: str | None | MISSING = "EVENT.THE_ARCHITECT",
    extra_actions: tuple[native.Action, ...] = (),
    cost: native.ActionCost | MISSING = MISSING,
) -> native.Observation:
    observation = native.Observation.model_validate_json(RUN)
    assert isinstance(observation.state, native.RunState)
    event = (
        None
        if event_definition_id is None
        else native.EventState(
            ref="event:53:1",
            name="建筑师",
            ancient=False,
            definition_id=event_definition_id,
        )
    )
    primary = native.Action(
        ref=action_ref,
        label=label,
        enabled=True,
        subject=option_ref,
        cost=cost,
    )
    objects: dict[str, native.ObjectEntry] = {
        option_ref: native.OptionEntry(kind=native.ObjectKind.OPTION, name=label),
    }
    if event is not None:
        objects[event.ref] = native.EventEntry(
            kind=native.ObjectKind.EVENT,
            name=event.name,
            ancient=event.ancient,
            definition_id=event.definition_id,
        )
    return observation.model_copy(
        update={
            "game_language": "zhs",
            "stamp": observation.stamp.model_copy(update={"observation_sequence": sequence}),
            "location": observation.location.model_copy(update={"node_id": "node-53"}),
            "objects": objects,
            "state": observation.state.model_copy(
                update={
                    "screen": screen,
                    "event": event,
                    "progress": observation.state.progress.model_copy(
                        update={
                            "act": act,
                            "acts": acts,
                            "floor": floor,
                            "room_type": room_type,
                            "definition_id": act_definition_id,
                            "bosses_left": bosses_left,
                        }
                    ),
                    "actions": (primary, *extra_actions),
                }
            ),
        }
    )


def test_glory_post_boss_epilogue_auto_continues_to_victory_without_model_calls(
    tmp_path: Path,
) -> None:
    from io import StringIO

    from sts2_harness.diagnostics import Diagnostics
    from sts2_harness.game import RunEndReason, Settle

    pages = [
        glory_epilogue_page("interaction:53:1", "啾", option_ref="option:53:1", sequence=2429),
        glory_epilogue_page(
            "interaction:53:2", "继续", option_ref="option:53:2", sequence=2434, floor=50
        ),
        glory_epilogue_page(
            "interaction:53:3", "继续", option_ref="option:53:3", sequence=2439, floor=50
        ),
    ]
    victory = terminal(pages[-1]).model_copy(
        update={"stamp": pages[-1].stamp.model_copy(update={"observation_sequence": 2441})}
    )
    http = AutoHTTP([*pages, victory], [])

    def mod(request: httpx.Request) -> httpx.Response:
        response = http.mod(request)
        if request.url.path == "/action" and len(http.actions) == len(pages):
            return httpx.Response(
                200,
                json={
                    "operation_id": http.actions[-1].operation_id,
                    "status": "completed",
                    "boundary": "terminal",
                },
            )
        return response

    console = StringIO()
    diagnostics = Diagnostics("glory-epilogue", console=console)

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="glory-epilogue",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
            diagnostics=diagnostics,
        ) as session:
            ended = await session.run()
        assert ended.reason is RunEndReason.TERMINAL
        assert ended.observation is not None
        assert isinstance(ended.observation.state, native.TerminalState)
        assert ended.observation.state.screen is native.TerminalScreen.RUN_COMPLETE
        assert ended.observation.state.result is native.TerminalResult.VICTORY
        assert http.inputs == []
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == ["interaction:53:1", "interaction:53:2", "interaction:53:3"]

        archive = Journal.open(tmp_path / "archive.sqlite")
        assert archive.requests() == ()
        settles = [op for op in archive.operations() if isinstance(op.request, Settle)]
        assert len(settles) == 3
        recorded_terminal = [
            obs.state
            for op in archive.operations()
            if op.reply is not None
            for obs in op.reply.observations
            if isinstance(obs.state, native.TerminalState)
        ]
        assert recorded_terminal
        assert recorded_terminal[-1].result is native.TerminalResult.VICTORY
        archive.close()

        events = [json.loads(line) for line in console.getvalue().splitlines()]
        auto_events = [line for line in events if line["event"] == "auto_finished"]
        assert [line["label"] for line in auto_events] == ["啾", "继续", "继续"]
        assert all(line["outcome"] == "completed" for line in auto_events)
        diagnostics.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "multi_option",
        "other_event",
        "missing_event",
        "bosses_left_nonzero",
        "bosses_left_missing",
        "not_final_act",
        "other_act",
        "action_has_cost",
    ],
)
def test_glory_epilogue_auto_stops_and_defers_unsafe_or_ordinary_events(
    tmp_path: Path, case: str
) -> None:
    from sts2_harness.journal_models import AutomaticSnapshot

    page1 = glory_epilogue_page("interaction:53:1", "啾", option_ref="option:53:1", sequence=2429)
    if case == "multi_option":
        unsafe = glory_epilogue_page(
            "interaction:53:2",
            "继续",
            option_ref="option:53:2",
            sequence=2434,
            extra_actions=(native.Action(ref="interaction:53:alt", label="另一项", enabled=True),),
        )
        states = [page1, unsafe, terminal(unsafe)]
        expected_actions = ["interaction:53:1", "interaction:53:2"]
        expected_auto = ["啾"]
        chosen_ref = "interaction:53:2"
    else:
        unsafe = glory_epilogue_page(
            "interaction:53:1",
            "继续",
            option_ref="option:53:1",
            sequence=2429,
            event_definition_id=(
                "EVENT.TRIAL"
                if case == "other_event"
                else None
                if case == "missing_event"
                else "EVENT.THE_ARCHITECT"
            ),
            bosses_left=(
                1
                if case == "bosses_left_nonzero"
                else MISSING
                if case == "bosses_left_missing"
                else 0
            ),
            act=2 if case == "not_final_act" else 3,
            act_definition_id="ACT.HIVE" if case == "other_act" else "ACT.GLORY",
            cost=native.ActionCost(gold=10) if case == "action_has_cost" else MISSING,
        )
        states = [unsafe, terminal(unsafe)]
        expected_actions = ["interaction:53:1"]
        expected_auto = []
        chosen_ref = "interaction:53:1"

    http = AutoHTTP(
        states,
        [
            reply(
                (
                    "choose",
                    "run_act",
                    f'{{"steps":[{{"kind":"interact","action":"{chosen_ref}"}}]}}',
                )
            )
        ],
    )

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(http.mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id=f"unsafe-{case}",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
        ) as session:
            await session.run()
        assert len(http.inputs) == 1
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == expected_actions
        archive = Journal.open(tmp_path / "archive.sqlite")
        automatic = [
            message
            for message in archive.requests()[0].input
            if isinstance(message, AutomaticSnapshot)
        ]
        assert [message.content.automatic_progress.label for message in automatic] == expected_auto
        archive.close()

    asyncio.run(scenario())


def test_glory_epilogue_auto_stops_on_phase_change_to_combat(tmp_path: Path) -> None:
    from test_lifecycle import unavailable_history
    from test_mod_client import COMBAT

    from sts2_harness.game import RunEndReason

    page1 = glory_epilogue_page("interaction:53:1", "啾", option_ref="option:53:1", sequence=2429)
    combat_base = native.Observation.model_validate_json(COMBAT)
    assert isinstance(combat_base.state, native.CombatState)
    combat = combat_base.model_copy(
        update={
            "stamp": page1.stamp.model_copy(update={"observation_sequence": 2434}),
            "location": native.Location(
                game_instance_id="game",
                run_id="run",
                node_id="node-54",
                owner=native.DecisionOwner.COMBAT,
            ),
            "state": combat_base.state.model_copy(
                update={
                    "progress": combat_base.state.progress.model_copy(
                        update={"act": 3, "acts": 3, "definition_id": "ACT.GLORY"}
                    )
                }
            ),
        }
    )
    http = AutoHTTP(
        [page1, combat, terminal(combat)],
        [reply(("turn", "combat_act", '{"steps":[{"kind":"end_turn"}]}'))],
    )

    def mod(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/inspect":
            return unavailable_history(http.current)
        return http.mod(request)

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                materials=SessionMaterials(
                    entry_documents_enabled=False, act_guidance={}, battle_guidance=()
                ),
                recording_id="epilogue-phase-change",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
        ) as session:
            ended = await session.run()
        assert ended.reason is RunEndReason.TERMINAL
        assert len(http.inputs) == 1
        assert isinstance(http.actions[0].step, native.Interact)
        assert http.actions[0].step.action == "interaction:53:1"
        assert isinstance(http.actions[1].step, native.EndTurn)

    asyncio.run(scenario())


@pytest.mark.parametrize("relic_id", ["RELIC.BELT_BUCKLE", "RELIC.SOZU"])
def test_auto_potion_claim_skips_belt_buckle_and_sozu(relic_id: str) -> None:
    from sts2_harness.auto import battle_reward, choose_auto

    base = native.Observation.model_validate_json(RUN)
    assert isinstance(base.state, native.RunState)
    potion_entry = native.PotionEntry(
        kind=native.ObjectKind.POTION,
        definition_id="POTION.FIRE_POTION",
        name="火焰药水",
        usable=False,
        needs_target=True,
        target_type="anyenemy",
        usage=None,
    )
    relic_entry = native.RelicEntry(
        kind=native.ObjectKind.RELIC,
        definition_id=relic_id,
        name="Relic",
        status=native.RelicStatus.NORMAL,
        is_used_up=False,
        is_melted=False,
        stack_count=1,
    )
    offer = native.Offer(
        ref="offer:pot",
        kind=native.OfferKind.POTION,
        contents=("pot:1",),
        actions=("claim-pot",),
        requires_selection=False,
    )
    actions = (
        native.Action(ref="claim-pot", label="火焰药水", enabled=True, subject="offer:pot"),
        native.Action(
            ref="proceed",
            label="继续",
            enabled=True,
            operation=native.ActionOperation.PROCEED,
        ),
    )
    monster_plain = base.model_copy(
        update={
            "objects": {"pot:1": potion_entry},
            "state": base.state.model_copy(
                update={
                    "screen": native.RunScreen.REWARDS,
                    "progress": base.state.progress.model_copy(update={"room_type": "monster"}),
                    "player": base.state.player.model_copy(update={"empty_potion_slot_count": 2}),
                    "relics": (),
                    "offers": (offer,),
                    "actions": actions,
                }
            ),
        }
    )
    assert battle_reward(monster_plain) == actions[0]

    monster_blocked = monster_plain.model_copy(
        update={
            "objects": {"pot:1": potion_entry, "rel:1": relic_entry},
            "state": monster_plain.state.model_copy(update={"relics": ("rel:1",)}),
        }
    )
    assert battle_reward(monster_blocked) is None

    event_plain = monster_plain.model_copy(
        update={
            "state": monster_plain.state.model_copy(
                update={"progress": base.state.progress.model_copy(update={"room_type": "event"})}
            )
        }
    )
    assert choose_auto(event_plain, shop_strategy=False) is not None

    event_blocked = event_plain.model_copy(
        update={
            "objects": {"pot:1": potion_entry, "rel:1": relic_entry},
            "state": event_plain.state.model_copy(update={"relics": ("rel:1",)}),
        }
    )
    assert choose_auto(event_blocked, shop_strategy=False) is None


def test_battle_reward_skips_gold_when_ectoplasm_owned() -> None:
    from sts2_harness.auto import battle_reward

    base = native.Observation.model_validate_json(RUN)
    assert isinstance(base.state, native.RunState)
    ectoplasm = native.RelicEntry(
        kind=native.ObjectKind.RELIC,
        definition_id="RELIC.ECTOPLASM",
        name="灵液",
        status=native.RelicStatus.NORMAL,
        is_used_up=False,
        is_melted=False,
        stack_count=1,
    )
    gold_offer = native.Offer(
        ref="offer:gold",
        kind=native.OfferKind.GOLD,
        contents=(),
        actions=("claim-gold",),
        amount=25,
    )
    claim_gold = native.Action(
        ref="claim-gold", label="25 金币", enabled=True, subject="offer:gold"
    )
    obs = base.model_copy(
        update={
            "objects": {"rel:ecto": ectoplasm},
            "state": base.state.model_copy(
                update={
                    "screen": native.RunScreen.REWARDS,
                    "progress": base.state.progress.model_copy(update={"room_type": "monster"}),
                    "relics": ("rel:ecto",),
                    "offers": (gold_offer,),
                    "actions": (claim_gold,),
                }
            ),
        }
    )
    assert battle_reward(obs) is None
