import asyncio
import json

import pytest
from pydantic import MISSING, ValidationError
from test_mod_client import COMBAT
from test_orb_observation import read_observation

from sts2_harness.client import ClientFailureKind, ClientUnavailable
from sts2_harness.client.models import ArrowSlotStatus, CombatState, DecisionOwner
from sts2_harness.context import CombatActInput, Context
from sts2_harness.context.run_models import RunObservation


def test_combat_delivers_ordered_arrow_slots_and_replaces_their_previous_targets() -> None:
    async def read() -> None:
        context = Context(DecisionOwner.COMBAT)
        slots = [
            {"status": "retrieved"},
            {"status": "away", "impaled_target_ref": "e1"},
            {"status": "away"},
        ]
        payload = json.loads(COMBAT)
        payload["state"]["arrow_slots"] = slots
        observed = (await read_observation(json.dumps(payload))).observation
        assert isinstance(observed.state, CombatState)
        assert observed.state.arrow_slots is not MISSING
        assert observed.state.arrow_slots[1].status is ArrowSlotStatus.AWAY
        assert observed.state.arrow_slots[1].impaled_target_ref == "e1"
        context.append_task(CombatActInput(), observed)
        assert json.loads(context.model_input()[-1].text)["observation"]["arrow_slots"] == slots

        slots[1] = {"status": "retrieved"}
        payload["stamp"]["observation_sequence"] += 1
        observed = (await read_observation(json.dumps(payload))).observation
        context.append_task(CombatActInput(), observed)
        assert json.loads(context.model_input()[-1].text)["observation"]["arrow_slots"] == slots

        del payload["state"]["arrow_slots"]
        payload["stamp"]["observation_sequence"] += 1
        observed = (await read_observation(json.dumps(payload))).observation
        assert isinstance(observed.state, CombatState)
        assert observed.state.arrow_slots is MISSING
        context.append_task(CombatActInput(), observed)
        assert json.loads(context.model_input()[-1].text)["observation"]["arrow_slots"] is None

    asyncio.run(read())


def test_mod_rejects_unknown_arrow_status() -> None:
    async def read(status: str) -> None:
        payload = json.loads(COMBAT)
        payload["state"]["arrow_slots"] = [{"status": status}]
        with pytest.raises(ClientUnavailable) as failure:
            await read_observation(json.dumps(payload))
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    for bad_status in ("normal", "destroyed"):
        asyncio.run(read(bad_status))
        with pytest.raises(ValidationError):
            RunObservation.model_validate_json(f'{{"arrow_slots":[{{"status":"{bad_status}"}}]}}')
