import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from test_combat import reply
from test_mod_client import CARD, RUN
from test_responses import configuration

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.decisions import ActTask
from sts2_harness.execution import Executed, Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.llm.client import Llm
from sts2_harness.notes import Notes
from sts2_harness.results import ActionCompleted
from sts2_harness.run import RunAgent
from sts2_harness.tools import ToolCatalog


@pytest.mark.parametrize(
    ("choice", "requires_selection", "mixed_rewards"),
    [
        ("take", True, False),
        ("skip", True, False),
        ("take", False, False),
        ("skip", False, False),
        pytest.param("take", True, True, id="decline-special-before-normal"),
    ],
)
def test_reward_choice_opens_one_group_then_uses_native_selection(
    choice: str, requires_selection: bool, mixed_rewards: bool, tmp_path: Path
) -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(RUN)
        assert isinstance(observation.state, native.RunState)
        card = native.CardEntry.model_validate_json(CARD)
        reward_numbers = (1, 2) if requires_selection and not mixed_rewards else (1,)
        state = observation.state.model_copy(
            update={
                "screen": native.RunScreen.REWARDS,
                "deck": ("owned",),
                "offers": tuple(
                    native.Offer(
                        ref=f"o{i}",
                        kind=native.OfferKind.CARD,
                        requires_selection=requires_selection,
                        contents=(f"c{i}",),
                        actions=(f"open{i}",),
                    )
                    for i in reward_numbers
                ),
                "actions": tuple(
                    native.Action(ref=f"open{i}", label="Reward", enabled=True, subject=f"o{i}")
                    for i in reward_numbers
                ),
            }
        )
        if mixed_rewards:
            state = state.model_copy(
                update={
                    "offers": (
                        native.Offer(
                            ref="special",
                            kind=native.OfferKind.CARD,
                            requires_selection=False,
                            contents=("special-card",),
                            actions=("claim-special",),
                        ),
                        *state.offers,
                    ),
                    "actions": (
                        native.Action(
                            ref="claim-special",
                            label="Special reward",
                            enabled=True,
                            subject="special",
                        ),
                        *state.actions,
                    ),
                }
            )
        if not requires_selection or mixed_rewards:
            state = state.model_copy(
                update={
                    "actions": (
                        *state.actions,
                        native.Action(
                            ref="proceed",
                            label="Continue",
                            enabled=True,
                            operation=native.ActionOperation.PROCEED,
                        ),
                    ),
                }
            )
        observation = observation.model_copy(
            update={
                "state": state,
                "objects": {
                    "owned": card,
                    "c1": card,
                    "c2": card,
                    "special-card": card,
                },
                "descriptions": {"owned": "Draw.", "c1": "Draw.", "c2": "Draw."},
            }
        )
        steps: list[native.NativeStep] = []
        arguments: dict[str, object] = {
            "offer": "o1",
            "choice": choice,
            "reason": "A measured change.",
        }
        if choice == "take":
            arguments.update(card="c1", **{"class": "engine"}, links=["CARD.ONE"])
        invalid = {**arguments, "offer": "missing"}
        if choice == "take":
            invalid = {**arguments, "links": ["CARD.NOT_OWNED"]}
        responses = iter(
            [
                reply(
                    (
                        "skip-special",
                        "run_deck_choice",
                        '{"offer":"special","choice":"skip","reason":"Keep the deck lean."}',
                    )
                )
                if mixed_rewards
                else reply(("invalid", "run_deck_choice", json.dumps(invalid))),
                reply(
                    (
                        "wrong",
                        "run_act",
                        json.dumps(
                            {
                                "steps": [
                                    {
                                        "kind": "interact",
                                        "action": "claim-special" if mixed_rewards else "open1",
                                    }
                                ]
                            }
                        ),
                    )
                ),
                reply(("choice", "run_deck_choice", json.dumps(arguments))),
            ]
        )
        inputs: list[dict[str, JsonValue]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal observation
            if request.url.path == "/status":
                return httpx.Response(
                    200,
                    json={
                        "location": observation.location.model_dump(mode="json"),
                        "presentation": None,
                        "control_intent": "running",
                        "input_readiness": observation.input_readiness.value,
                        "receipt": None,
                    },
                )
            if request.url.path == "/state":
                return httpx.Response(
                    200,
                    content=native.StateCompleted(
                        status=native.ResultStatus.COMPLETED, observation=observation
                    ).to_json(),
                )
            if request.url.path.endswith("/v1/responses"):
                inputs.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
                if mixed_rewards:
                    assert steps == []
                return httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                )
            assert request.url.path == "/action"
            step = native.StepRequest.model_validate_json(request.content)
            steps.append(step.step)
            current = observation.state
            assert isinstance(current, native.RunState)
            if isinstance(step.step, native.Interact):
                if not requires_selection or mixed_rewards and step.step.action == "proceed":
                    expected = "open1" if not mixed_rewards and choice == "take" else "proceed"
                    assert step.step.action == expected
                    if mixed_rewards:
                        assert [offer.ref for offer in current.offers] == ["special"]
                        assert current.deck == ("owned", "c1")
                    current = current.model_copy(
                        update={
                            "offers": (),
                            "actions": (),
                            "deck": (*current.deck, "c1")
                            if step.step.action == "open1"
                            else current.deck,
                        }
                    )
                    observation = observation.model_copy(update={"state": current})
                    return httpx.Response(
                        200,
                        json={
                            "operation_id": step.operation_id,
                            "status": "completed",
                            "boundary": "ready",
                        },
                    )
                assert step.step.action == "open1"
                current = current.model_copy(
                    update={
                        "selection": native.Selection(
                            prompt="Pick",
                            source=None,
                            min=0,
                            max=1,
                            candidates=("c1",),
                            selected=(),
                        ),
                    }
                )
                boundary = native.InputReadiness.SELECTION
            else:
                assert isinstance(step.step, native.Select)
                current = current.model_copy(
                    update={
                        "selection": None,
                        "offers": tuple(offer for offer in current.offers if offer.ref != "o1"),
                        "actions": tuple(
                            action for action in current.actions if action.ref != "open1"
                        ),
                        "deck": (*current.deck, *step.step.refs),
                    }
                )
                boundary = native.InputReadiness.READY
            observation = observation.model_copy(
                update={"state": current, "input_readiness": boundary}
            )
            return httpx.Response(
                200,
                json={
                    "operation_id": step.operation_id,
                    "status": "completed",
                    "boundary": boundary.value,
                },
            )

        budget = ToolRecoveryBudget()
        async with ModClient("http://mod", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="deck", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                recovery=budget,
            )
            async with Llm(
                configuration(tmp_path), transport=httpx.MockTransport(respond)
            ) as model:
                tools = ToolCatalog(
                    native.DecisionOwner.RUN,
                    observe=execution.observe,
                    inspect=execution.inspect,
                    catalog=execution.catalog,
                    notes=Notes(""),
                )
                agent = RunAgent(lambda: model, tools, budget)
                task = ActTask(native.RouteKey.model_validate(observation.location.model_dump()))
                result = await execution.run_unit(agent, task)
                if mixed_rewards:
                    assert len(steps) == 2
                    result = await execution.run_unit(agent, task)
        assert isinstance(result, Executed)
        assert isinstance(result.outcome.outcome, ActionCompleted)
        assert len(steps) == (3 if mixed_rewards else 2 if requires_selection else 1)
        if mixed_rewards:
            assert isinstance(steps[0], native.Interact) and steps[0].action == "open1"
            assert isinstance(steps[-1], native.Interact) and steps[-1].action == "proceed"
            assert len(inputs) == 3
            messages = inputs[-1]["input"]
            assert isinstance(messages, list)
            rejection = next(
                message
                for message in messages
                if isinstance(message, dict)
                and message.get("type") == "function_call_output"
                and message.get("call_id") == "wrong"
            )
            assert isinstance(rejection, dict) and isinstance(rejection["output"], str)
            assert (
                json.loads(rejection["output"])["outcome"]["reason"]["code"] == "invalid_arguments"
            )
        if requires_selection:
            assert isinstance(steps[1], native.Select)
            assert steps[1].refs == (("c1",) if choice == "take" else ())
        assert isinstance(observation.state, native.RunState)
        assert [offer.ref for offer in observation.state.offers] == (
            ["o2"] if requires_selection and not mixed_rewards else []
        )
        assert observation.state.deck == (("owned", "c1") if choice == "take" else ("owned",))

    asyncio.run(scenario())
