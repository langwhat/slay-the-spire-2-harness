"""Automated tests for the standalone skills/play-sts2/scripts/sts2_cli.py client."""

import importlib.util
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

_CLI_PATH = Path(__file__).resolve().parents[1] / "skills" / "play-sts2" / "scripts" / "sts2_cli.py"

type JsonObj = dict[str, object]
type AutoStep = tuple[JsonObj, str, tuple[str, str] | None] | None


def _load_cli_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("play_sts2_cli", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_cli_module()
_NS: dict[str, object] = vars(_MOD)
_render_reply = cast(Callable[..., str], _NS["render_reply"])
_record_guards = cast(
    Callable[[JsonObj, JsonObj], None],
    _NS["_record_observed_auto_guards"],
)
_next_auto = cast(Callable[..., AutoStep], _NS["_next_auto_step"])
_auto_advance = cast(
    Callable[..., tuple[JsonObj, list[str]]],
    _NS["_run_auto_advance"],
)
_cmd_call = cast(Callable[..., int], _NS["cmd_call"])


def _base_run_payload(
    *,
    run_id: str = "run-1",
    node_id: str = "node-1",
    game_instance_id: str = "game-1",
    act: int = 1,
    floor: int = 2,
    position: str = "map:1:0",
    room_type: str = "monster",
    screen: str = "rewards",
    empty_potions: int = 1,
    offers: Sequence[Mapping[str, object]] | None = None,
    actions: Sequence[Mapping[str, object]] | None = None,
    selection: Mapping[str, object] | None = None,
    objects: Mapping[str, object] | None = None,
    descriptions: Mapping[str, object] | None = None,
    mechanisms: Sequence[Mapping[str, object]] | None = None,
    outcome: Mapping[str, object] | None = None,
    include_location: bool = True,
) -> JsonObj:
    payload: JsonObj = {
        "outcome": dict(outcome) if outcome is not None else {"status": "completed"},
        "state": {
            "phase": "run",
            "screen": screen,
            "control": "ready",
            "progress": {
                "acts": 3,
                "act": act,
                "floor": floor,
                "position": position,
                "room_type": room_type,
                "bosses_left": 1,
            },
            "player": {
                "name": "Ironclad",
                "definition_id": "CHARACTER.IRONCLAD",
                "hp": 70,
                "max_hp": 80,
                "gold": 99,
                "potion_slot_count": 3,
                "empty_potion_slot_count": empty_potions,
                "base_draw": 5,
                "base_energy": 3,
            },
            "relics": ["relic-1"],
            "potions": [],
            "deck": ["card-1"],
            "offers": [dict(item) for item in offers] if offers is not None else [],
            "actions": [dict(item) for item in actions] if actions is not None else [],
            "selection": dict(selection) if selection is not None else None,
            "event": None,
            "card_rarity_offset": 0.0,
        },
        "objects": {
            "relic-1": {
                "kind": "relic",
                "definition_id": "RELIC.BURNING_BLOOD",
                "name": "Burning Blood",
                "status": "normal",
                "is_used_up": False,
                "is_melted": False,
                "stack_count": 1,
            },
            "card-1": {
                "kind": "card",
                "definition_id": "CARD.STRIKE_IRONCLAD",
                "name": "Strike",
                "upgrades": 0,
                "card_type": "attack",
                "card_pool": "ironclad",
                "rarity": "basic",
                "is_upgradable": True,
                "is_removable": True,
                "keywords": [],
                "should_retain_this_turn": False,
                "exhaust_on_next_play": False,
                "has_turn_end_in_hand_effect": False,
                "enchantment": None,
                "affliction": None,
                "cost": {"energy": 1},
            },
            **(dict(objects) if objects is not None else {}),
        },
        "descriptions": {
            "relic-1": "At the end of combat, heal 6 HP.",
            "card-1": "Deal 6 damage.",
            **(dict(descriptions) if descriptions is not None else {}),
        },
        "modifier_descriptions": {},
        "upgrades": {},
        "generated": {},
        "mechanisms": (
            [dict(item) for item in mechanisms]
            if mechanisms is not None
            else [
                {
                    "kind": "other",
                    "definition_id": "ENCOUNTER.BOSS_A",
                    "name": "Act Boss",
                    "text": "First move: SLAM",
                    "owner": "act",
                }
            ]
        ),
    }
    if include_location:
        payload["location"] = {
            "game_instance_id": game_instance_id,
            "run_id": run_id,
            "node_id": node_id,
            "owner": "run",
        }
    return payload


def _combat_payload(
    *,
    mechanisms: Sequence[Mapping[str, object]] | None = None,
) -> JsonObj:
    return {
        "outcome": {"status": "completed"},
        "location": {
            "game_instance_id": "game-1",
            "run_id": "run-1",
            "node_id": "node-2",
            "owner": "combat",
        },
        "state": {
            "phase": "combat",
            "screen": "combat",
            "control": "ready",
            "turn": 1,
            "progress": {
                "acts": 3,
                "act": 1,
                "floor": 3,
                "position": "map:2:0",
                "room_type": "monster",
                "bosses_left": 1,
            },
            "player": {
                "name": "Ironclad",
                "definition_id": "CHARACTER.IRONCLAD",
                "hp": 65,
                "max_hp": 80,
                "gold": 99,
                "potion_slot_count": 3,
                "empty_potion_slot_count": 2,
                "block": 0,
                "resources": {"energy": 3, "stars": 0},
                "powers": [],
            },
            "relics": ["relic-1"],
            "potions": [],
            "deck": ["card-1"],
            "piles": {
                "hand": ["card-1"],
                "draw": [],
                "discard": [],
                "exhaust": [],
                "other": [],
            },
            "enemies": [
                {
                    "ref": "enemy-1",
                    "name": "Cultist",
                    "definition_id": "MONSTER.CULTIST",
                    "hp": 48,
                    "max_hp": 48,
                    "block": 0,
                    "hp_display": "numeric",
                    "powers": [],
                    "intents": [{"type": "attack", "damage": 6, "hits": 1}],
                    "next_move_id": "INCANTATION",
                }
            ],
            "inactive_enemies": [],
            "actions": [],
            "selection": None,
        },
        "objects": {
            "relic-1": {
                "kind": "relic",
                "definition_id": "RELIC.BURNING_BLOOD",
                "name": "Burning Blood",
                "status": "normal",
                "is_used_up": False,
                "is_melted": False,
                "stack_count": 1,
            },
            "card-1": {
                "kind": "card",
                "definition_id": "CARD.STRIKE_IRONCLAD",
                "name": "Strike",
                "upgrades": 0,
                "card_type": "attack",
                "card_pool": "ironclad",
                "rarity": "basic",
                "is_upgradable": True,
                "is_removable": True,
                "keywords": [],
                "should_retain_this_turn": False,
                "exhaust_on_next_play": False,
                "has_turn_end_in_hand_effect": False,
                "enchantment": None,
                "affliction": None,
                "cost": {"energy": 1},
                "playable": True,
            },
            "enemy-1": {
                "kind": "enemy",
                "name": "Cultist",
                "definition_id": "MONSTER.CULTIST",
                "hp": 48,
                "max_hp": 48,
                "block": 0,
                "hp_display": "numeric",
                "powers": [],
                "intents": [{"type": "attack", "damage": 6, "hits": 1}],
                "next_move_id": "INCANTATION",
            },
        },
        "descriptions": {
            "relic-1": "At the end of combat, heal 6 HP.",
            "card-1": "Deal 6 damage.",
        },
        "modifier_descriptions": {},
        "upgrades": {},
        "generated": {},
        "mechanisms": [dict(item) for item in mechanisms] if mechanisms is not None else [],
    }


def _start_fake_server(
    replies: Sequence[JsonObj],
) -> tuple[ThreadingHTTPServer, str, list[tuple[str, JsonObj]]]:
    requests_log: list[tuple[str, JsonObj]] = []
    queue = list(replies)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            requests_log.append((self.path, cast(JsonObj, json.loads(raw))))
            body: JsonObj = (
                queue.pop(0) if queue else {"outcome": {"status": "failed"}, "state": None}
            )
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = server.server_address[0]
    port = server.server_address[1]
    assert isinstance(host, str)
    return server, f"http://{host}:{port}", requests_log


def _string_list(value: object) -> list[str]:
    assert isinstance(value, list)
    result: list[str] = []
    for item in cast(list[object], value):
        assert isinstance(item, str)
        result.append(item)
    return result


def test_same_session_deduplicates_static_definitions_and_mechanisms() -> None:
    cache: JsonObj = {}
    payload = _base_run_payload()
    first = _render_reply(payload, cache)
    second = _render_reply(payload, cache)

    assert "MECHANISMS (new):" in first
    assert "Act Boss(ENCOUNTER.BOSS_A)" in first
    assert "Burning Blood" in first
    assert "MECHANISMS (new):" not in second
    assert "session_calls=2" in second


def test_new_run_detected_via_location_even_at_same_character_act_and_floor() -> None:
    cache: JsonObj = {}
    first_payload = _base_run_payload(run_id="run-1", node_id="node-1", act=1, floor=1)
    _render_reply(first_payload, cache)
    cache["opened_shops"] = ["game-1:run-1:node-1"]
    cache["opened_card_offers"] = ["game-1:run-1:node-1:offer-a"]

    restarted_same_floor = _base_run_payload(run_id="run-2", node_id="node-1", act=1, floor=1)
    rendered = _render_reply(restarted_same_floor, cache)

    assert cache["run_id"] == "run-2"
    assert "opened_shops" not in cache
    assert "opened_card_offers" not in cache
    assert "MECHANISMS (new):" in rendered
    assert "session_calls=1" in rendered


def test_overlapping_card_offers_mark_only_actually_opened_reward() -> None:
    cache: JsonObj = {}
    offers: list[JsonObj] = [
        {
            "ref": "offer-overlay",
            "parent_offer_ref": "offer-a",
            "kind": "card",
            "contents": ["card-shared", "card-a2"],
            "actions": ["act-overlay"],
            "requires_selection": True,
        },
        {
            "ref": "offer-a",
            "kind": "card",
            "contents": ["card-shared", "card-a2"],
            "actions": ["act-a"],
            "requires_selection": True,
        },
        {
            "ref": "offer-b",
            "kind": "card",
            "contents": ["card-shared", "card-b2"],
            "actions": ["act-b"],
            "requires_selection": True,
        },
    ]
    selection: JsonObj = {
        "prompt": "Choose a card",
        "source": None,
        "min": 0,
        "max": 1,
        "candidates": ["card-shared", "card-a2"],
        "selected": [],
        "offer_ref": "offer-a",
    }
    payload = _base_run_payload(offers=offers, selection=selection)
    _record_guards(payload, cache)
    opened = set(_string_list(cache["opened_card_offers"]))
    assert "game-1:run-1:node-1:offer-a" in opened
    assert "game-1:run-1:node-1:offer-b" not in opened

    # Fallback without offer_ref uses strict equality rather than arbitrary intersection.
    cache_fallback: JsonObj = {}
    selection_no_ref = {k: v for k, v in selection.items() if k != "offer_ref"}
    offers_no_parent = [dict(offers[1]), dict(offers[2])]
    payload_fallback = _base_run_payload(offers=offers_no_parent, selection=selection_no_ref)
    _record_guards(payload_fallback, cache_fallback)
    opened_fallback = set(_string_list(cache_fallback["opened_card_offers"]))
    assert "game-1:run-1:node-1:offer-a" in opened_fallback
    assert "game-1:run-1:node-1:offer-b" not in opened_fallback


def test_auto_advance_rejected_preserves_guard_and_unknown_stops_immediately() -> None:
    shop_state = _base_run_payload(
        room_type="shop",
        screen="shop",
        offers=[],
        actions=[
            {"ref": "open", "label": "Open Shop", "enabled": True, "operation": "open_shop"},
            {"ref": "leave", "label": "Leave", "enabled": True, "operation": "leave_shop"},
        ],
    )
    rejected_reply = _base_run_payload(
        room_type="shop",
        screen="shop",
        outcome={"status": "rejected", "reason": {"code": "busy", "message": "Busy"}},
    )
    server, url, requests_log = _start_fake_server([rejected_reply])
    try:
        cache: JsonObj = {}
        final, log = _auto_advance(
            url,
            shop_state,
            cache,
            allow_stateful_auto=True,
            skip_card_open=False,
        )
        outcome = cast(JsonObj, final["outcome"])
        assert outcome["status"] == "rejected"
        assert len(requests_log) == 1
        assert cache["opened_shops"] == []
        assert any("auto stopped" in entry and "rejected" in entry for entry in log)
    finally:
        server.shutdown()

    unknown_reply: JsonObj = {
        "outcome": {
            "status": "unknown",
            "confirmed_steps": 0,
            "uncertain_step": 1,
            "reason": {"code": "execution_uncertain", "message": "Uncertain"},
        },
        "state": None,
    }
    server2, url2, requests_log2 = _start_fake_server([unknown_reply, shop_state])
    try:
        cache2: JsonObj = {}
        final2, log2 = _auto_advance(
            url2,
            shop_state,
            cache2,
            allow_stateful_auto=True,
            skip_card_open=False,
        )
        outcome2 = cast(JsonObj, final2["outcome"])
        assert outcome2["status"] == "unknown"
        assert len(requests_log2) == 1
        assert any("unknown" in entry for entry in log2)
    finally:
        server2.shutdown()


def test_battle_reward_auto_respects_potion_slots_and_unambiguous_relics() -> None:
    two_potions: list[JsonObj] = [
        {
            "ref": "offer-p1",
            "kind": "potion",
            "contents": ["pot-1"],
            "actions": ["act-p1"],
        },
        {
            "ref": "offer-p2",
            "kind": "potion",
            "contents": ["pot-2"],
            "actions": ["act-p2"],
        },
    ]
    potion_actions: list[JsonObj] = [
        {"ref": "act-p1", "label": "Potion 1", "enabled": True, "subject": "offer-p1"},
        {"ref": "act-p2", "label": "Potion 2", "enabled": True, "subject": "offer-p2"},
    ]
    tight_slots = _base_run_payload(
        empty_potions=1,
        offers=two_potions,
        actions=potion_actions,
    )
    assert (
        _next_auto(
            tight_slots,
            opened_shops=set(),
            opened_card_offers=set(),
            allow_stateful_auto=True,
            skip_card_open=False,
        )
        is None
    )

    enough_slots = _base_run_payload(
        empty_potions=2,
        offers=two_potions,
        actions=potion_actions,
    )
    step = _next_auto(
        enough_slots,
        opened_shops=set(),
        opened_card_offers=set(),
        allow_stateful_auto=True,
        skip_card_open=False,
    )
    assert step is not None
    assert step[0] == {"kind": "interact", "action": "act-p1"}

    # Relic with requires_selection=True is left to Run; unambiguous relic before card is claimed.
    selective_relic = _base_run_payload(
        offers=[
            {
                "ref": "offer-rel",
                "kind": "relic",
                "contents": ["rel-new"],
                "actions": ["act-rel"],
                "requires_selection": True,
            }
        ],
        actions=[{"ref": "act-rel", "label": "Relic", "enabled": True, "subject": "offer-rel"}],
    )
    assert (
        _next_auto(
            selective_relic,
            opened_shops=set(),
            opened_card_offers=set(),
            allow_stateful_auto=True,
            skip_card_open=False,
        )
        is None
    )

    relic_and_card = _base_run_payload(
        offers=[
            {
                "ref": "offer-card",
                "kind": "card",
                "contents": ["card-1"],
                "actions": ["act-card"],
                "requires_selection": True,
            },
            {
                "ref": "offer-rel",
                "kind": "relic",
                "contents": ["rel-new"],
                "actions": ["act-rel"],
            },
        ],
        actions=[
            {"ref": "act-card", "label": "Card", "enabled": True, "subject": "offer-card"},
            {"ref": "act-rel", "label": "Relic", "enabled": True, "subject": "offer-rel"},
        ],
    )
    relic_step = _next_auto(
        relic_and_card,
        opened_shops=set(),
        opened_card_offers=set(),
        allow_stateful_auto=True,
        skip_card_open=False,
    )
    assert relic_step is not None
    assert relic_step[0] == {"kind": "interact", "action": "act-rel"}


def test_shop_auto_skips_foul_potion_when_ectoplasm_owned() -> None:
    payload = _base_run_payload(
        room_type="shop",
        screen="shop",
        offers=[],
        actions=[
            {"ref": "open", "label": "Open Shop", "enabled": True, "operation": "open_shop"},
            {"ref": "leave", "label": "Leave", "enabled": True, "operation": "leave_shop"},
        ],
        objects={
            "relic-ecto": {
                "kind": "relic",
                "definition_id": "RELIC.ECTOPLASM",
                "name": "Ectoplasm",
                "status": "normal",
                "is_used_up": False,
                "is_melted": False,
                "stack_count": 1,
            },
            "pot-foul": {
                "kind": "potion",
                "definition_id": "POTION.FOUL_POTION",
                "name": "Foul Potion",
                "usable": True,
                "needs_target": False,
                "target_type": "none",
                "usage": "any_time",
            },
        },
    )
    state = cast(JsonObj, payload["state"])
    state["relics"] = ["relic-ecto"]
    state["potions"] = ["pot-foul"]

    step = _next_auto(
        payload,
        opened_shops=set(),
        opened_card_offers=set(),
        allow_stateful_auto=True,
        skip_card_open=False,
    )
    assert step is not None
    assert step[0] == {"kind": "interact", "action": "open"}


def test_rotation_hint_at_24_calls_and_handoff_trim_and_mechanism_completeness() -> None:
    cache: JsonObj = {"calls": 23}
    combat = _combat_payload(
        mechanisms=[
            {
                "kind": "other",
                "definition_id": "MONSTER.CULTIST",
                "name": "Cultist",
                "text": "First move: INCANTATION",
                "owner": "enemy-1",
                "completeness": "partial",
            },
            {
                "kind": "other",
                "definition_id": "MONSTER.MYSTERY",
                "name": "Mystery",
                "text": None,
                "owner": "enemy-2",
                "completeness": "unavailable",
            },
        ]
    )
    rendered = _render_reply(combat, cache, handoff_trim=True)

    assert "session_calls=24" in rendered
    assert "ROTATE_RECOMMENDED" in rendered
    assert "Cultist" in rendered
    assert "65/80" in rendered
    assert "Strike" in rendered
    assert "[partial]" in rendered
    assert "[unavailable]: (rules unavailable)" in rendered


@pytest.mark.parametrize("status", ["completed"])
def test_cmd_call_roundtrip_against_fake_http_server(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    payload = _base_run_payload(outcome={"status": status})
    server, url, requests_log = _start_fake_server([payload])

    def cache_path(session: str) -> Path:
        return tmp_path / f"{session}.json"

    monkeypatch.setattr(_MOD, "_cache_path", cache_path)
    try:
        rc = _cmd_call(
            url, "run_inspect", '{"area":"state"}', session="unit", full=False, raw=False
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "OUTCOME: status=completed" in out
        assert requests_log == [("/tools/run_inspect", {"area": "state"})]
    finally:
        server.shutdown()


@pytest.mark.parametrize("auto_on_skip", [False, True])
def test_cmd_call_new_run_after_raw_preserves_skipped_card_guard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    auto_on_skip: bool,
) -> None:
    def cache_path(session: str) -> Path:
        return tmp_path / f"{session}.json"

    monkeypatch.setattr(_MOD, "_cache_path", cache_path)

    # Seed existing session cache from an older run at identical act/floor/character.
    old_cache: JsonObj = {
        "game_instance_id": "game-1",
        "run_id": "run-1",
        "last_act": 1,
        "last_floor": 2,
        "player_definition_id": "CHARACTER.IRONCLAD",
        "was_terminal": False,
        "calls": 7,
        "defs": {"CARD.STRIKE_IRONCLAD": "Deal 6 damage."},
        "opened_card_offers": ["game-1:run-1:node-old:offer-old"],
    }
    cache_path("shared").write_text(json.dumps(old_cache), encoding="utf-8")

    card_offer = {
        "ref": "offer-card-1",
        "kind": "card",
        "requires_selection": True,
        "contents": [],
        "actions": ["interaction:1:12"],
    }
    card_action = {
        "ref": "interaction:1:12",
        "label": "Choose a Card",
        "enabled": True,
        "subject": "offer-card-1",
    }
    new_run_state = _base_run_payload(
        run_id="run-2",
        node_id="node-new",
        offers=[card_offer],
        actions=[card_action],
    )
    # Sanity-check that without the skip guard, auto-advance would open this card reward.
    unguarded_step = _next_auto(
        new_run_state,
        opened_shops=set(),
        opened_card_offers=set(),
        allow_stateful_auto=True,
        skip_card_open=False,
    )
    assert unguarded_step is not None
    assert unguarded_step[0] == {"kind": "interact", "action": "interaction:1:12"}
    server, url, requests_log = _start_fake_server(
        [
            new_run_state,  # 1. --raw inspect (does not touch cache)
            new_run_state,  # 2. run_act open + skip card reward
            new_run_state,  # 3. follow-up run_inspect --auto
        ]
    )
    try:
        # Step 1: raw inspect on new run leaves old session cache untouched.
        rc_raw = _cmd_call(
            url, "run_inspect", '{"area":"state"}', session="shared", full=False, raw=True
        )
        assert rc_raw == 0
        capsys.readouterr()

        # Step 2: first normal response on new run opens & skips the card reward.
        skip_args = json.dumps(
            {
                "steps": [
                    {"kind": "interact", "action": "interaction:1:12"},
                    {"kind": "select", "refs": []},
                ]
            }
        )
        rc_skip = _cmd_call(
            url,
            "run_act",
            skip_args,
            session="shared",
            full=False,
            raw=False,
            auto=auto_on_skip,
        )
        assert rc_skip == 0
        skip_out = capsys.readouterr().out
        assert "session_calls=1" in skip_out
        assert "Deal 6 damage." in skip_out

        saved = cast(JsonObj, json.loads(cache_path("shared").read_text(encoding="utf-8")))
        assert saved["run_id"] == "run-2"
        assert saved["opened_card_offers"] == ["game-1:run-2:node-new:offer-card-1"]

        # Step 3: next auto inspect must NOT re-open the skipped card offer.
        rc_auto = _cmd_call(
            url,
            "run_inspect",
            '{"area":"state"}',
            session="shared",
            full=False,
            raw=False,
            auto=True,
        )
        assert rc_auto == 0
        capsys.readouterr()
        assert [path for path, _ in requests_log] == [
            "/tools/run_inspect",
            "/tools/run_act",
            "/tools/run_inspect",
        ]
    finally:
        server.shutdown()
