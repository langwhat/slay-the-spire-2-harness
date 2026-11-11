import asyncio

import httpx2 as httpx
from pydantic import MISSING
from test_mod_client import COMBAT

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LiveGame, unwrap
from sts2_harness_eval.identity import IdentityGame, TargetIdentity


def test_stale_refs_and_routes_cannot_hit_colliding_native_objects() -> None:
    original = native.Observation.model_validate_json(COMBAT)
    loaded_location = native.Location(
        game_instance_id="B", run_id="B-run", node_id="node-2", owner=native.DecisionOwner.COMBAT
    )
    requests: list[native.StepRequest] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/inspect":
            query = native.InspectRequest.model_validate_json(request.content).query
            assert isinstance(query, native.CardQuery)
            assert query.refs is not MISSING
            assert query.refs == ("eval-stale:card:3:1",)
            return httpx.Response(
                200,
                content=native.InspectRejected(
                    status=native.ResultStatus.REJECTED,
                    error=native.InvalidRefs(
                        code=native.ErrorCode.INVALID_REFS,
                        message="card:3:1 remains opaque text",
                        invalid_refs=query.refs,
                    ),
                    observation=None,
                ).to_json(),
            )
        submission = native.StepRequest.model_validate_json(request.content)
        requests.append(submission)
        if len(requests) == 1:
            assert submission.expected_route.node_id == "node-2"
            assert submission.step == native.PlayCard(
                kind=native.StepKind.PLAY_CARD, card="eval-stale:card:3:1", target="enemy:2:7"
            )
            error = native.InvalidRefs(
                code=native.ErrorCode.INVALID_REFS,
                message="invalid",
                invalid_refs=("eval-stale:card:3:1",),
            )
        else:
            assert submission.expected_route.node_id == "eval-stale:node-3"
            error = native.RouteChanged(
                code=native.ErrorCode.ROUTE_CHANGED,
                message="moved",
                actual_location=loaded_location.model_copy(update={"node_id": "node-3"}),
            )
        return httpx.Response(
            200,
            content=native.ReceiptRejected(
                operation_id=submission.operation_id,
                status=native.ResultStatus.REJECTED,
                error=error,
            ).to_json(),
        )

    async def scenario() -> None:
        async with ModClient("http://B", transport=httpx.MockTransport(respond)) as client:
            game = IdentityGame(
                LiveGame(client, namespace="B-op", game_instance_id="B", run_id="B-run"),
                TargetIdentity(original.location, loaded_location, {"enemy:12:7": "enemy:2:7"}),
            )
            inspected = unwrap(
                await game.inspect(
                    native.CardQuery(area=native.InspectArea.DECK, refs=("card:3:1",))
                )
            ).result
            assert isinstance(inspected, native.InspectRejected)
            assert isinstance(inspected.error, native.InvalidRefs)
            assert inspected.error.invalid_refs == ("card:3:1",)
            assert inspected.error.message == "card:3:1 remains opaque text"
            route = native.RouteKey.model_validate(original.location.model_dump())
            result = unwrap(
                await game.settle(
                    route,
                    native.PlayCard(
                        kind=native.StepKind.PLAY_CARD, card="card:3:1", target="enemy:12:7"
                    ),
                )
            )
            assert isinstance(result.receipt, native.ReceiptRejected)
            assert isinstance(result.receipt.error, native.InvalidRefs)
            assert result.receipt.error.invalid_refs == ("card:3:1",)
            stale_route = route.model_copy(update={"node_id": "node-3"})
            result = unwrap(
                await game.settle(stale_route, native.EndTurn(kind=native.StepKind.END_TURN))
            )
            assert result.request.expected_route == stale_route
            assert isinstance(result.receipt, native.ReceiptRejected)
            assert isinstance(result.receipt.error, native.RouteChanged)
            assert result.receipt.error.actual_location.node_id == "b:node-3"

    asyncio.run(scenario())


def test_inspection_translates_typed_details_and_keeps_rules_and_text_opaque() -> None:
    original = native.Observation.model_validate_json(COMBAT)
    loaded_location = original.location.model_copy(
        update={"game_instance_id": "B", "run_id": "B-run"}
    )
    enemy = native.MechanismView(
        kind=native.MechanismKind.POWER,
        definition_id="POWER.TEST",
        name="Mechanism",
        text="enemy:2:7",
        owner="enemy:2:7",
    )
    shared = enemy.model_copy(update={"owner": "player"})
    card = original.objects["c1"]
    assert isinstance(card, native.CardEntry)
    card = card.model_copy(
        update={"previews": (native.TargetPreview(target="enemy:2:7", text="enemy:2:7"),)}
    )
    loaded = original.model_copy(
        update={
            "location": loaded_location,
            "stamp": original.stamp.model_copy(update={"game_instance_id": "B"}),
        }
    )
    detail = native.DetailData(
        objects={
            "card:2:1": card,
            "bundle:2:4": native.BundleEntry(
                kind=native.ObjectKind.BUNDLE, name="Bundle", members=("card:2:1", "card:2:9")
            ),
        },
        descriptions={"card:2:1": "card:2:1"},
        modifier_descriptions=original.modifier_descriptions,
        generated={"card:2:1": original.generated["c1"], "player": original.generated["c1"]},
        mechanisms=(enemy, shared),
        upgrades={"card:2:1": original.upgrades["c1"]},
    )

    def respond(request: httpx.Request) -> httpx.Response:
        query = native.InspectRequest.model_validate_json(request.content).query
        assert isinstance(query, native.CardQuery)
        assert query.refs == ("card:2:1", "card:2:9")
        return httpx.Response(
            200,
            content=native.InspectCompleted(
                status=native.ResultStatus.COMPLETED, observation=loaded, data=detail
            ).to_json(),
        )

    async def scenario() -> None:
        async with ModClient("http://B", transport=httpx.MockTransport(respond)) as client:
            game = IdentityGame(
                LiveGame(client, namespace="B-op", game_instance_id="B", run_id="B-run"),
                TargetIdentity(
                    original.location,
                    loaded_location,
                    {"card:12:5": "card:2:1", "enemy:12:7": "enemy:2:7"},
                ),
            )
            reply = await game.inspect(
                native.CardQuery(
                    area=native.InspectArea.HAND, refs=("card:12:5", "b:card:2:9"), upgrade=True
                )
            )
            result = unwrap(reply).result
            assert reply.observations[0].location == original.location
            assert isinstance(result, native.InspectCompleted)
            assert isinstance(result.data, native.DetailData)
            logical_card = result.data.objects["card:12:5"]
            assert isinstance(logical_card, native.CardEntry)
            assert logical_card.previews == (
                native.TargetPreview(target="enemy:12:7", text="enemy:2:7"),
            )
            bundle = result.data.objects["b:bundle:2:4"]
            assert isinstance(bundle, native.BundleEntry)
            assert bundle.members == ("card:12:5", "b:card:2:9")
            assert result.data.descriptions == {"card:12:5": "card:2:1"}
            assert result.data.generated["card:12:5"] == original.generated["c1"]
            assert result.data.generated["player"] == original.generated["c1"]
            assert result.data.upgrades is not MISSING
            assert result.data.upgrades["card:12:5"] == original.upgrades["c1"]
            assert tuple(item.owner for item in result.data.mechanisms) == ("enemy:12:7", "player")
            assert tuple(item.text for item in result.data.mechanisms) == ("enemy:2:7", "enemy:2:7")

    asyncio.run(scenario())
