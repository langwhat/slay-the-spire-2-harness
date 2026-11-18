import asyncio
import json
from pathlib import Path

import httpx2 as httpx
import pytest
from test_combat import reply
from test_lifecycle import Campaign, run_state
from test_mod_client import CARD
from test_responses import configuration

from sts2_harness.client import ClientFailureKind, ClientUnavailable, ModClient
from sts2_harness.client import models as native
from sts2_harness.config import Configuration, Driver
from sts2_harness.decisions import (
    ActionDecision,
    ActionKind,
    ActionProposal,
    ActionStep,
    ActTask,
    Decision,
    DecisionTask,
    LocalProgress,
)
from sts2_harness.deck_role import DeckAgent
from sts2_harness.execution import Execution
from sts2_harness.execution.recovery import ToolRecoveryBudget
from sts2_harness.game import LiveGame
from sts2_harness.interactive import InteractiveOptions, run_interactive
from sts2_harness.journal import Journal
from sts2_harness.llm.client import Llm
from sts2_harness.notes import Notes
from sts2_harness.results import ActionToolResult, OutcomeStatus
from sts2_harness.tools import ToolCatalog, ToolName


def test_mod_client_display_validates_contract_and_execution_display_is_best_effort() -> None:
    seen: list[str] = []
    mode = "ok"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/display"
        payload = json.loads(request.content)
        seen.append(payload["reason"])
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if mode == "duplicate":
            return httpx.Response(200, content=b'{"status":"shown","status":"shown"}')
        if mode == "rejected":
            return httpx.Response(
                400,
                json={
                    "status": "rejected",
                    "error": {
                        "code": "invalid_request",
                        "message": "Invalid display payload.",
                        "fields": ["reason"],
                    },
                },
            )
        return httpx.Response(200, json={"status": "shown"})

    async def exercise() -> None:
        nonlocal mode
        async with ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client:
            execution = Execution(
                LiveGame(client, namespace="ns", game_instance_id="game", run_id="run"),
                game_instance_id="game",
                run_id="run",
                display=client.display,
            )
            reply = await client.display("Play Bash first.")
            assert isinstance(reply, native.DisplayReply)
            assert reply.status is native.DisplayStatus.SHOWN

            await execution.display_reason(None)
            await execution.display_reason("   \n\t  ")
            await execution.display_reason("  Open chest.  ")
            await execution.display_reason("Open chest.")
            assert seen == ["Play Bash first.", "Open chest."]

            mode = "rejected"
            rejected = await client.display("x")
            assert isinstance(rejected, native.InvalidRequestReply)

            mode = "timeout"
            await execution.display_reason("Timeout should be ignored.")

            mode = "duplicate"
            with pytest.raises(ClientUnavailable) as error:
                await client.display("Bad reply")
            assert error.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(exercise())


def test_interactive_publishes_reasons_without_recording_display_in_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = Campaign()
    displayed: list[tuple[str, int, int]] = []
    fail_display = False
    stop = asyncio.Event()

    def mod_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            return httpx.Response(200, json={"mode": json.loads(request.content)["mode"]})
        if request.url.path == "/display":
            body = json.loads(request.content)
            displayed.append((body["reason"], len(campaign.presentations), len(campaign.actions)))
            if fail_display:
                raise httpx.ReadTimeout("overlay busy", request=request)
            return httpx.Response(200, json={"status": "shown"})
        response = campaign.mod(request)
        if campaign.current.input_readiness is native.InputReadiness.TERMINAL:
            stop.set()
        return response

    model_config = configuration(tmp_path)
    real_init = Llm.__init__

    def fake_llm_init(self: Llm, config: object, **kwargs: object) -> None:
        real_init(
            self,
            model_config,
            transport=httpx.MockTransport(campaign.model),
        )

    monkeypatch.setattr(Llm, "__init__", fake_llm_init)

    async def immediate(_seconds: float) -> None:
        return None

    fail_display = True
    asyncio.run(
        run_interactive(
            InteractiveOptions("http://127.0.0.1:17877", tmp_path / "live"),
            Configuration(Driver.BUILTIN, model_config),
            stop_requested=stop,
            client_factory=lambda: ModClient(
                "http://127.0.0.1:17877", transport=httpx.MockTransport(mod_handler)
            ),
            wait=immediate,
        )
    )

    assert len(displayed) >= 4
    # Route reason from run_plan/run_next is published before /map/present and /action.
    first_reason, presentations_before, actions_before = displayed[0]
    assert first_reason
    assert presentations_before == 0 and actions_before == 0

    archives = list((tmp_path / "live").glob("*/archive.sqlite"))
    assert len(archives) == 1
    archive = Journal.open(archives[0])
    assert archive.gaps == ()
    for op in archive.operations():
        assert op.request.kind.value != "display"
    archive.close()


def test_locally_stopped_map_travel_skips_display_and_local_deck_skip_publishes(
    tmp_path: Path,
) -> None:
    observation = run_state(1, "map-node")
    data = json.loads(observation.to_json())
    data["state"]["screen"] = "map"
    data["state"]["progress"]["position"] = "0:0"
    data["state"]["actions"][0].update(enabled=True, destination="1:0")
    data["state"]["actions"][0].pop("reason", None)
    map_obs = native.Observation.model_validate_json(json.dumps(data))
    assert isinstance(map_obs.state, native.RunState)
    action_ref = map_obs.state.actions[0].ref

    published: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "location": map_obs.location.model_dump(mode="json"),
                    "control_intent": "running",
                    "input_readiness": "ready",
                    "receipt": None,
                },
            )
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=map_obs
                ).to_json(),
            )
        raise AssertionError(f"Unexpected path {request.url.path}")

    class IllegalMapSource:
        async def decide(self, task: DecisionTask, observation: native.Observation) -> Decision:
            return ActionDecision(
                ActionProposal(
                    (ActionStep(ActionKind.INTERACT, action_ref),),
                    reason="Should not be shown when blocked locally.",
                    allow_map_travel=False,
                )
            )

        async def accept(self, outcome: ActionToolResult) -> None:
            assert outcome.outcome.status is OutcomeStatus.STOPPED

    async def record_display(text: str) -> None:
        published.append(text)

    responses = iter(
        (
            reply(
                (
                    "call-skip",
                    ToolName.RUN_DECK_CHOICE.value,
                    json.dumps(
                        {
                            "choice": "skip",
                            "offer": "offer:1",
                            "reason": "Skip redundant attack to keep draw lean.",
                        }
                    ),
                )
            ),
            reply(text="Done."),
        )
    )

    def deck_model(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=next(responses), headers={"Content-Type": "text/event-stream"}
        )

    async def exercise() -> None:
        async with (
            ModClient("http://mod.test", transport=httpx.MockTransport(respond)) as client,
            Llm(configuration(tmp_path), transport=httpx.MockTransport(deck_model)) as model,
        ):
            execution = Execution(
                LiveGame(
                    client,
                    namespace="ns",
                    game_instance_id=map_obs.location.game_instance_id,
                    run_id=map_obs.location.run_id or "run",
                ),
                game_instance_id=map_obs.location.game_instance_id,
                run_id=map_obs.location.run_id or "run",
                display=record_display,
            )
            route = native.RouteKey.model_validate(map_obs.location.model_dump())
            await execution.run_unit(IllegalMapSource(), ActTask(route))
            assert published == []

            # DeckAgent local decline (requires_selection=False, skip) still publishes reason.
            reward_raw = json.loads(run_state(1, "rewards").to_json())
            reward_raw["state"]["screen"] = "rewards"
            reward_raw["state"]["offers"] = [
                {
                    "ref": "offer:1",
                    "kind": "card",
                    "contents": ["card:1"],
                    "actions": [reward_raw["state"]["actions"][0]["ref"]],
                    "requires_selection": False,
                }
            ]
            reward_raw["objects"]["card:1"] = json.loads(CARD)
            reward_obs = native.Observation.model_validate_json(json.dumps(reward_raw))

            async def observe_reward() -> native.Observation:
                return reward_obs

            async def unused_inspect(query: native.InspectQuery) -> native.InspectReply:
                raise AssertionError(query)

            async def unused_catalog(request: native.CatalogRequest) -> native.CatalogReply:
                raise AssertionError(request)

            tools = ToolCatalog(
                native.DecisionOwner.RUN,
                observe=observe_reward,
                inspect=unused_inspect,
                catalog=unused_catalog,
                display=execution.display_reason,
                notes=Notes(""),
                focused_deck=True,
            )
            deck = DeckAgent(
                lambda: model,
                tools,
                ToolRecoveryBudget(),
                offer_ref="offer:1",
            )
            reward_route = native.RouteKey.model_validate(reward_obs.location.model_dump())
            decision = await deck.decide(ActTask(reward_route), reward_obs)
            assert isinstance(decision, LocalProgress)
            assert published == ["Skip redundant attack to keep draw lean."]

    asyncio.run(exercise())
