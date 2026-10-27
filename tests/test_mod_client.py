import asyncio
import json
from collections.abc import MutableMapping

import httpx2 as httpx
import pytest
from pydantic import MISSING, JsonValue, TypeAdapter

from sts2_harness.client import ClientFailureKind, ClientUnavailable, ModClient
from sts2_harness.client.models import (
    CardEntry,
    CardQuery,
    CatalogCompleted,
    CatalogFailed,
    CatalogMode,
    CatalogQuery,
    CatalogRequest,
    CombatState,
    ControlIntent,
    CostSymbol,
    DetailData,
    ErrorCode,
    InputReadiness,
    InspectArea,
    InspectCompleted,
    InspectFailed,
    InspectRejected,
    InvalidRequestReply,
    LookupData,
    LookupQuery,
    MapQuery,
    NativeCategory,
    ReadError,
    RunState,
    SearchQuery,
    StateCompleted,
    Status,
    TechnicalError,
    TerminalState,
    UpgradeAvailable,
)

RUN = """{
  "stamp":{"game_instance_id":"game","observation_sequence":1},
  "location":{"game_instance_id":"game","run_id":"run","node_id":"node","owner":"run"},
  "input_readiness":"ready",
  "state":{
    "phase":"run","screen":"map","control":"ready",
    "progress":{"act":1,"floor":0,"position":null,"room_type":null},
    "player":{"name":"角色","definition_id":"CHARACTER.TEST","hp":10,"max_hp":10,
      "gold":0,"potion_slot_count":1,"empty_potion_slot_count":1},
    "relics":[],"potions":[],
    "actions":[{"ref":"travel","label":"前进","enabled":false,"reason":"等待"}],
    "selection":null,"deck":[],"offers":[],"event":null
  },
  "objects":{},"descriptions":{},"modifier_descriptions":{},"upgrades":{},
  "generated":{},"mechanisms":[]
}"""

CARD_FACTS = """
  "definition_id":"CARD.ONE","name":"牌","upgrades":0,"card_type":"attack",
  "card_pool":null,"rarity":null,"is_upgradable":true,"is_removable":null,"keywords":[],
  "should_retain_this_turn":false,"exhaust_on_next_play":false,"has_turn_end_in_hand_effect":null,
  "enchantment":{"definition_id":"ENCHANTMENT.ONE","name":"附魔","amount":0},
  "affliction":null,"cost":{"energy":"X","stars":"none"}
"""
PREVIEW = "{" + CARD_FACTS + ',"text":"造成3点伤害。"}'
CARD = (
    '{"kind":"card",'
    + CARD_FACTS
    + ',"playable":null,"previews":[{"target":"e1","text":"造成3点伤害。"}]}'
)
COMBAT = """{
  "stamp":{"game_instance_id":"game","observation_sequence":2},
  "location":{"game_instance_id":"game","run_id":"run","node_id":"node","owner":"combat"},
  "input_readiness":"selection",
  "state":{
    "phase":"combat","screen":"card_selection","control":"ready",
    "progress":{"act":1,"floor":1,"position":"0:0","room_type":"monster"},
    "player":{"name":"角色","definition_id":"CHARACTER.TEST","hp":10,"max_hp":10,
      "gold":0,"potion_slot_count":1,"empty_potion_slot_count":1,"block":0,
      "resources":{"energy":0,"stars":null},
      "powers":[{"name":"力量","definition_id":"POWER.STRENGTH","amount":0}]},
    "relics":[],"potions":[],"actions":[],
    "selection":{"prompt":"选择","source":null,"min":0,"max":1,
      "candidates":["c2","c1"],"selected":[]},
    "turn":null,"piles":{"hand":["c2","c1"],"draw":[],"discard":[],"exhaust":[],"other":[]},
    "enemies":[{"ref":"e1","name":"敌人","definition_id":"ENEMY.ONE","hp":null,
      "max_hp":null,"block":0,"powers":[],"hp_display":"infinite",
      "intents":[{"type":"unknown"},{"type":"attack","damage_per_hit":null,"hits":2}]}],
    "inactive_enemies":[],"orbs":{"capacity":1,"slots":[{"name":"闪电",
      "definition_id":"ORB.LIGHTNING","passive":0,"evoke":null}]},"allies":[]
  },
  "objects":{"c1":CARD_VALUE,"c2":CARD_VALUE},
  "descriptions":{"c1":null,"c2":"造成3点伤害。"},
  "modifier_descriptions":{"c1":{"enchantment":"附魔说明","affliction":null}},
  "upgrades":{"c1":{"status":"available","card":PREVIEW_VALUE},
    "c2":{"status":"not_upgradable"},"c3":{"status":"unavailable"}},
  "generated":{"c1":[{"source":{"name":"来源","definition_id":"CARD.ONE"},
    "card":PREVIEW_VALUE}],"c2":[]},
  "mechanisms":[{"kind":"power","definition_id":"POWER.STRENGTH","name":"力量",
    "owner":"player","text":"甲"},{"kind":"power","definition_id":"POWER.STRENGTH",
    "name":"力量","owner":"e1","text":"乙"}],
  "map":{"bosses":[{"name":"首领","definition_id":"ENCOUNTER.BOSS"}],
    "nodes":[{"position":"0:0","room_type":"monster","children":[],"odds":{"monster":1}}]}
}""".replace("CARD_VALUE", CARD).replace("PREVIEW_VALUE", PREVIEW)


def _canonical_json(payload: str) -> str:
    return json.dumps(TypeAdapter(JsonValue).validate_json(payload), sort_keys=True)


def test_combat_raw_preserves_source_details_nulls_and_membership() -> None:
    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content='{"status":"completed","observation":' + COMBAT + "}"
                )
            ),
        ) as client:
            result = await client.state()
        assert isinstance(result, StateCompleted)
        observation = result.observation
        assert isinstance(observation.state, CombatState)
        assert observation.state.piles.hand == ("c2", "c1")
        card = observation.objects["c1"]
        assert isinstance(card, CardEntry)
        assert not isinstance(observation.objects, MutableMapping)
        assert card.cost.energy is CostSymbol.X
        assert card.cost.stars is CostSymbol.NONE
        assert card.playable is None
        assert card.local_keywords is MISSING
        assert observation.descriptions["c1"] is None
        assert observation.generated["c1"][0].source.definition_id == "CARD.ONE"
        assert observation.generated["c2"] == ()
        assert tuple(mechanism.text for mechanism in observation.mechanisms) == ("甲", "乙")
        assert _canonical_json(observation.to_json()) == _canonical_json(COMBAT)

    asyncio.run(read())


def test_terminal_is_a_complete_observation_not_no_run() -> None:
    terminal = (
        RUN.replace(
            '"phase":"run","screen":"map","control":"ready"',
            '"phase":"terminal","screen":"run_complete","control":"unavailable"',
        )
        .replace('"owner":"run"', '"owner":null')
        .replace('"input_readiness":"ready"', '"input_readiness":"terminal"')
        .replace('[{"ref":"travel","label":"前进","enabled":false,"reason":"等待"}]', "[]")
        .replace('"offers":[],"event":null', '"result":"victory"')
    )

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content='{"status":"completed","observation":' + terminal + "}"
                )
            ),
        ) as client:
            result = await client.state()
        assert isinstance(result, StateCompleted)
        assert isinstance(result.observation.state, TerminalState)
        assert result.observation.state.actions == ()
        assert result.observation.location.run_id == "run"

    asyncio.run(read())


def test_state_keeps_complete_current_observation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/state"
        return httpx.Response(200, content='{"status":"completed","observation":' + RUN + "}")

    async def read() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(handler)) as client:
            result = await client.state()
        assert isinstance(result, StateCompleted)
        assert isinstance(result.observation.state, RunState)
        assert result.observation.state.player.gold == 0
        assert result.observation.state.actions[0].label == "前进"
        assert result.observation.location.node_id == "node"

    asyncio.run(read())


def test_failed_reads_never_borrow_a_previous_successful_observation() -> None:
    responses = iter(
        [
            '{"status":"completed","observation":' + RUN + "}",
            '{"status":"failed","error":{"code":"no_run","message":"No active run."}}',
            '{"status":"failed","error":{"code":"observation_changed",'
            '"message":"Read again."},"observation":null}',
            '{"status":"rejected","error":{"code":"invalid_refs","message":"Invalid reference.",'
            '"invalid_refs":["missing"]},"observation":' + RUN + "}",
            '{"status":"completed","observation":'
            + RUN.replace('"observation_sequence":1', '"observation_sequence":3')
            + "}",
        ]
    )

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=next(responses))),
        ) as client:
            initial = await client.state()
            absent = await client.state()
            changed = await client.inspect(MapQuery(area=InspectArea.MAP))
            rejected = await client.inspect(MapQuery(area=InspectArea.MAP))
            current = await client.state()
        assert isinstance(initial, StateCompleted)
        assert isinstance(absent, ReadError) and absent.error.code is ErrorCode.NO_RUN
        assert isinstance(changed, InspectFailed) and changed.observation is None
        assert isinstance(rejected, InspectRejected) and rejected.observation is not None
        assert isinstance(current, StateCompleted)
        assert current.observation.stamp.observation_sequence == 3
        assert initial.observation.stamp.observation_sequence == 1

    asyncio.run(read())


@pytest.mark.parametrize(
    ("readiness", "receipt"),
    [
        ("busy", '{"operation_id":"op","status":"pending"}'),
        ("selection", '{"operation_id":"op","status":"completed","boundary":"selection"}'),
        (
            "ready",
            '{"operation_id":"op","status":"rejected","error":{"code":"invalid_refs","message":"Expired.","invalid_refs":["c1"]}}',
        ),
        (
            "unavailable",
            '{"operation_id":"op","status":"failed","error":{"code":"identity_unregistered","message":"Unavailable."}}',
        ),
        (
            "ready",
            '{"operation_id":"op","status":"unknown","error":{"code":"execution_uncertain",'
            '"message":"Inspect before deciding."}}',
        ),
        ("ready", "null"),
    ],
)
def test_status_keeps_control_intent_separate_from_native_input(
    readiness: str, receipt: str
) -> None:
    payload = (
        '{"location":{"game_instance_id":"game","run_id":"run","node_id":"node","owner":"run"},'
        '"control_intent":"paused","input_readiness":"' + readiness + '","receipt":' + receipt + "}"
    )

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)),
        ) as client:
            result = await client.status()
        assert isinstance(result, Status)
        assert result.control_intent is ControlIntent.PAUSED
        assert result.input_readiness is InputReadiness(readiness)
        assert _canonical_json(result.to_json()) == _canonical_json(payload)

    asyncio.run(read())


@pytest.mark.parametrize(
    ("run_id", "node_id", "owner", "valid"),
    [
        ("run", "node", None, True),
        ("run", "node", "combat", False),
        (None, "node", None, False),
        ("run", None, None, False),
        (None, None, None, False),
    ],
)
def test_terminal_status_retains_last_run_and_node_without_a_decision_owner(
    run_id: str | None, node_id: str | None, owner: str | None, valid: bool
) -> None:
    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "location": {
                            "game_instance_id": "game",
                            "run_id": run_id,
                            "node_id": node_id,
                            "owner": owner,
                        },
                        "control_intent": "paused",
                        "input_readiness": "terminal",
                        "receipt": None,
                    },
                )
            ),
        ) as client:
            if not valid:
                with pytest.raises(ClientUnavailable) as failure:
                    await client.status()
                assert failure.value.reason is ClientFailureKind.PROTOCOL
                return
            result = await client.status()
        assert isinstance(result, Status)
        assert result.location.run_id == "run"
        assert result.location.node_id == "node"
        assert result.location.owner is None

    asyncio.run(read())


@pytest.mark.parametrize("category", list(NativeCategory))
def test_lookup_decodes_each_concrete_native_category(category: NativeCategory) -> None:
    extra = {
        NativeCategory.RELIC: ',"rarity":null',
        NativeCategory.POTION: ',"target_type":null,"usage":null',
        NativeCategory.POWER: ',"type":null',
    }.get(category, "")
    native = (
        PREVIEW.replace("CARD.ONE", "NATIVE.ONE")
        if category is NativeCategory.CARD
        else ('{"definition_id":"NATIVE.ONE","name":"原生","text":"完整原生规则。"' + extra + "}")
    )
    upgrade = ',"upgrade":{"status":"unavailable"}' if category is NativeCategory.CARD else ""
    payload = (
        '{"status":"completed","data":{"entries":[{"category":"'
        + category
        + '","definition_id":"NATIVE.ONE","name":"原生","native":'
        + native
        + upgrade
        + '}],"missing_ids":["NATIVE.MISSING"]}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/catalog"
        assert request.content == b'{"mode":"lookup","ids":["NATIVE.ONE","NATIVE.MISSING"]}'
        return httpx.Response(200, content=payload)

    async def read() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(handler)) as client:
            result = await client.catalog(
                LookupQuery(mode=CatalogMode.LOOKUP, ids=("NATIVE.ONE", "NATIVE.MISSING"))
            )
        assert isinstance(result, CatalogCompleted) and isinstance(result.data, LookupData)
        assert result.data.entries[0].category is category
        assert result.data.missing_ids == ("NATIVE.MISSING",)
        assert _canonical_json(result.to_json()) == _canonical_json(payload)

    asyncio.run(read())


@pytest.mark.parametrize(
    ("found", "missing", "valid"),
    [
        ((("A", "A"), ("C", "C")), ("B", "D"), True),
        ((("A", "A"),), ("B",), False),
        ((("A", "A"), ("C", "C"), ("E", "E")), ("B", "D"), False),
        ((("A", "A"), ("C", "C")), ("B", "D", "E"), False),
        ((("A", "A"), ("A", "A"), ("C", "C")), ("B", "D"), False),
        ((("A", "A"), ("C", "C")), ("B", "B", "D"), False),
        ((("A", "A"), ("C", "C")), ("A", "B", "D"), False),
        ((("C", "C"), ("A", "A")), ("B", "D"), False),
        ((("A", "A"), ("C", "C")), ("D", "B"), False),
        ((("A", "Z"), ("C", "C")), ("B", "D"), False),
    ],
)
def test_lookup_accounts_for_requested_ids_in_order_with_matching_native_identity(
    found: tuple[tuple[str, str], ...], missing: tuple[str, ...], valid: bool
) -> None:
    payload = json.dumps(
        {
            "status": "completed",
            "data": {
                "entries": [
                    {
                        "category": "relic",
                        "definition_id": outer_id,
                        "name": "Relic",
                        "native": {
                            "definition_id": native_id,
                            "name": "Native relic",
                            "text": None,
                            "rarity": None,
                        },
                    }
                    for outer_id, native_id in found
                ],
                "missing_ids": missing,
            },
        }
    )

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload)),
        ) as client:
            query = LookupQuery(mode=CatalogMode.LOOKUP, ids=("A", "B", "C", "D"))
            if not valid:
                with pytest.raises(ClientUnavailable) as failure:
                    await client.catalog(query)
                assert failure.value.reason is ClientFailureKind.PROTOCOL
                return
            result = await client.catalog(query)
        assert isinstance(result, CatalogCompleted) and isinstance(result.data, LookupData)
        assert tuple(entry.definition_id for entry in result.data.entries) == ("A", "C")
        assert result.data.missing_ids == ("B", "D")

    asyncio.run(read())


def test_lookup_unknown_category_is_catalog_unavailable_not_a_missing_id() -> None:
    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "data": {
                            "entries": [
                                {
                                    "category": "future",
                                    "definition_id": "FUTURE.ID",
                                    "name": "Future",
                                    "native": {"name": "Future"},
                                }
                            ],
                            "missing_ids": [],
                        },
                    },
                )
            ),
        ) as client:
            result = await client.catalog(LookupQuery(mode=CatalogMode.LOOKUP, ids=("FUTURE.ID",)))
        assert isinstance(result, CatalogFailed)
        assert result.error.code is ErrorCode.CATALOG_UNAVAILABLE

    asyncio.run(read())


@pytest.mark.parametrize(
    ("query", "data"),
    [
        (CatalogQuery(mode=CatalogMode.CATALOG), '{"entries":[],"missing_ids":[]}'),
        (
            SearchQuery(mode=CatalogMode.SEARCH),
            '{"categories":[],"card_filters":{"pools":[],"types":[],"rarities":[],"energy_costs":[]}}',
        ),
        (
            LookupQuery(mode=CatalogMode.LOOKUP, ids=("CARD.ONE",)),
            '{"matches":[],"total":0,"next_offset":null}',
        ),
    ],
)
def test_catalog_success_data_must_match_the_requested_mode(
    query: CatalogRequest, data: str
) -> None:
    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content='{"status":"completed","data":' + data + "}")
            ),
        ) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.catalog(query)
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())


def test_inspection_rejects_data_from_another_area_without_a_second_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/inspect"
        assert request.content == b'{"query":{"area":"map"}}'
        return httpx.Response(
            200,
            content='{"status":"completed","observation":'
            + RUN
            + ',"data":{"cards_played_this_combat":[],"cards_played_this_turn":[],'
            '"cards_exhausted_this_turn":[]}}',
        )

    async def read() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.inspect(MapQuery(area=InspectArea.MAP))
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())


def test_explicit_upgrade_details_do_not_overwrite_live_objects() -> None:
    detail = (
        '{"objects":{"c1":' + CARD + '},"descriptions":{"c1":"完整描述。"},'
        '"modifier_descriptions":{},"generated":{},"mechanisms":[],"upgrades":'
        '{"c1":{"status":"available","card":'
        + PREVIEW.replace('"upgrades":0', '"upgrades":1')
        + "}}}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b'{"query":{"area":"hand","refs":["c1"],"upgrade":true}}'
        return httpx.Response(
            200, content='{"status":"completed","observation":' + COMBAT + ',"data":' + detail + "}"
        )

    async def read() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(handler)) as client:
            result = await client.inspect(
                CardQuery(area=InspectArea.HAND, refs=("c1",), upgrade=True)
            )
        assert isinstance(result, InspectCompleted) and isinstance(result.data, DetailData)
        assert isinstance(result.observation.objects["c1"], CardEntry)
        assert result.observation.objects["c1"].upgrades == 0
        assert result.data.upgrades is not MISSING
        preview = result.data.upgrades["c1"]
        assert isinstance(preview, UpgradeAvailable) and preview.card.upgrades == 1

    asyncio.run(read())


@pytest.mark.parametrize(("upgrade", "upgrades"), [(True, ""), (False, ',"upgrades":{}')])
def test_detail_upgrade_material_matches_the_request(upgrade: bool, upgrades: str) -> None:
    data = (
        '{"objects":{},"descriptions":{},"modifier_descriptions":{},"generated":{},"mechanisms":[]'
        + upgrades
        + "}"
    )

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content='{"status":"completed","observation":' + RUN + ',"data":' + data + "}",
                )
            ),
        ) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.inspect(CardQuery(area=InspectArea.DECK, upgrade=upgrade))
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())


def test_expected_http_boundary_errors_keep_their_structured_bodies() -> None:
    async def read() -> None:
        responses = iter(
            [
                httpx.Response(
                    400,
                    json={
                        "status": "rejected",
                        "error": {
                            "code": "invalid_request",
                            "message": "Invalid request.",
                            "fields": ["query.refs"],
                        },
                    },
                ),
                httpx.Response(
                    500,
                    json={
                        "status": "failed",
                        "error": {"code": "internal_error", "message": "Read failed."},
                    },
                ),
            ]
        )
        async with ModClient(
            "http://mod.test", transport=httpx.MockTransport(lambda _: next(responses))
        ) as client:
            invalid = await client.state()
            failed = await client.state()
        assert isinstance(invalid, InvalidRequestReply)
        assert invalid.error.fields == ("query.refs",)
        assert isinstance(failed, TechnicalError)
        assert failed.error.code is ErrorCode.INTERNAL_ERROR

    asyncio.run(read())


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (200, b"\xff", ClientFailureKind.PROTOCOL),
        (200, b"not json", ClientFailureKind.PROTOCOL),
        (200, b'{"status":"completed","observation":null}', ClientFailureKind.PROTOCOL),
        (
            400,
            b'{"status":"failed","error":{"code":"internal_error","message":"secret"}}',
            ClientFailureKind.PROTOCOL,
        ),
        (503, b"server text", ClientFailureKind.HTTP),
    ],
)
def test_invalid_or_unexpected_http_does_not_become_a_gameplay_result(
    status: int, body: bytes, reason: ClientFailureKind
) -> None:
    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body)),
        ) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.state()
        assert failure.value.reason is reason
        assert "secret" not in str(failure.value)

    asyncio.run(read())


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        (httpx.ReadTimeout, ClientFailureKind.TIMEOUT),
        (httpx.ConnectError, ClientFailureKind.CONNECTION),
    ],
)
def test_transport_failures_do_not_implicitly_repeat_reads(
    error_type: type[httpx.TransportError], reason: ClientFailureKind
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_type("secret", request=request)

    async def read() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.state()
        assert failure.value.reason is reason
        assert attempts == 1
        assert "secret" not in str(failure.value)

    asyncio.run(read())


def test_invalid_compression_is_a_safe_protocol_failure_without_another_read() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"secret")
        )

    async def read() -> None:
        async with ModClient("http://mod.test", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.status()
        assert failure.value.reason is ClientFailureKind.PROTOCOL
        assert attempts == 1
        assert "secret" not in str(failure.value)

    asyncio.run(read())


@pytest.mark.parametrize(
    "observation",
    [
        RUN.replace('"input_readiness":"ready"', '"input_readiness":"busy"'),
        RUN.replace('"run_id":"run"', '"run_id":null'),
        RUN.replace('"observation_sequence":1', '"observation_sequence":true'),
        RUN.replace(
            '"game_instance_id":"game","observation_sequence"',
            '"game_instance_id":"other","observation_sequence"',
        ),
        RUN.replace('"floor":0', '"floor":0,"floor":1'),
    ],
)
def test_untrustworthy_observation_is_not_success(observation: str) -> None:
    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content='{"status":"completed","observation":' + observation + "}"
                )
            ),
        ) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.state()
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())


@pytest.mark.parametrize(
    ("amount", "valid"),
    [
        ("3", True),
        ("3.0", True),
        ("3.5", True),
        ("true", False),
        ("NaN", False),
        ("Infinity", False),
    ],
)
def test_raw_numbers_preserve_integer_and_decimal_forms_without_accepting_non_numbers(
    amount: str, valid: bool
) -> None:
    observation = COMBAT.replace('"amount":0', '"amount":' + amount)

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content='{"status":"completed","observation":' + observation + "}"
                )
            ),
        ) as client:
            if not valid:
                with pytest.raises(ClientUnavailable) as failure:
                    await client.state()
                assert failure.value.reason is ClientFailureKind.PROTOCOL
                return
            result = await client.state()
        assert isinstance(result, StateCompleted)
        assert _canonical_json(result.observation.to_json()) == _canonical_json(observation)

    asyncio.run(read())


@pytest.mark.parametrize("odds", ["-0.1", "1.1"])
def test_raw_map_odds_retain_probability_bounds(odds: str) -> None:
    observation = COMBAT.replace('"monster":1', '"monster":' + odds)

    async def read() -> None:
        async with ModClient(
            "http://mod.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content='{"status":"completed","observation":' + observation + "}"
                )
            ),
        ) as client:
            with pytest.raises(ClientUnavailable) as failure:
                await client.state()
        assert failure.value.reason is ClientFailureKind.PROTOCOL

    asyncio.run(read())
