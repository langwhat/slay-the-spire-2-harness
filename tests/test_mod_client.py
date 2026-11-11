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
    CatalogMode,
    CombatState,
    CostSymbol,
    DetailData,
    ErrorCode,
    InspectArea,
    InspectCompleted,
    InspectFailed,
    InspectRejected,
    LookupData,
    LookupQuery,
    MapQuery,
    NativeCategory,
    ReadError,
    RunState,
    StateCompleted,
    TerminalState,
    UpgradeAvailable,
)

RUN = """{
  "game_language":"zhs",
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
  "game_language":"zhs",
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
    "deck":["c1"],"turn":null,"piles":{"hand":["c2","c1"],"draw":[],"discard":[],"exhaust":[],"other":[]},
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
    "nodes":[{"position":"0:0","room_type":"monster","children":[],"odds":{"monster":1}}],
    "legal_destinations":null}
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


def test_observation_requires_native_language_and_preserves_optional_operation() -> None:
    from pydantic import ValidationError

    from sts2_harness.client.models import ActionOperation, Observation

    payload = json.loads(RUN)
    for language in ("eng", "zhs", "", "future-language"):
        payload["game_language"] = language
        payload["state"]["actions"][0]["operation"] = "proceed"
        observed = Observation.model_validate_json(json.dumps(payload))
        assert observed.game_language == language
        assert isinstance(observed.state, RunState)
        assert observed.state.actions[0].operation is ActionOperation.PROCEED
    for invalid in (None, 3):
        payload["game_language"] = invalid
        with pytest.raises(ValidationError):
            Observation.model_validate_json(json.dumps(payload))
    del payload["game_language"]
    with pytest.raises(ValidationError):
        Observation.model_validate_json(json.dumps(payload))
