import asyncio
import json

import httpx2 as httpx
import pytest
from test_mod_client import COMBAT, RUN

from sts2_harness.client import models as native
from sts2_harness_eval.laboratory import (
    LaboratoryClient,
    LaboratoryErrorCode,
    LaboratoryOutcome,
    LaboratoryRecipe,
)

RECIPE = LaboratoryRecipe(
    deck=("CARD.BURNING_PACT", "CARD.SECOND_WIND", "CARD.SECOND_WIND"),
    relics=("RELIC.STRAWBERRY",),
    potions=(),
    encounter="ENCOUNTER.SOUL_FYSH_BOSS",
)


def test_construct_sends_the_recipe_once_and_decodes_both_native_observations() -> None:
    requests: list[httpx.Request] = []

    def mod(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/operator/eval/scenario"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "deck": ["CARD.BURNING_PACT", "CARD.SECOND_WIND", "CARD.SECOND_WIND"],
            "relics": ["RELIC.STRAWBERRY"],
            "potions": [],
            "encounter": "ENCOUNTER.SOUL_FYSH_BOSS",
        }
        return httpx.Response(
            200,
            content='{"status":"completed","error":null,"prepared_observation":'
            + RUN
            + ',"observation":'
            + COMBAT
            + "}",
        )

    async def scenario() -> None:
        async with LaboratoryClient("http://mod", transport=httpx.MockTransport(mod)) as client:
            reply = await client.construct(RECIPE)
        assert reply.status is LaboratoryOutcome.COMPLETED
        assert reply.error is None
        assert reply.prepared_observation == native.Observation.model_validate_json(RUN)
        assert reply.observation is not None
        assert reply.observation == native.Observation.model_validate_json(COMBAT)
        assert isinstance(reply.observation.state, native.CombatState)
        assert reply.observation.state.phase is native.Phase.COMBAT

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    (
        (LaboratoryOutcome.REJECTED, LaboratoryErrorCode.DEVELOPMENT_DISABLED),
        (LaboratoryOutcome.UNKNOWN, LaboratoryErrorCode.NATIVE_INCOMPLETE),
    ),
)
def test_handled_rejection_or_unknown_is_returned_without_repeating_construction(
    status: LaboratoryOutcome,
    code: LaboratoryErrorCode,
) -> None:
    requests = 0

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "status": status.value,
                "error": {"code": code.value, "message": "Not confirmed."},
                "prepared_observation": None,
                "observation": None,
            },
        )

    async def scenario() -> None:
        async with LaboratoryClient("http://mod", transport=httpx.MockTransport(mod)) as client:
            reply = await client.construct(RECIPE)
        assert reply.status is status
        assert reply.error is not None and reply.error.code is code
        assert reply.prepared_observation is None and reply.observation is None

    asyncio.run(scenario())
    assert requests == 1


@pytest.mark.parametrize("cancel", (False, True))
def test_interrupted_construction_propagates_without_repeating(cancel: bool) -> None:
    requests = 0

    def mod(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if cancel:
            raise asyncio.CancelledError
        raise httpx.ReadTimeout("Reply interrupted.", request=request)

    async def scenario() -> None:
        async with LaboratoryClient("http://mod", transport=httpx.MockTransport(mod)) as client:
            with pytest.raises(asyncio.CancelledError if cancel else httpx.ReadTimeout):
                await client.construct(RECIPE)

    asyncio.run(scenario())
    assert requests == 1
