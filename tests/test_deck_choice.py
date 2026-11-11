import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
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


@pytest.mark.parametrize("choice", ["take", "skip"])
def test_reward_choice_opens_one_group_then_uses_native_selection(
    choice: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        observation = native.Observation.model_validate_json(RUN)
        assert isinstance(observation.state, native.RunState)
        card = native.CardEntry.model_validate_json(CARD)
        state = observation.state.model_copy(
            update={
                "screen": native.RunScreen.REWARDS,
                "deck": ("owned",),
                "offers": tuple(
                    native.Offer(
                        ref=f"o{i}",
                        kind=native.OfferKind.CARD,
                        contents=(f"c{i}",),
                        actions=(f"open{i}",),
                    )
                    for i in (1, 2)
                ),
                "actions": tuple(
                    native.Action(ref=f"open{i}", label="Reward", enabled=True, subject=f"o{i}")
                    for i in (1, 2)
                ),
            }
        )
        observation = observation.model_copy(
            update={
                "state": state,
                "objects": {"owned": card, "c1": card, "c2": card},
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
        responses = iter(
            [
                reply(("wrong", "run_act", '{"steps":[{"kind":"interact","action":"open1"}]}')),
                reply(("choice", "run_deck_choice", json.dumps(arguments))),
            ]
        )

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
                return httpx.Response(
                    200, content=next(responses), headers={"Content-Type": "text/event-stream"}
                )
            assert request.url.path == "/action"
            step = native.StepRequest.model_validate_json(request.content)
            steps.append(step.step)
            current = observation.state
            assert isinstance(current, native.RunState)
            if isinstance(step.step, native.Interact):
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
                        "offers": current.offers[1:],
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
                result = await execution.run_unit(
                    agent,
                    ActTask(native.RouteKey.model_validate(observation.location.model_dump())),
                )
        assert isinstance(result, Executed)
        assert isinstance(result.outcome.outcome, ActionCompleted)
        assert len(steps) == 2
        assert isinstance(steps[1], native.Select)
        assert steps[1].refs == (("c1",) if choice == "take" else ())
        assert isinstance(observation.state, native.RunState)
        assert [offer.ref for offer in observation.state.offers] == ["o2"]
        assert observation.state.deck == (("owned", "c1") if choice == "take" else ("owned",))

    asyncio.run(scenario())
