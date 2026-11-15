import asyncio
import json

import httpx2 as httpx
import pytest
from pydantic import MISSING
from test_lifecycle import terminal
from test_mod_client import COMBAT, RUN

from sts2_harness.client import ClientFailureKind, ClientUnavailable, ModClient
from sts2_harness.client.models import (
    CombatState,
    DecisionOwner,
    RunState,
    StateCompleted,
    TerminalState,
)
from sts2_harness.context import CombatActInput, Context
from sts2_harness.context.models import RunActInput
from sts2_harness.deck import deck_context, deck_observation
from sts2_harness.prompts import PackagedPrompts


async def read_observation(payload: str) -> StateCompleted:
    async with ModClient(
        "http://mod.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content='{"status":"completed","observation":' + payload + "}"
            )
        ),
    ) as client:
        result = await client.state()
    assert isinstance(result, StateCompleted)
    return result


@pytest.mark.parametrize(("character", "slots"), [("IRONCLAD", 0), ("DEFECT", 3)])
def test_run_and_deck_receive_native_base_orb_slots(character: str, slots: int) -> None:
    payload = RUN.replace("CHARACTER.TEST", f"CHARACTER.{character}").replace(
        '"base_orb_slots":0', f'"base_orb_slots":{slots}'
    )

    async def read() -> None:
        result = await read_observation(payload)
        observation = result.observation
        assert isinstance(observation.state, RunState)
        assert observation.state.player.base_orb_slots == slots

        run = Context(DecisionOwner.RUN)
        run.append_task(RunActInput(), observation)
        assert (
            json.loads(run.model_input()[-1].text)["observation"]["player"]["base_orb_slots"]
            == slots
        )

        focused = deck_observation(observation, None)
        deck = deck_context(PackagedPrompts(), (), focused, minimal=False)
        deck.append_task(RunActInput(), focused)
        assert (
            json.loads(deck.model_input()[-1].text)["observation"]["player"]["base_orb_slots"]
            == slots
        )

    asyncio.run(read())


@pytest.mark.parametrize("value", [None, "null", "true", '"0"', "-1"])
def test_mod_rejects_missing_or_invalid_base_orb_slots(value: str | None) -> None:
    replacement = "" if value is None else f',"base_orb_slots":{value}'
    payload = RUN.replace(',"base_orb_slots":0', replacement)

    async def read() -> None:
        with pytest.raises(ClientUnavailable) as failure:
            await read_observation(payload)
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())


def test_zero_orb_capacity_is_visible_without_fabricating_base_slots() -> None:
    payload = COMBAT.replace(
        '"capacity":1,"slots":[{"name":"闪电",\n'
        '      "definition_id":"ORB.LIGHTNING","passive":0,"evoke":null}]',
        '"capacity":0,"slots":[]',
    )
    assert payload != COMBAT

    async def read() -> None:
        result = await read_observation(payload)
        assert isinstance(result.observation.state, CombatState)
        orbs = result.observation.state.orbs
        assert orbs is not MISSING
        assert orbs.capacity == 0
        assert orbs.slots == ()

        combat = Context(DecisionOwner.COMBAT)
        combat.append_task(CombatActInput(), result.observation)
        view = json.loads(combat.model_input()[-1].text)["observation"]
        assert view["orbs"] == {"capacity": 0}
        assert "base_orb_slots" not in view["player"]

        run = Context(DecisionOwner.RUN)
        run.append_task(RunActInput(), result.observation)
        view = json.loads(run.model_input()[-1].text)["observation"]
        assert "base_orb_slots" not in view["player"]

        ended = await read_observation(terminal(result.observation).to_json())
        assert isinstance(ended.observation.state, TerminalState)
        assert ended.observation.state.player.base_orb_slots is MISSING
        ended_run = Context(DecisionOwner.RUN)
        ended_run.append_task(RunActInput(), ended.observation)
        view = json.loads(ended_run.model_input()[-1].text)["observation"]
        assert "base_orb_slots" not in view["player"]

    asyncio.run(read())
