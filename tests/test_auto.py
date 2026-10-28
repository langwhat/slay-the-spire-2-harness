import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue
from test_combat import reply
from test_lifecycle import terminal
from test_mod_client import RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.journal import Journal, Manifest
from sts2_harness.live_session import live_session
from sts2_harness.llm.client import Llm


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
                recording_id="auto",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="Current plan.",
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
        assert task.content.strategy_notes == "Current plan."
        assert task.content.observation.updates.model_dump(exclude_unset=True) == {}
        assert all(message.content.state is not None for message in automatic)
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
def test_unsuccessful_auto_requires_strategy_even_after_interrupted_model_request(
    tmp_path: Path, outcome: str
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
                recording_id="force",
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


@pytest.mark.parametrize("language", ["", "future-language"])
def test_unknown_language_uses_strategy_and_shop_stays_with_strategy_after_resume(
    tmp_path: Path, language: str
) -> None:
    from sts2_harness.journal_models import AutomaticSnapshot

    closed = shop(opened=False, potion=True).model_copy(update={"game_language": language})
    http = AutoHTTP(
        [closed, terminal(closed)],
        [reply(("leave", "run_act", '{"steps":[{"kind":"interact","action":"leave"}]}'))],
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
                    "control_intent": "paused",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        return http.mod(request)

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
        assert http.actions == []
        http.current = http.current.model_copy(update={"game_language": "zhs"})
        paused = False

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(mod)),
            Manifest(
                recording_id="language",
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
        assert attempts == 2 and len(http.actions) == 1
        assert isinstance(http.actions[0].step, native.Interact)
        assert http.actions[0].step.action == "leave"
        archive = Journal.open(tmp_path / "archive.sqlite")
        assert not any(
            isinstance(message, AutomaticSnapshot)
            for request in archive.requests()
            for message in request.input
        )
        archive.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cross_act", [False, True])
def test_auto_preserves_handoff_before_next_combat_and_act_rebuild(
    tmp_path: Path, cross_act: bool
) -> None:
    from test_lifecycle import CodeCombat, battle_state

    from sts2_harness.context.models import HandoffContent, RunActHandoffContent
    from sts2_harness.execution import Execution
    from sts2_harness.execution.recovery import ToolRecoveryBudget
    from sts2_harness.journal_models import AutomaticSnapshot, RunTaskSnapshot

    reward = scene(
        native.RunScreen.REWARDS,
        "reward1",
        (
            native.Action(
                ref="continue", label="继续", enabled=True, operation=native.ActionOperation.PROCEED
            ),
        ),
    )
    second = battle_state(2 if cross_act else 1, "battle2")
    choice = scene(
        native.RunScreen.EVENT,
        "choice",
        (native.Action(ref="choose", label="Choice", enabled=True),),
    )
    if cross_act:
        assert isinstance(choice.state, native.RunState)
        choice = choice.model_copy(
            update={"state": choice.state.model_copy(update={"progress": second.state.progress})}
        )
    http = AutoHTTP(
        [battle_state(1, "battle1"), reward, second, choice, terminal(choice)],
        [
            reply(("notes", "run_notes", '{"mode":"replace","text":"Save for the next battle."}')),
            reply(text="Report processed."),
            reply(("choose", "run_act", '{"steps":[{"kind":"interact","action":"choose"}]}')),
        ],
    )
    openings: list[str] = []

    def combat(
        execution: Execution,
        recovery: ToolRecoveryBudget,
        observation: native.Observation,
        notes: str,
    ) -> CodeCombat:
        openings.append(notes)
        return CodeCombat()

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(http.mod)),
            Manifest(
                recording_id="handoff",
                core_version="test",
                game_instance_id="game",
                run_id="run",
                initial_notes="Initial strategy.",
            ),
            tmp_path / "archive.sqlite",
            model=Llm(configuration(tmp_path), transport=httpx.MockTransport(http.model)),
            combat_factory=combat,
        ) as session:
            await session.run()
        assert openings == ["Initial strategy.", "Save for the next battle."]
        archive = Journal.open(tmp_path / "archive.sqlite")
        requests = archive.requests()
        first_task = requests[0].input[-1]
        assert isinstance(first_task, RunTaskSnapshot) and isinstance(
            first_task.content, HandoffContent
        )
        assert first_task.content.strategy_notes == "Initial strategy."
        automatic = [
            message for message in requests[0].input if isinstance(message, AutomaticSnapshot)
        ]
        assert len(automatic) == (0 if cross_act else 1)
        if automatic:
            assert automatic[0].content.state is not None
            assert not isinstance(automatic[0].content.state, native.RunState)
        assert requests[0].stream == ("run:2" if cross_act else "run:1")
        last_task = requests[-1].input[-1]
        assert isinstance(last_task, RunTaskSnapshot) and isinstance(
            last_task.content, RunActHandoffContent
        )
        assert last_task.content.strategy_notes is MISSING
        assert len(http.actions) == 4 and len(requests) == 3
        archive.close()

    asyncio.run(scenario())


def test_rewards_stay_with_strategy_and_pure_dialogue_advances_from_changed_native_text(
    tmp_path: Path,
) -> None:
    from sts2_harness.journal_models import AutomaticSnapshot

    reward = scene(
        native.RunScreen.REWARDS,
        "rewards",
        (
            native.Action(ref="claim", label="Claim", enabled=True),
            native.Action(
                ref="skip", label="Continue", enabled=True, operation=native.ActionOperation.PROCEED
            ),
        ),
    )
    assert isinstance(reward.state, native.RunState)
    reward = reward.model_copy(
        update={
            "state": reward.state.model_copy(
                update={
                    "offers": (
                        native.Offer(
                            ref="reward",
                            kind=native.OfferKind.GOLD,
                            contents=(),
                            actions=("claim",),
                        ),
                    )
                }
            )
        }
    )
    assert isinstance(reward.state, native.RunState)
    empty = reward.model_copy(
        update={
            "state": reward.state.model_copy(
                update={"offers": (), "actions": (reward.state.actions[1],)}
            )
        }
    )
    dialogue = scene(
        native.RunScreen.EVENT,
        "dialogue",
        (
            native.Action(
                ref="dialogue",
                label="Continue",
                enabled=True,
                operation=native.ActionOperation.PROCEED,
            ),
        ),
    )
    dialogue = dialogue.model_copy(
        update={
            "objects": {
                "event": native.EventEntry(
                    kind=native.ObjectKind.EVENT,
                    name="Event",
                    ancient=False,
                    definition_id="EVENT.TEST",
                )
            },
            "descriptions": {"event": "First line."},
        }
    )
    next_line = dialogue.model_copy(update={"descriptions": {"event": "Second line."}})
    choice = scene(
        native.RunScreen.EVENT,
        "dialogue",
        (native.Action(ref="choose", label="Choose", enabled=True),),
    )
    http = AutoHTTP(
        [reward, empty, dialogue, next_line, choice, terminal(choice)],
        [
            reply(("claim", "run_act", '{"steps":[{"kind":"interact","action":"claim"}]}')),
            reply(("choose", "run_act", '{"steps":[{"kind":"interact","action":"choose"}]}')),
        ],
    )

    async def scenario() -> None:
        async with live_session(
            ModClient("http://mod", transport=httpx.MockTransport(http.mod)),
            Manifest(
                recording_id="dialogue",
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
        assert len(archive.requests()) == 2
        automatic = [
            message
            for message in archive.requests()[-1].input
            if isinstance(message, AutomaticSnapshot)
        ]
        assert len(automatic) == 3
        assert automatic[0].content.updates.descriptions == {"event": "First line."}
        assert automatic[1].content.updates.descriptions == {"event": "Second line."}
        assert [
            step.step.action for step in http.actions if isinstance(step.step, native.Interact)
        ] == ["claim", "skip", "dialogue", "dialogue", "choose"]
        archive.close()

    asyncio.run(scenario())
