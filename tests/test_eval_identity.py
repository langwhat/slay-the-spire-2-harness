import asyncio

import httpx2 as httpx
import pytest
from pydantic import MISSING
from test_mod_client import COMBAT

from sts2_harness.client import ModClient
from sts2_harness.client import models as native
from sts2_harness.game import LifetimeEnded, LiveGame, unwrap
from sts2_harness_eval.identity import IdentityGame, TargetIdentity


def test_known_moving_and_new_cards_keep_logical_names_and_echo_constructed_alias() -> None:
    original = native.Observation.model_validate_json(COMBAT)
    assert isinstance(original.state, native.CombatState)
    loaded_location = native.Location(
        game_instance_id="B", run_id="B-run", node_id="node-2", owner=native.DecisionOwner.COMBAT
    )
    loaded = original.model_copy(
        update={
            "location": loaded_location,
            "stamp": native.Stamp(game_instance_id="B", observation_sequence=50),
            "state": original.state.model_copy(
                update={
                    "piles": native.Piles(
                        hand=("card:2:9",), draw=(), discard=("card:2:1",), exhaust=(), other=()
                    )
                }
            ),
            "objects": {},
            "descriptions": {},
            "generated": {},
            "upgrades": {},
            "modifier_descriptions": {},
            "mechanisms": (),
        }
    )
    submissions: list[native.StepRequest] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            return httpx.Response(
                200,
                content=native.StateCompleted(
                    status=native.ResultStatus.COMPLETED, observation=loaded
                ).to_json(),
            )
        assert request.url.path == "/action"
        submission = native.StepRequest.model_validate_json(request.content)
        submissions.append(submission)
        return httpx.Response(
            200,
            content=native.ReceiptCompleted(
                operation_id=submission.operation_id,
                status=native.ResultStatus.COMPLETED,
                boundary=native.InputReadiness.READY,
            ).to_json(),
        )

    async def scenario() -> None:
        async with ModClient("http://B", transport=httpx.MockTransport(respond)) as client:
            game = IdentityGame(
                LiveGame(client, namespace="B-op", game_instance_id="B", run_id="B-run"),
                TargetIdentity(original.location, loaded_location, {"card:12:5": "card:2:1"}),
            )
            observed = unwrap(await game.observe()).observation
            assert observed.location == original.location
            assert observed.stamp == native.Stamp(game_instance_id="game", observation_sequence=50)
            assert isinstance(observed.state, native.CombatState)
            assert observed.state.piles.hand == ("b:card:2:9",)
            assert observed.state.piles.discard == ("card:12:5",)
            route = native.RouteKey.model_validate(original.location.model_dump())
            step = native.PlayCard(kind=native.StepKind.PLAY_CARD, card="b:card:2:1")
            settled = unwrap(await game.settle(route, step))
            assert settled.request == native.StepRequest(
                operation_id="B-op:1", expected_route=route, step=step
            )
            assert settled.receipt is not None and settled.receipt.operation_id == "B-op:1"
        assert submissions[0].step == native.PlayCard(
            kind=native.StepKind.PLAY_CARD, card="card:2:1"
        )
        assert submissions[0].expected_route.node_id == "node-2"

    asyncio.run(scenario())


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


def test_player_generation_keeps_its_owner_and_still_compares_its_contents() -> None:
    from sts2_harness_eval.verification import SourceCorrespondence, VerificationFailure

    encoded = COMBAT
    for old, new in (
        ("c1", "card:11:1"),
        ("c2", "card:11:2"),
        ("c3", "card:11:3"),
        ("e1", "enemy:11:4"),
    ):
        encoded = encoded.replace('"' + old + '"', '"' + new + '"')
    source = native.Observation.model_validate_json(encoded)
    source = source.model_copy(
        update={"generated": {**source.generated, "player": source.generated["card:11:1"]}}
    )
    loaded = native.Observation.model_validate_json(source.to_json().replace(":11:", ":21:"))
    correspondence = SourceCorrespondence()
    refs = correspondence.compare(source, loaded, 1)
    assert refs["card:11:1"] == "card:21:1"
    assert "player" not in refs

    async def scenario() -> None:
        async with ModClient("http://unused") as client:
            logical = IdentityGame(
                LiveGame(client, namespace="test", game_instance_id="game", run_id="run"),
                TargetIdentity(source.location, loaded.location, refs),
            ).observation(loaded)
            assert logical.generated["player"] == source.generated["player"]
            assert logical.generated["card:11:1"] == source.generated["card:11:1"]

    asyncio.run(scenario())
    changed = loaded.model_copy(update={"generated": {**loaded.generated, "player": ()}})
    with pytest.raises(VerificationFailure, match="generated.player.length"):
        correspondence.compare(source, changed, 2)


def test_admission_pause_and_lifetime_keep_real_run_changes_visible() -> None:
    original = native.Observation.model_validate_json(COMBAT)
    location = original.location.model_copy(update={"game_instance_id": "B", "run_id": "B-run"})
    loaded = original.model_copy(
        update={
            "location": location,
            "stamp": original.stamp.model_copy(update={"game_instance_id": "B"}),
        }
    )
    current = location

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/control":
            return httpx.Response(
                200,
                content=native.ControlReply(mode=native.ControlIntent.PAUSED).to_json(),
            )
        if request.url.path == "/status":
            return httpx.Response(
                200,
                content=native.Status(
                    location=current,
                    control_intent=native.ControlIntent.RUNNING,
                    input_readiness=native.InputReadiness.READY,
                    receipt=None,
                ).to_json(),
            )
        assert request.url.path == "/state"
        observation = loaded.model_copy(
            update={
                "location": current,
                "stamp": loaded.stamp.model_copy(
                    update={"game_instance_id": current.game_instance_id}
                ),
            }
        )
        return httpx.Response(
            200,
            content=native.StateCompleted(
                status=native.ResultStatus.COMPLETED, observation=observation
            ).to_json(),
        )

    async def scenario() -> None:
        nonlocal current
        async with ModClient("http://B", transport=httpx.MockTransport(respond)) as client:
            game = IdentityGame(
                LiveGame(client, namespace="B-op", game_instance_id="B", run_id="B-run"),
                TargetIdentity(original.location, location, {}),
            )
            admitted = await game.admit()
            assert unwrap(admitted).allowed
            assert admitted.observations[0].location == original.location
            resumed = await game.pause_until_resumed(None)
            assert unwrap(resumed).observation.location == original.location
            current = location.model_copy(update={"node_id": "node-next"})
            moved = unwrap(await game.observe()).observation
            assert moved.location.node_id == "b:node-next"
            current = location.model_copy(update={"run_id": "new-run", "node_id": "node-next"})
            changed = await game.pause_until_resumed(None)

            assert isinstance(changed.outcome, LifetimeEnded)
            assert changed.outcome.location is not None
            assert changed.outcome.location.run_id == "new-run"
            current = current.model_copy(update={"game_instance_id": "new-instance"})
            changed_observation = unwrap(await game.observe()).observation
            assert changed_observation.location.game_instance_id == "new-instance"
            assert changed_observation.stamp.game_instance_id == "new-instance"
            assert changed_observation.location.run_id == "new-run"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("logical", "expected"),
    [
        (
            native.UsePotion(
                kind=native.StepKind.USE_POTION, potion="potion:12:1", target="b:enemy:2:9"
            ),
            native.UsePotion(
                kind=native.StepKind.USE_POTION, potion="potion:2:1", target="enemy:2:9"
            ),
        ),
        (
            native.DiscardPotion(kind=native.StepKind.DISCARD_POTION, potion="potion:12:1"),
            native.DiscardPotion(kind=native.StepKind.DISCARD_POTION, potion="potion:2:1"),
        ),
        (
            native.Interact(kind=native.StepKind.INTERACT, action="interaction:12:2"),
            native.Interact(kind=native.StepKind.INTERACT, action="interaction:2:2"),
        ),
        (
            native.Select(
                kind=native.StepKind.SELECT, refs=("potion:12:1", "b:card:2:9", "card:3:1")
            ),
            native.Select(
                kind=native.StepKind.SELECT, refs=("potion:2:1", "card:2:9", "eval-stale:card:3:1")
            ),
        ),
    ],
)
def test_action_shapes_send_native_refs_and_return_logical_target_errors(
    logical: native.NativeStep, expected: native.NativeStep
) -> None:
    original = native.Observation.model_validate_json(COMBAT)
    location = original.location.model_copy(update={"game_instance_id": "B", "run_id": "B-run"})

    def respond(request: httpx.Request) -> httpx.Response:
        submission = native.StepRequest.model_validate_json(request.content)
        assert submission.step == expected
        return httpx.Response(
            200,
            content=native.ReceiptRejected(
                operation_id=submission.operation_id,
                status=native.ResultStatus.REJECTED,
                error=native.InvalidTarget(
                    code=native.ErrorCode.INVALID_TARGET,
                    message="invalid",
                    target="eval-stale:enemy:3:1",
                    legal_targets=("enemy:2:7", "enemy:2:9"),
                ),
            ).to_json(),
        )

    async def scenario() -> None:
        async with ModClient("http://B", transport=httpx.MockTransport(respond)) as client:
            game = IdentityGame(
                LiveGame(client, namespace="B-op", game_instance_id="B", run_id="B-run"),
                TargetIdentity(
                    original.location,
                    location,
                    {
                        "potion:12:1": "potion:2:1",
                        "interaction:12:2": "interaction:2:2",
                        "enemy:12:7": "enemy:2:7",
                    },
                ),
            )
            route = native.RouteKey.model_validate(original.location.model_dump())
            result = unwrap(await game.settle(route, logical))
            assert result.request.step == logical
            assert isinstance(result.receipt, native.ReceiptRejected)
            assert isinstance(result.receipt.error, native.InvalidTarget)
            assert result.receipt.error.target == "enemy:3:1"
            assert result.receipt.error.legal_targets == ("enemy:12:7", "b:enemy:2:9")

    asyncio.run(scenario())
