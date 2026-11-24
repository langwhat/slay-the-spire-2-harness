#!/usr/bin/env python3
"""Compact, session-deduplicated CLI client for the STS2 Harness external HTTP backend.

Uses only the Python 3 standard library. Emits concise factual views of RawResult
responses while preserving actionable refs, dynamic numbers, previews, modifiers,
and changes. Pass --session <id> to deduplicate unchanged rules, descriptions,
relics, potions, non-hand combat piles, deck entries, and act map topology within
a conversation; pass --full to print a full compact snapshot and reset the delta
baseline for that session.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import cast

DEFAULT_URL = os.environ.get("STS2_TOOL_URL", "http://127.0.0.1:17878")
ROTATE_CALL_THRESHOLD = 24
JsonObj = dict[str, object]
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_dict(value: object) -> JsonObj:
    if not isinstance(value, dict):
        return {}
    raw = cast(dict[object, object], value)
    return {str(k): v for k, v in raw.items()}


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return list(cast(list[object], value))


def _hash(obj: object) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _one_line(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def _cache_path(session: str) -> Path:
    # Use raw_ only for lowercase ASCII so case-insensitive filesystems (macOS APFS)
    # cannot alias distinct names like "Game" and "game".
    if re.fullmatch(r"[a-z0-9._-]{1,64}", session):
        return Path(tempfile.gettempdir()) / f"sts2_cli_session_raw_{session}.json"
    safe = re.sub(r"[^a-z0-9._-]", "_", session.lower())[:48]
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"sts2_cli_session_enc_{safe}_{digest}.json"


def _load_cache(session: str | None) -> JsonObj:
    if not session:
        return {}
    path = _cache_path(session)
    if not path.exists():
        return {}
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        return _as_dict(loaded)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(session: str | None, cache: JsonObj) -> None:
    if not session:
        return
    path = _cache_path(session)
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _send_request(req: urllib.request.Request, *, timeout: int = 300) -> tuple[int, str]:
    host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
    opener = _NO_PROXY_OPENER if host in ("127.0.0.1", "localhost", "::1") else None
    try:
        resp = (
            opener.open(req, timeout=timeout)
            if opener is not None
            else urllib.request.urlopen(req, timeout=timeout)
        )
        with resp:
            status = _int(getattr(resp, "status", 200), 200)
            return status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as err:
        detail = getattr(err, "reason", err)
        return 0, f"Connection error: {detail}"


def _http_get(url: str) -> tuple[int, str]:
    return _send_request(urllib.request.Request(url, method="GET"))


def _http_post(url: str, body: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _send_request(req)


def _fmt_cost(cost: object) -> str:
    d = _as_dict(cost)
    if not d:
        return ""
    parts = [f"{k}={v}" for k, v in d.items() if not (k != "energy" and v == "none")]
    if not parts:
        parts = [f"{k}={v}" for k, v in d.items()]
    return ",".join(parts)


def _fmt_powers(powers: object) -> str:
    items = _as_list(powers)
    if not items:
        return ""
    out: list[str] = []
    for raw in items:
        p = _as_dict(raw)
        name = _str(p.get("name"))
        did = _str(p.get("definition_id"))
        disp = p.get("display_amount")
        amt = p.get("amount")
        if disp is not None:
            if "amount" in p and amt != disp:
                suffix = f"={disp}(amt={amt})"
            else:
                suffix = f"={disp}"
        elif "amount" in p:
            suffix = f"={amt}"
        else:
            suffix = ""
        out.append(f"{name}({did}){suffix}")
    return ", ".join(out)


def _fmt_intents(intents: object) -> str:
    items = _as_list(intents)
    if not items:
        return "none"
    out: list[str] = []
    for raw in items:
        it = _as_dict(raw)
        t = _str(it.get("type"))
        if t == "unknown":
            out.append("unknown")
            continue
        dmg = it.get("damage_per_hit")
        hits = it.get("hits")
        amt = it.get("amount")
        desc = _one_line(it.get("description"))
        pieces = [t]
        if dmg is not None:
            if "hits" in it and hits is None:
                pieces.append(f"{dmg}x?")
            elif hits is not None and hits != 1:
                pieces.append(f"{dmg}x{hits}")
            else:
                pieces.append(f"{dmg}")
        elif amt is not None:
            pieces.append(f"amt={amt}")
        if desc:
            pieces.append(f'"{desc}"')
        out.append(" ".join(pieces))
    return "; ".join(out)


def _fmt_modifier(label: str, mod: object, mod_text: object) -> str:
    m = _as_dict(mod)
    if not m:
        return ""
    name = _str(m.get("name"))
    did = _str(m.get("definition_id"))
    amt = m.get("amount")
    status = m.get("status")
    parts = [f"{label}:{name}({did})"]
    if amt is not None:
        parts.append(f"amt={amt}")
    if status is not None:
        parts.append(f"status={status}")
    txt = _one_line(mod_text)
    if txt:
        parts.append(f"[{txt}]")
    return " ".join(parts)


def _modifier_sig(mod: object) -> str:
    m = _as_dict(mod)
    if not m:
        return ""
    return f"{m.get('definition_id')}:{m.get('amount')}:{m.get('status')}"


def _card_key(entry: JsonObj, desc: object = None) -> str:
    did = _str(entry.get("definition_id"))
    up = entry.get("upgrades")
    key = did
    if isinstance(up, int) and up > 0:
        key += f"+{up}"
    ench = _modifier_sig(entry.get("enchantment"))
    aff = _modifier_sig(entry.get("affliction"))
    if ench:
        key += f"|E:{ench}"
    if aff:
        key += f"|A:{aff}"
    cost_s = _fmt_cost(entry.get("cost"))
    if cost_s:
        key += f"|C:{cost_s}"
    txt = _one_line(desc)
    if txt:
        key += f"|T:{_hash(txt)[:8]}"
    return key


def _card_def_key(entry: JsonObj) -> str:
    did = _str(entry.get("definition_id"))
    up = entry.get("upgrades")
    key = did
    if isinstance(up, int) and up > 0:
        key += f"+{up}"
    ench = _modifier_sig(entry.get("enchantment"))
    aff = _modifier_sig(entry.get("affliction"))
    if ench:
        key += f"|E:{ench}"
    if aff:
        key += f"|A:{aff}"
    return key


def _fmt_upgrade_entry(up: object) -> str:
    u = _as_dict(up)
    status = _str(u.get("status"))
    if status != "available":
        return f"upgrade={status}" if status else ""
    c = _as_dict(u.get("card"))
    name = _str(c.get("name"))
    cost = _fmt_cost(c.get("cost"))
    kws = ",".join(_str(k) for k in _as_list(c.get("keywords")))
    txt = _one_line(c.get("text"))
    parts = [f"upgrade->{name}"]
    if cost:
        parts.append(f"cost({cost})")
    if kws:
        parts.append(f"kw({kws})")
    if txt:
        parts.append(f'"{txt}"')
    return " ".join(parts)


def _fmt_card_line(
    ref: str,
    entry: JsonObj,
    desc: object,
    mod_desc: JsonObj,
    upgrade: object,
    seen_defs: JsonObj,
    *,
    show_upgrade: bool = False,
    force_text: bool = False,
) -> str:
    name = _str(entry.get("name"))
    did = _str(entry.get("definition_id"))
    up = entry.get("upgrades")
    up_tag = f"+{up}" if isinstance(up, int) and up > 0 else ""
    ctype = _str(entry.get("card_type"))
    rarity = _str(entry.get("rarity"))
    cost = _fmt_cost(entry.get("cost"))
    base_cost = _fmt_cost(entry.get("cost_without_global_effects"))
    playable = entry.get("playable")

    parts = [f"{ref} | {name}{up_tag} ({did})"]
    meta: list[str] = []
    if ctype:
        meta.append(ctype)
    if rarity and rarity not in ("basic", "common"):
        meta.append(rarity)
    if cost:
        meta.append(f"cost({cost})")
    if base_cost and base_cost != cost:
        meta.append(f"local_cost({base_cost})")
    if playable is False:
        meta.append("UNPLAYABLE")
    elif playable is None and "playable" in entry:
        meta.append("playable=unknown")
    if entry.get("should_retain_this_turn"):
        meta.append("RETAIN_THIS_TURN")
    if entry.get("exhaust_on_next_play"):
        meta.append("EXHAUST_NEXT")
    if entry.get("has_turn_end_in_hand_effect"):
        meta.append("TURN_END_EFFECT")
    if entry.get("is_upgradable") is False and show_upgrade:
        meta.append("not_upgradable")
    if entry.get("is_removable") is False:
        meta.append("unremovable")
    kws = _as_list(entry.get("keywords"))
    if kws:
        meta.append("kw(" + ",".join(_str(k) for k in kws) + ")")
    lkws_raw = entry.get("local_keywords")
    if isinstance(lkws_raw, list):
        lkws = _as_list(cast(object, lkws_raw))
        if lkws != kws:
            meta.append("local_kw(" + ",".join(_str(k) for k in lkws) + ")")
    if meta:
        parts.append("[" + " ".join(meta) + "]")

    ench_s = _fmt_modifier("ench", entry.get("enchantment"), mod_desc.get("enchantment"))
    aff_s = _fmt_modifier("aff", entry.get("affliction"), mod_desc.get("affliction"))
    if ench_s:
        parts.append(ench_s)
    if aff_s:
        parts.append(aff_s)

    txt = _one_line(desc)
    dkey = _card_def_key(entry)
    if txt:
        if force_text or seen_defs.get(dkey) != txt:
            parts.append(f'"{txt}"')
            seen_defs[dkey] = txt
    elif desc is None:
        parts.append("text=null")

    previews = _as_list(entry.get("previews"))
    if previews:
        prev_strs: list[str] = []
        for pr in previews:
            pd = _as_dict(pr)
            ptxt = _one_line(pd.get("text"))
            if ptxt != txt:
                prev_strs.append(f'{pd.get("target")}->"{ptxt}"')
        if prev_strs:
            parts.append("previews{" + "; ".join(prev_strs) + "}")

    if show_upgrade and upgrade is not None:
        us = _fmt_upgrade_entry(upgrade)
        if us:
            parts.append("| " + us)

    return " ".join(parts)


def _fmt_hp(obj: JsonObj) -> str:
    hpd = obj.get("hp_display")
    hp = obj.get("hp")
    max_hp = obj.get("max_hp")
    if hpd in (None, "numeric"):
        return f"{hp}/{max_hp}"
    if hp is not None and max_hp is not None:
        return f"{hp}/{max_hp}({hpd})"
    return f"{hpd}"


def _fmt_enemy_line(en: JsonObj) -> str:
    epow = _fmt_powers(en.get("powers"))
    ep_s = f" powers=[{epow}]" if epow else ""
    nm = en.get("next_move_id")
    nm_s = f" next_move={nm}" if isinstance(nm, str) and nm else ""
    hp_s = _fmt_hp(en)
    return (
        f"  {en.get('ref')} | {en.get('name')} ({en.get('definition_id')}) "
        f"HP={hp_s} blk={en.get('block')}{nm_s} intent=[{_fmt_intents(en.get('intents'))}]{ep_s}"
    )


def _fmt_item_or_bundle(
    cref: str,
    objects: JsonObj,
    descriptions: JsonObj,
    mod_descriptions: JsonObj,
    upgrades: JsonObj,
    seen_defs: JsonObj,
    *,
    indent: str,
) -> list[str]:
    ce = _as_dict(objects.get(cref))
    kind = _str(ce.get("kind"))
    if kind == "card":
        return [
            indent
            + _fmt_card_line(
                cref,
                ce,
                descriptions.get(cref),
                _as_dict(mod_descriptions.get(cref)),
                upgrades.get(cref),
                seen_defs,
                show_upgrade=True,
                force_text=True,
            )
        ]
    if kind == "bundle":
        members = [_str(m) for m in _as_list(ce.get("members"))]
        out = [f"{indent}{cref} | bundle {ce.get('name')} members=[{','.join(members)}]"]
        for mref in members:
            out.extend(
                _fmt_item_or_bundle(
                    mref,
                    objects,
                    descriptions,
                    mod_descriptions,
                    upgrades,
                    seen_defs,
                    indent=indent + "  ",
                )
            )
        return out
    cdesc = _one_line(descriptions.get(cref))
    did = f" ({ce.get('definition_id')})" if ce.get("definition_id") else ""
    return [f"{indent}{cref} | {kind} {ce.get('name')}{did}" + (f' "{cdesc}"' if cdesc else "")]


_ODDS_LABELS: dict[str, str] = {
    "monster": "mon",
    "elite": "elite",
    "event": "event",
    "shop": "shop",
    "treasure": "treas",
}


def _fmt_odds(odds: JsonObj) -> str:
    return "{" + ",".join(f"{_ODDS_LABELS.get(k, k)}:{v}" for k, v in odds.items()) + "}"


def _fmt_map(m: JsonObj) -> list[str]:
    lines: list[str] = []
    bosses = [
        f"{_as_dict(b).get('name')}({_as_dict(b).get('definition_id')})"
        for b in _as_list(m.get("bosses"))
    ]
    elites = [
        f"{_as_dict(e).get('name')}({_as_dict(e).get('definition_id')})"
        for e in _as_list(m.get("elite_pool"))
    ]
    elite_s = f" elite_pool=[{', '.join(elites)}]" if elites else ""
    nodes = [_as_dict(raw) for raw in _as_list(m.get("nodes"))]
    unique_odds = {_fmt_odds(_as_dict(n.get("odds"))) for n in nodes if _as_dict(n.get("odds"))}
    shared_odds = next(iter(unique_odds)) if len(unique_odds) == 1 else ""
    odds_hdr = f" unknown_odds={shared_odds}" if shared_odds else ""
    legal_s = json.dumps(m.get("legal_destinations"), ensure_ascii=False)
    lines.append(
        f"Map: bosses=[{', '.join(bosses)}]{elite_s}{odds_hdr} legal_destinations={legal_s}"
    )
    if nodes:
        node_strs: list[str] = []
        for n in nodes:
            pos = _str(n.get("position"))
            rt = _str(n.get("room_type"))
            ch = ",".join(_str(c) for c in _as_list(n.get("children")))
            odds = _as_dict(n.get("odds"))
            odds_s = _fmt_odds(odds) if odds and not shared_odds else ""
            node_strs.append(f"{pos}:{rt}{odds_s}->[{ch}]")
        lines.append("  nodes: " + " | ".join(node_strs))
    return lines


def _node_key(state: JsonObj) -> str:
    prog = _as_dict(state.get("progress"))
    return f"{prog.get('act')}:{prog.get('floor')}:{prog.get('position')}"


def _append_guard(items: list[str], key: str, *, limit: int = 32) -> list[str]:
    out = [x for x in items if x != key]
    out.append(key)
    return out[-limit:]


def _reset_session_if_new_run(payload: JsonObj, cache: JsonObj) -> bool:
    """Reset cache when a new run begins (act/floor drop, character change, or post-terminal)."""
    state = _as_dict(payload.get("state"))
    if not state:
        return False
    phase = _str(state.get("phase"))
    prog = _as_dict(state.get("progress"))
    act = _int(prog.get("act"), -1)
    floor = _int(prog.get("floor"), -1)
    player = _as_dict(state.get("player"))
    player_did = _str(player.get("definition_id"))

    last_act = _int(cache.get("last_act"), -1)
    last_floor = _int(cache.get("last_floor"), -1)
    last_player = _str(cache.get("player_definition_id"))
    was_terminal = cache.get("was_terminal") is True

    is_new_run = (
        (was_terminal and phase != "terminal")
        or (bool(last_player) and bool(player_did) and player_did != last_player)
        or (act >= 0 and last_act >= 0 and act < last_act)
        or (floor >= 0 and last_floor >= 0 and floor < last_floor)
    )
    if is_new_run:
        cache.clear()

    if act >= 0:
        cache["last_act"] = act
    if floor >= 0:
        cache["last_floor"] = floor
    if player_did:
        cache["player_definition_id"] = player_did
    cache["was_terminal"] = phase == "terminal"
    return is_new_run


def _record_observed_auto_guards(
    payload: JsonObj,
    cache: JsonObj,
    skipped_card_actions: set[str] | None = None,
) -> None:
    """Track visited shop entrances and opened/skipped card reward offers across calls."""
    state = _as_dict(payload.get("state"))
    if state.get("phase") != "run":
        return

    nkey = _node_key(state)
    actions = [_as_dict(a) for a in _as_list(state.get("actions"))]
    enabled = [a for a in actions if a.get("enabled", True)]
    enabled_ops = {_str(a.get("operation")) for a in enabled if a.get("operation") is not None}
    offers = [_as_dict(o) for o in _as_list(state.get("offers"))]

    # If the shop panel is currently open (close_shop enabled or shop offers shown), mark visited.
    if "close_shop" in enabled_ops or (state.get("screen") == "shop" and offers):
        opened_shops = [_str(x) for x in _as_list(cache.get("opened_shops"))]
        cache["opened_shops"] = _append_guard(opened_shops, nkey)

    # If a batched [interact(openA), select([])] skipped card offer A without ever showing the
    # intermediate selection screen to CLI, mark any card offer owning those interact action refs.
    if state.get("screen") == "rewards" and skipped_card_actions:
        opened_cards = [_str(x) for x in _as_list(cache.get("opened_card_offers"))]
        for of in offers:
            if of.get("kind") != "card":
                continue
            of_acts = {_str(a) for a in _as_list(of.get("actions")) if a is not None}
            if of_acts & skipped_card_actions:
                opened_cards = _append_guard(opened_cards, f"{nkey}:{_str(of.get('ref'))}")
        cache["opened_card_offers"] = opened_cards

    # If a rewards selection is currently open, mark only card offers matching the active
    # selection's candidate refs so unopened Prayer Wheel card offers (whose contents are
    # pre-populated before opening) stay auto-openable.
    selection = _as_dict(state.get("selection"))
    if state.get("screen") == "rewards" and selection:
        opened_cards = [_str(x) for x in _as_list(cache.get("opened_card_offers"))]
        card_offers = [of for of in offers if of.get("kind") == "card"]
        cand_set = {_str(c) for c in _as_list(selection.get("candidates")) if c is not None}
        matched = False
        if cand_set:
            for of in card_offers:
                of_contents = {_str(c) for c in _as_list(of.get("contents")) if c is not None}
                if of_contents and (of_contents == cand_set or bool(of_contents & cand_set)):
                    opened_cards = _append_guard(opened_cards, f"{nkey}:{_str(of.get('ref'))}")
                    matched = True
        if not matched and len(card_offers) == 1:
            opened_cards = _append_guard(opened_cards, f"{nkey}:{_str(card_offers[0].get('ref'))}")
        cache["opened_card_offers"] = opened_cards


def _next_auto_step(
    payload: JsonObj,
    *,
    opened_shops: set[str],
    opened_card_offers: set[str],
    allow_stateful_auto: bool,
    skip_card_open: bool,
) -> tuple[JsonObj, str, tuple[str, str] | None] | None:
    """Return one strictly mechanical, zero-branch Run step, or None if a decision is needed."""
    outcome = _as_dict(payload.get("outcome"))
    if outcome.get("status") != "completed":
        return None
    state = _as_dict(payload.get("state"))
    if (
        state.get("phase") != "run"
        or state.get("control") != "ready"
        or state.get("selection") is not None
    ):
        return None
    screen = _str(state.get("screen"))
    prog = _as_dict(state.get("progress"))
    room_type = _str(prog.get("room_type"))
    nkey = _node_key(state)
    player = _as_dict(state.get("player"))
    empty_potions = _int(player.get("empty_potion_slot_count"))
    objects = _as_dict(payload.get("objects"))
    actions = [_as_dict(a) for a in _as_list(state.get("actions"))]
    enabled = [a for a in actions if a.get("enabled", True)]
    enabled_by_ref = {_str(a.get("ref")): a for a in enabled}
    offers = [_as_dict(o) for o in _as_list(state.get("offers"))]

    # 1. Battle rewards: claim gold (unless a relic is pending ahead of it), fitting
    # potions, relics (only when no card offer is pending), or open sole card offer.
    if screen == "rewards" and room_type in ("monster", "elite", "boss") and offers:
        potion_offers = [of for of in offers if of.get("kind") == "potion"]
        has_card_offer = any(of.get("kind") == "card" for of in offers)
        has_relic_offer = any(of.get("kind") == "relic" for of in offers)
        if has_card_offer:
            claim_kinds = [] if has_relic_offer else ["gold"]
        else:
            claim_kinds = ["relic", "gold"]
        if 0 < len(potion_offers) <= empty_potions:
            claim_kinds.append("potion")
        for kind in claim_kinds:
            for of in offers:
                if of.get("kind") == kind:
                    for aref in _as_list(of.get("actions")):
                        act = enabled_by_ref.get(_str(aref))
                        if act is not None:
                            return (
                                {"kind": "interact", "action": _str(act.get("ref"))},
                                f"claim {kind}: {_str(act.get('label'))}",
                                None,
                            )
        if (
            allow_stateful_auto
            and not skip_card_open
            and len(offers) == 1
            and offers[0].get("kind") == "card"
            and offers[0].get("requires_selection") is True
        ):
            of = offers[0]
            of_actions = _as_list(of.get("actions"))
            oref = f"{nkey}:{_str(of.get('ref'))}"
            if len(of_actions) == 1 and oref not in opened_card_offers:
                act = enabled_by_ref.get(_str(of_actions[0]))
                if act is not None and act.get("subject") == of.get("ref"):
                    return (
                        {"kind": "interact", "action": _str(act.get("ref"))},
                        f"open card reward: {_str(act.get('label'))}",
                        ("card", oref),
                    )
        return None

    # 2. Event reward potions when all offered potions fit into empty slots.
    if screen == "rewards" and room_type == "event" and offers:
        if 0 < len(offers) <= empty_potions:
            claims = {_str(a.get("ref")): a for a in enabled if a.get("operation") != "proceed"}
            if len(claims) == len(offers) and all(
                of.get("kind") == "potion"
                and len(_as_list(of.get("actions"))) == 1
                and _str(_as_list(of.get("actions"))[0]) in claims
                and claims[_str(_as_list(of.get("actions"))[0])].get("subject") == of.get("ref")
                for of in offers
            ):
                act = claims[_str(_as_list(offers[0].get("actions"))[0])]
                return (
                    {"kind": "interact", "action": _str(act.get("ref"))},
                    f"claim event potion: {_str(act.get('label'))}",
                    None,
                )
        return None

    if offers:
        return None

    # 3. Unopened real shop entrance (at most once per map node; use POTION.FOUL_POTION
    # first if usable). Require room_type == "shop" so FakeMerchant events never auto-fight.
    all_ops = {_str(a.get("operation")) for a in actions if a.get("operation") is not None}
    enabled_ops = {_str(a.get("operation")) for a in enabled if a.get("operation") is not None}
    shop_ops = {"open_shop", "close_shop", "leave_shop"}
    is_shop_entrance = (
        room_type == "shop"
        and {"open_shop", "leave_shop"} <= all_ops
        and "close_shop" not in enabled_ops
        and all(_str(a.get("operation")) in shop_ops for a in enabled)
    )
    if allow_stateful_auto and nkey not in opened_shops and is_shop_entrance:
        opening = next((a for a in enabled if a.get("operation") == "open_shop"), None)
        if opening is not None:
            has_ectoplasm = any(
                _as_dict(objects.get(_str(rref))).get("definition_id") == "RELIC.ECTOPLASM"
                for rref in _as_list(state.get("relics"))
            )
            if not has_ectoplasm:
                for pref_raw in _as_list(state.get("potions")):
                    pref = _str(pref_raw)
                    pot = _as_dict(objects.get(pref))
                    if (
                        pot.get("kind") == "potion"
                        and pot.get("definition_id") == "POTION.FOUL_POTION"
                        and pot.get("usable") is True
                        and pot.get("needs_target") is False
                    ):
                        return (
                            {"kind": "use_potion", "potion": pref},
                            f"use merchant potion: {_str(pot.get('name'))}",
                            None,
                        )
            return (
                {"kind": "interact", "action": _str(opening.get("ref"))},
                f"open shop: {_str(opening.get('label'))}",
                ("shop", nkey),
            )

    # 4. Single legal map destination.
    if screen == "map":
        dest_acts = [a for a in enabled if a.get("destination") is not None]
        top_map = _as_dict(payload.get("map"))
        legal = _as_list(top_map.get("legal_destinations"))
        if len(legal) == 1 and len(dest_acts) == 1 and dest_acts[0].get("destination") == legal[0]:
            act = dest_acts[0]
            return (
                {"kind": "interact", "action": _str(act.get("ref"))},
                f"travel sole path -> {legal[0]}",
                None,
            )

    # 5. Treasure chest open or sole unambiguous chest relic claim.
    if screen == "treasure":
        chests = [a for a in enabled if a.get("operation") == "open_chest"]
        if len(chests) == 1:
            act = chests[0]
            return (
                {"kind": "interact", "action": _str(act.get("ref"))},
                f"open chest: {_str(act.get('label'))}",
                None,
            )
        if room_type == "treasure":
            avail = [a for a in enabled if a.get("operation") != "proceed"]
            if len(avail) == 1:
                act = avail[0]
                subj = _str(act.get("subject"))
                relic_refs = {_str(r) for r in _as_list(state.get("relics"))}
                if subj and subj not in relic_refs:
                    obj = _as_dict(objects.get(subj))
                    if obj.get("kind") == "relic":
                        return (
                            {"kind": "interact", "action": _str(act.get("ref"))},
                            f"claim chest relic: {_str(obj.get('name'))}",
                            None,
                        )

    # 6. Sole proceed action.
    if len(enabled) == 1 and enabled[0].get("operation") == "proceed":
        act = enabled[0]
        return (
            {"kind": "interact", "action": _str(act.get("ref"))},
            f"proceed: {_str(act.get('label'))}",
            None,
        )

    return None


def _uncertain_auto_reply(message: str) -> JsonObj:
    return {
        "outcome": {
            "status": "unknown",
            "reason": {"code": "execution_unconfirmed", "message": message},
            "suggested_actions": [
                {"message": "Inspect the current state before submitting another action."}
            ],
        },
        "state": None,
    }


def _run_auto_advance(
    url: str,
    payload: JsonObj,
    cache: JsonObj,
    *,
    allow_stateful_auto: bool,
    skip_card_open: bool,
    max_steps: int = 12,
) -> tuple[JsonObj, list[str]]:
    """Advance strictly deterministic mechanical Run steps after a completed call."""
    _reset_session_if_new_run(payload, cache)
    _record_observed_auto_guards(payload, cache)
    opened_shops_list = [_str(x) for x in _as_list(cache.get("opened_shops"))]
    opened_card_offers_list = [_str(x) for x in _as_list(cache.get("opened_card_offers"))]
    log: list[str] = []
    current = payload
    endpoint = f"{url.rstrip('/')}/tools/run_act"

    def _record_guard(guard: tuple[str, str] | None) -> None:
        nonlocal opened_shops_list, opened_card_offers_list
        if guard is None:
            return
        gkind, gkey = guard
        if gkind == "shop":
            opened_shops_list = _append_guard(opened_shops_list, gkey)
        elif gkind == "card":
            opened_card_offers_list = _append_guard(opened_card_offers_list, gkey)

    caller_outcome = _as_dict(payload.get("outcome"))
    caller_completed_steps = caller_outcome.get("completed_steps")

    for _step_idx in range(max_steps):
        nxt = _next_auto_step(
            current,
            opened_shops=set(opened_shops_list),
            opened_card_offers=set(opened_card_offers_list),
            allow_stateful_auto=allow_stateful_auto,
            skip_card_open=skip_card_open,
        )
        if nxt is None:
            break
        step, desc, guard = nxt
        body = json.dumps({"steps": [step], "reason": desc}, ensure_ascii=False)
        code, text = _http_post(endpoint, body)
        if code != 200:
            _record_guard(guard)
            if caller_outcome.get("status") == "completed" and caller_completed_steps is not None:
                log.insert(0, f"caller_completed_steps={caller_completed_steps}")
            log.append(f"uncertain (HTTP {code}) during {desc}")
            current = _uncertain_auto_reply(
                f"Auto-advance step '{desc}' got HTTP {code}; state is unconfirmed."
            )
            break
        try:
            next_payload = _as_dict(json.loads(text))
        except json.JSONDecodeError:
            _record_guard(guard)
            if caller_outcome.get("status") == "completed" and caller_completed_steps is not None:
                log.insert(0, f"caller_completed_steps={caller_completed_steps}")
            log.append(f"uncertain (bad JSON) during {desc}")
            current = _uncertain_auto_reply(
                f"Auto-advance step '{desc}' returned invalid JSON; state is unconfirmed."
            )
            break
        next_outcome = _as_dict(next_payload.get("outcome"))
        out_status = _str(next_outcome.get("status"))
        if out_status != "completed":
            # Only burn a one-shot guard if execution may have occurred; never on rejected.
            if out_status != "rejected":
                _record_guard(guard)
            if caller_outcome.get("status") == "completed" and caller_completed_steps is not None:
                log.insert(0, f"caller_completed_steps={caller_completed_steps}")
            reason_code = _str(_as_dict(next_outcome.get("reason")).get("code"))
            suffix = f" ({reason_code})" if reason_code else ""
            # Propagate the latest payload (including state: null on busy/paused);
            # render_reply reports CALLER_OUTCOME separately so caller receipts stay intact.
            current = next_payload
            log.append(f"auto stopped on {desc}: {out_status}{suffix}")
            break
        _record_guard(guard)
        if next_payload.get("state") == current.get("state"):
            log.append(f"no-progress on {desc}")
            break
        log.append(desc)
        current = next_payload
        _record_observed_auto_guards(current, cache)
        for x in _as_list(cache.get("opened_shops")):
            opened_shops_list = _append_guard(opened_shops_list, _str(x))
        for x in _as_list(cache.get("opened_card_offers")):
            opened_card_offers_list = _append_guard(opened_card_offers_list, _str(x))
    if (
        log
        and current is not payload
        and caller_outcome.get("status") == "completed"
        and caller_completed_steps is not None
        and not log[0].startswith("caller_completed_steps=")
    ):
        log.insert(0, f"caller_completed_steps={caller_completed_steps}")
    cache["opened_shops"] = opened_shops_list[-32:]
    cache["opened_card_offers"] = opened_card_offers_list[-32:]
    return current, log


def render_reply(
    payload: JsonObj,
    cache: JsonObj,
    *,
    full: bool = False,
    tool_name: str = "",
    force_map_topology: bool = False,
    handoff_trim: bool = False,
    auto_log: list[str] | None = None,
    caller_outcome: JsonObj | None = None,
) -> str:
    if _reset_session_if_new_run(payload, cache):
        full = True
    lines: list[str] = []
    calls = _int(cache.get("calls")) + 1
    cache["calls"] = calls

    outcome = _as_dict(payload.get("outcome"))
    if auto_log and caller_outcome and caller_outcome != outcome:
        caller_bits = [f"status={_str(caller_outcome.get('status'))}"]
        for k in ("completed_steps", "next_step", "confirmed_steps", "uncertain_step"):
            if k in caller_outcome and caller_outcome[k] is not None:
                caller_bits.append(f"{k}={caller_outcome[k]}")
        lines.append("CALLER_OUTCOME: " + " ".join(caller_bits))
    if auto_log:
        lines.append("AUTO_ADVANCED: " + " -> ".join(auto_log))

    out_status = _str(outcome.get("status"))
    out_bits = [f"status={out_status}"]
    for k in ("completed_steps", "next_step", "confirmed_steps", "uncertain_step"):
        if k in outcome and outcome[k] is not None:
            out_bits.append(f"{k}={outcome[k]}")
    reason = _as_dict(outcome.get("reason"))
    if reason:
        out_bits.append(f"reason={reason.get('code')}: {_one_line(reason.get('message'))}")
    out_bits.append(f"session_calls={calls}")
    if handoff_trim and calls >= ROTATE_CALL_THRESHOLD:
        out_bits.append("ROTATE_RECOMMENDED")
    lines.append("OUTCOME: " + " ".join(out_bits))
    for sug in _as_list(outcome.get("suggested_actions")):
        msg = _one_line(_as_dict(sug).get("message"))
        if msg:
            lines.append(f"  suggestion: {msg}")

    state = _as_dict(payload.get("state"))
    data = payload.get("data")

    if not state:
        lines.append("STATE: null")
        if data is not None:
            lines.append("DATA: " + json.dumps(data, ensure_ascii=False))
        return "\n".join(lines)

    _record_observed_auto_guards(payload, cache)

    objects = _as_dict(payload.get("objects"))
    descriptions = _as_dict(payload.get("descriptions"))
    mod_descriptions = _as_dict(payload.get("modifier_descriptions"))
    upgrades = _as_dict(payload.get("upgrades"))
    generated = _as_dict(payload.get("generated"))
    mechanisms = _as_list(payload.get("mechanisms"))
    top_map = _as_dict(payload.get("map"))

    nkey = _node_key(state)
    if full:
        seen_defs: JsonObj = {}
        seen_mechs: set[str] = set()
        seen_gen: set[str] = set()
        seen_relics: JsonObj = {}
        last_potions_hash = ""
        last_piles_hash: JsonObj = {}
        last_piles_node = ""
        last_deck_counts: JsonObj = {}
        last_map_topo_hash = ""
    else:
        seen_defs = dict(_as_dict(cache.get("defs")))
        seen_mechs = set(_str(x) for x in _as_list(cache.get("mechs")))
        seen_gen = set(_str(x) for x in _as_list(cache.get("gen")))
        seen_relics = dict(_as_dict(cache.get("relics")))
        last_potions_hash = _str(cache.get("potions_hash"))
        last_piles_hash = dict(_as_dict(cache.get("piles_hash")))
        last_piles_node = _str(cache.get("piles_node"))
        last_deck_counts = dict(_as_dict(cache.get("deck_counts")))
        last_map_topo_hash = _str(cache.get("map_topo_hash"))

    if last_piles_node != nkey:
        last_piles_hash = {}

    phase = _str(state.get("phase"))
    control = _str(state.get("control"))
    screen = _str(state.get("screen"))
    prog = _as_dict(state.get("progress"))
    act_str = f"{prog.get('act')}/{prog.get('acts')}"
    if prog.get("name"):
        act_did = f"({prog.get('definition_id')})" if prog.get("definition_id") else ""
        act_str += f"[{prog.get('name')}{act_did}]"
    hdr = [
        f"phase={phase}",
        f"control={control}",
        f"screen={screen}",
        f"act={act_str}",
        f"floor={prog.get('floor')}",
        f"pos={prog.get('position')}",
        f"room={prog.get('room_type')}",
    ]
    if prog.get("bosses_left") is not None:
        hdr.append(f"bosses_left={prog.get('bosses_left')}")
    if state.get("turn") is not None:
        hdr.append(f"turn={state.get('turn')}")
    if state.get("battle_turns") is not None:
        hdr.append(f"battle_turns={state.get('battle_turns')}")
    if state.get("last_observed_turn") is not None:
        hdr.append(f"last_turn={state.get('last_observed_turn')}")
    if state.get("result") is not None:
        hdr.append(f"result={state.get('result')}")
    if state.get("card_rarity_offset") is not None:
        hdr.append(f"rarity_offset={state.get('card_rarity_offset')}")
    lines.append("STATE: " + " ".join(hdr))

    player = _as_dict(state.get("player"))
    p_bits = [
        f"{player.get('name')}({player.get('definition_id')})",
        f"HP={player.get('hp')}/{player.get('max_hp')}",
        f"gold={player.get('gold')}",
        f"potion_slots={player.get('potion_slot_count')}"
        f"(empty={player.get('empty_potion_slot_count')})",
    ]
    if player.get("block") is not None:
        p_bits.append(f"block={player.get('block')}")
    res = _as_dict(player.get("resources"))
    if res:
        p_bits.append("res(" + ",".join(f"{k}={v}" for k, v in res.items()) + ")")
    if player.get("base_draw") is not None or player.get("base_energy") is not None:
        p_bits.append(
            f"base(draw={player.get('base_draw')},"
            f"energy={player.get('base_energy')},orbs={player.get('base_orb_slots')})"
        )
    ppow = _fmt_powers(player.get("powers"))
    if ppow:
        p_bits.append(f"powers=[{ppow}]")
    lines.append("PLAYER: " + " ".join(p_bits))

    # Potions (deduplicated when refs, usability, targeting, usage, and descriptions are unchanged)
    potions = [_str(r) for r in _as_list(state.get("potions"))]
    pot_sig = _hash(
        [(ref, _as_dict(objects.get(ref)), _one_line(descriptions.get(ref))) for ref in potions]
    )
    if not potions:
        lines.append("POTIONS: none")
    elif not full and pot_sig == last_potions_hash:
        lines.append(f"POTIONS: unchanged ({len(potions)})")
    else:
        lines.append("POTIONS:")
        for ref in potions:
            pe = _as_dict(objects.get(ref))
            name = _str(pe.get("name"))
            did = _str(pe.get("definition_id"))
            flags: list[str] = []
            if pe.get("usable") is not None:
                flags.append(f"usable={pe.get('usable')}")
            if pe.get("needs_target"):
                flags.append(f"target={pe.get('target_type')}")
            if pe.get("usage"):
                flags.append(f"usage={pe.get('usage')}")
            desc = _one_line(descriptions.get(ref))
            dkey = f"POT:{did}"
            txt_part = ""
            if desc and seen_defs.get(dkey) != desc:
                txt_part = f' "{desc}"'
                seen_defs[dkey] = desc
            lines.append(f"  {ref} | {name} ({did}) [{' '.join(flags)}]{txt_part}")
    last_potions_hash = pot_sig

    # Relics (deduplicated; handles duplicates and explicitly names removals/swaps)
    relic_refs = [_str(r) for r in _as_list(state.get("relics"))]
    new_relic_map: JsonObj = {}
    changed_relic_lines: list[str] = []
    did_counts: dict[str, int] = {}
    for ref in relic_refs:
        re_obj = _as_dict(objects.get(ref))
        did = _str(re_obj.get("definition_id")) or ref
        did_counts[did] = did_counts.get(did, 0) + 1
        rkey = f"{did}#{did_counts[did]}"
        desc = _one_line(descriptions.get(ref))
        sig = _hash({"o": re_obj, "d": desc})
        label_id = f"{re_obj.get('name')} ({did})"
        new_relic_map[rkey] = {"sig": sig, "label": label_id}
        prev_entry = _as_dict(seen_relics.get(rkey))
        if full or prev_entry.get("sig") != sig:
            extras: list[str] = []
            relic_fields = (
                "counter",
                "status",
                "stack_count",
                "used_this_combat",
                "is_used_up",
                "is_melted",
            )
            for k in relic_fields:
                val = re_obj.get(k)
                if (
                    val is not None
                    and val is not False
                    and not (k == "stack_count" and val == 1)
                    and not (k == "status" and val == "normal")
                ):
                    extras.append(f"{k}={val}")
            ex_s = f" [{' '.join(extras)}]" if extras else ""
            dkey = f"REL:{did}"
            txt_part = ""
            if desc and seen_defs.get(dkey) != desc:
                txt_part = f' "{desc}"'
                seen_defs[dkey] = desc
            changed_relic_lines.append(f"  {ref} | {label_id}{ex_s}{txt_part}")
    removed_relics = [
        _str(_as_dict(seen_relics.get(rk)).get("label") or rk)
        for rk in seen_relics
        if rk not in new_relic_map
    ]
    if changed_relic_lines or removed_relics:
        label = (
            "RELICS:" if full or not seen_relics else f"RELICS (updated; total {len(relic_refs)}):"
        )
        lines.append(label)
        lines.extend(changed_relic_lines)
        for rem in removed_relics:
            lines.append(f"  -removed: {rem}")
    else:
        lines.append(f"RELICS: unchanged ({len(relic_refs)})")
    seen_relics = new_relic_map

    # Combat sections (keep brief only in handoff mode when a run_* call just entered combat)
    run_entered_combat = (
        handoff_trim and tool_name.startswith("run_") and phase == "combat" and not full
    )
    if phase == "combat" and run_entered_combat:
        en_names = [
            f"{_as_dict(e).get('name')}({_as_dict(e).get('definition_id')})"
            for e in _as_list(state.get("enemies"))
        ]
        lines.append(f"COMBAT_ENTERED: enemies=[{', '.join(en_names)}]")
    elif phase == "combat":
        orbs = _as_dict(state.get("orbs"))
        if orbs:
            slots: list[str] = []
            for raw_o in _as_list(orbs.get("slots")):
                o = _as_dict(raw_o)
                odid = f"({o.get('definition_id')})" if o.get("definition_id") else ""
                slots.append(f"{o.get('name')}{odid}(p={o.get('passive')},e={o.get('evoke')})")
            lines.append(f"ORBS (cap={orbs.get('capacity')}): [{', '.join(slots)}]")
        arrows = _as_list(state.get("arrow_slots"))
        if arrows:
            as_s: list[str] = []
            for a in arrows:
                ad = _as_dict(a)
                st = _str(ad.get("status"))
                imp = ad.get("impaled_target_ref")
                as_s.append(f"{st}({imp})" if imp else st)
            lines.append("ARROWS: [" + ", ".join(as_s) + "]")
        allies = _as_list(state.get("allies"))
        if allies:
            lines.append("ALLIES:")
            for raw in allies:
                al = _as_dict(raw)
                apow = _fmt_powers(al.get("powers"))
                ap_s = f" powers=[{apow}]" if apow else ""
                adid = f" ({al.get('definition_id')})" if al.get("definition_id") else ""
                ahp_s = _fmt_hp(al)
                lines.append(
                    f"  {al.get('ref')} | {al.get('name')}{adid} "
                    f"HP={ahp_s} blk={al.get('block')} alive={al.get('alive')}{ap_s}"
                )

        lines.append("ENEMIES:")
        for raw in _as_list(state.get("enemies")):
            lines.append(_fmt_enemy_line(_as_dict(raw)))
        inactive = _as_list(state.get("inactive_enemies"))
        if inactive:
            lines.append("INACTIVE_ENEMIES:")
            for raw in inactive:
                lines.append(_fmt_enemy_line(_as_dict(raw)))

        piles = _as_dict(state.get("piles"))
        hand = [_str(r) for r in _as_list(piles.get("hand"))]
        lines.append(f"HAND ({len(hand)}):")
        hand_seen: JsonObj = {}
        for ref in hand:
            ce = _as_dict(objects.get(ref))
            lines.append(
                "  "
                + _fmt_card_line(
                    ref,
                    ce,
                    descriptions.get(ref),
                    _as_dict(mod_descriptions.get(ref)),
                    upgrades.get(ref),
                    hand_seen,
                    force_text=True,
                )
            )
        new_piles_hash: JsonObj = {}
        new_pile_defs: list[str] = []
        for pname in ("draw", "discard", "exhaust", "other"):
            prefs = [_str(r) for r in _as_list(piles.get(pname))]
            if not prefs and pname == "other":
                continue
            grouped: dict[str, list[str]] = {}
            sig_items: list[tuple[str, str]] = []
            for ref in prefs:
                ce = _as_dict(objects.get(ref))
                desc_val = descriptions.get(ref)
                m_desc = _as_dict(mod_descriptions.get(ref))
                up = ce.get("upgrades")
                up_s = f"+{up}" if isinstance(up, int) and up > 0 else ""
                cost_s = _fmt_cost(ce.get("cost"))
                label = f"{ce.get('name')}{up_s}"
                if cost_s:
                    label += f"({cost_s})"
                tags: list[str] = []
                ench_short = _fmt_modifier("ench", ce.get("enchantment"), None)
                aff_short = _fmt_modifier("aff", ce.get("affliction"), None)
                if ench_short:
                    tags.append(ench_short)
                if aff_short:
                    tags.append(aff_short)
                pkws = _as_list(ce.get("keywords"))
                if pkws:
                    tags.append("kw(" + ",".join(_str(k) for k in pkws) + ")")
                if tags:
                    label += "[" + " ".join(tags) + "]"
                grouped.setdefault(label, []).append(ref)
                sig_items.append((ref, _card_key(ce, desc_val)))
                dkey = _card_def_key(ce)
                txt = _one_line(desc_val)
                if txt and seen_defs.get(dkey) != txt:
                    seen_defs[dkey] = txt
                    r_meta = [_str(ce.get("card_type"))]
                    if cost_s:
                        r_meta.append(f"cost({cost_s})")
                    if pkws:
                        r_meta.append("kw(" + ",".join(_str(k) for k in pkws) + ")")
                    r_ench = _fmt_modifier("ench", ce.get("enchantment"), m_desc.get("enchantment"))
                    r_aff = _fmt_modifier("aff", ce.get("affliction"), m_desc.get("affliction"))
                    r_mods = " ".join(x for x in (r_ench, r_aff) if x)
                    r_mods_s = f" {r_mods}" if r_mods else ""
                    new_pile_defs.append(
                        f"  {ce.get('name')}{up_s} ({ce.get('definition_id')}) "
                        f'[{" ".join(x for x in r_meta if x)}]{r_mods_s}: "{txt}"'
                    )
            psig = _hash(sig_items)
            new_piles_hash[pname] = psig
            if not full and last_piles_hash.get(pname) == psig:
                lines.append(f"PILE.{pname.upper()}: unchanged ({len(prefs)})")
            else:
                summary = ", ".join(f"{lbl}[{','.join(refs)}]" for lbl, refs in grouped.items())
                lines.append(f"PILE.{pname.upper()} ({len(prefs)}): {summary or 'empty'}")
        if new_pile_defs:
            lines.append("PILE_CARD_RULES (new):")
            lines.extend(new_pile_defs)
        last_piles_hash = new_piles_hash
        last_piles_node = nkey

    # Run sections (keep brief only in handoff mode when a combat_* call just exited battle)
    combat_exit = handoff_trim and tool_name.startswith("combat_") and phase == "run" and not full
    if phase == "run" and not combat_exit:
        ev = _as_dict(state.get("event"))
        if ev:
            ev_ref = _str(ev.get("ref"))
            ev_desc = _one_line(descriptions.get(ev_ref))
            anc = " [ancient]" if ev.get("ancient") else ""
            lines.append(
                f"EVENT: {ev_ref} | {ev.get('name')} ({ev.get('definition_id')}){anc}"
                + (f' "{ev_desc}"' if ev_desc else "")
            )

        offers = _as_list(state.get("offers"))
        if offers:
            lines.append("OFFERS:")
            for raw in offers:
                of = _as_dict(raw)
                oref = _str(of.get("ref"))
                bits = [f"{oref} | kind={of.get('kind')}"]
                if of.get("price") is not None:
                    bits.append(f"price={of.get('price')}")
                if of.get("on_sale"):
                    bits.append("ON_SALE")
                if of.get("stocked") is False:
                    bits.append("SOLD_OUT")
                if of.get("amount") is not None:
                    bits.append(f"amount={of.get('amount')}")
                if of.get("requires_selection"):
                    bits.append("requires_selection")
                oacts = _as_list(of.get("actions"))
                if oacts:
                    bits.append("actions=" + ",".join(_str(a) for a in oacts))
                odesc = _one_line(descriptions.get(oref))
                if odesc:
                    bits.append(f'"{odesc}"')
                lines.append("  " + " ".join(bits))
                for cref_raw in _as_list(of.get("contents")):
                    lines.extend(
                        _fmt_item_or_bundle(
                            _str(cref_raw),
                            objects,
                            descriptions,
                            mod_descriptions,
                            upgrades,
                            seen_defs,
                            indent="    ",
                        )
                    )

        # Permanent deck (full on first sight or --full; delta when changed afterward)
        deck_refs = [_str(r) for r in _as_list(state.get("deck"))]
        grouped_deck: dict[str, list[tuple[str, JsonObj]]] = {}
        for ref in deck_refs:
            ce = _as_dict(objects.get(ref))
            grouped_deck.setdefault(_card_key(ce, descriptions.get(ref)), []).append((ref, ce))
        cur_deck_counts: JsonObj = {}
        for ckey, group in grouped_deck.items():
            _ref0, ce0 = group[0]
            up0 = ce0.get("upgrades")
            up0_s = f"+{up0}" if isinstance(up0, int) and up0 > 0 else ""
            cost0_s = _fmt_cost(ce0.get("cost"))
            dlabel = f"{ce0.get('name')}{up0_s} ({ce0.get('definition_id')})"
            if cost0_s:
                dlabel += f" [cost({cost0_s})]"
            d_ench = _fmt_modifier("ench", ce0.get("enchantment"), None)
            d_aff = _fmt_modifier("aff", ce0.get("affliction"), None)
            d_mods = " ".join(x for x in (d_ench, d_aff) if x)
            if d_mods:
                dlabel += f" {d_mods}"
            cur_deck_counts[ckey] = {"count": len(group), "label": dlabel}

        def _deck_entry_count(val: object) -> int:
            entry = _as_dict(val)
            if entry:
                return _int(entry.get("count"))
            return _int(val)

        def _deck_entry_label(ckey: str, val: object) -> str:
            entry = _as_dict(val)
            lbl = _str(entry.get("label"))
            return lbl if lbl else ckey

        deck_same = (
            bool(last_deck_counts)
            and set(cur_deck_counts) == set(last_deck_counts)
            and all(
                _deck_entry_count(cur_deck_counts[k]) == _deck_entry_count(last_deck_counts[k])
                for k in cur_deck_counts
            )
        )
        if full or not last_deck_counts:
            lines.append(f"DECK ({len(deck_refs)}):")
            for _ckey, group in grouped_deck.items():
                ref0, ce0 = group[0]
                refs_s = ",".join(r for r, _ in group)
                prefix = f"{len(group)}x[{refs_s}]" if len(group) > 1 else ref0
                lines.append(
                    "  "
                    + _fmt_card_line(
                        prefix,
                        ce0,
                        descriptions.get(ref0),
                        _as_dict(mod_descriptions.get(ref0)),
                        upgrades.get(ref0),
                        seen_defs,
                        show_upgrade=bool(upgrades.get(ref0)),
                        force_text=False,
                    )
                )
        elif deck_same:
            lines.append(f"DECK: unchanged ({len(deck_refs)} cards)")
        else:
            lines.append(f"DECK ({len(deck_refs)} cards, delta):")
            for ckey in sorted(set(cur_deck_counts) | set(last_deck_counts)):
                old_n = _deck_entry_count(last_deck_counts.get(ckey))
                new_n = _deck_entry_count(cur_deck_counts.get(ckey))
                if new_n > old_n:
                    group = grouped_deck[ckey]
                    ref0, ce0 = group[0]
                    lines.append(
                        f"  +{new_n - old_n}x (now {new_n}) "
                        + _fmt_card_line(
                            ref0,
                            ce0,
                            descriptions.get(ref0),
                            _as_dict(mod_descriptions.get(ref0)),
                            upgrades.get(ref0),
                            seen_defs,
                            show_upgrade=bool(upgrades.get(ref0)),
                            force_text=False,
                        )
                    )
                elif new_n < old_n:
                    rem_lbl = _deck_entry_label(ckey, last_deck_counts.get(ckey))
                    lines.append(f"  -{old_n - new_n}x (now {new_n}) {rem_lbl}")
        last_deck_counts = cur_deck_counts
    elif combat_exit:
        deck_refs = [_str(r) for r in _as_list(state.get("deck"))]
        lines.append(f"DECK: {len(deck_refs)} cards")

    # Terminal sections (include optional enemies and final deck summary)
    if phase == "terminal":
        term_enemies = _as_list(state.get("enemies"))
        if term_enemies:
            lines.append("ENEMIES:")
            for raw in term_enemies:
                lines.append(_fmt_enemy_line(_as_dict(raw)))
        deck_refs = [_str(r) for r in _as_list(state.get("deck"))]
        lines.append(f"DECK ({len(deck_refs)}):")
        grouped_term: dict[str, list[tuple[str, JsonObj]]] = {}
        for ref in deck_refs:
            ce = _as_dict(objects.get(ref))
            grouped_term.setdefault(_card_key(ce, descriptions.get(ref)), []).append((ref, ce))
        for _ckey, group in grouped_term.items():
            ref0, ce0 = group[0]
            up = ce0.get("upgrades")
            up_s = f"+{up}" if isinstance(up, int) and up > 0 else ""
            cnt = f"{len(group)}x " if len(group) > 1 else ""
            lines.append(f"  {cnt}{ce0.get('name')}{up_s} ({ce0.get('definition_id')})")

    # Actions (omit unavailable map moves unless --full)
    actions = _as_list(state.get("actions"))
    if actions and not combat_exit and not run_entered_combat:
        offer_contents = {
            _str(c)
            for o in _as_list(state.get("offers"))
            for c in _as_list(_as_dict(o).get("contents"))
        }
        action_lines: list[str] = []
        omitted_map_moves = 0
        for raw in actions:
            ac = _as_dict(raw)
            enabled = bool(ac.get("enabled", True))
            if not full and not enabled and ac.get("destination") is not None:
                omitted_map_moves += 1
                continue
            bits = [f'{ac.get("ref")} | "{ac.get("label")}"']
            if not enabled:
                bits.append(f"DISABLED({_one_line(ac.get('reason'))})")
            for k in ("operation", "destination", "subject"):
                if ac.get(k) is not None:
                    bits.append(f"{k}={ac.get(k)}")
            acost = _fmt_cost(ac.get("cost"))
            if acost:
                bits.append(f"cost({acost})")
            targs = _as_list(ac.get("targets"))
            if targs:
                bits.append("targets=" + ",".join(_str(t) for t in targs))
            subj = _str(ac.get("subject"))
            if subj and subj not in offer_contents:
                sobj = _as_dict(objects.get(subj))
                if sobj.get("definition_id"):
                    bits.append(f"({sobj.get('definition_id')})")
                if subj in descriptions:
                    sdesc = _one_line(descriptions.get(subj))
                    if sdesc:
                        bits.append(f'"{sdesc}"')
            action_lines.append("  " + " ".join(bits))
        if action_lines or omitted_map_moves:
            hdr_suffix = (
                f" (omitted {omitted_map_moves} unavailable map moves)" if omitted_map_moves else ""
            )
            lines.append(f"ACTIONS{hdr_suffix}:")
            lines.extend(action_lines)

    # Selection (always full, with candidate details & upgrade previews)
    sel = _as_dict(state.get("selection"))
    if sel and not combat_exit and not run_entered_combat:
        src = _as_dict(sel.get("source"))
        src_s = f" source={src.get('name')}({src.get('definition_id')})" if src else ""
        lines.append(
            f"SELECTION: min={sel.get('min')} max={sel.get('max')} "
            f'selected={sel.get("selected")}{src_s} prompt="{_one_line(sel.get("prompt"))}"'
        )
        for cref_raw in _as_list(sel.get("candidates")):
            lines.extend(
                _fmt_item_or_bundle(
                    _str(cref_raw),
                    objects,
                    descriptions,
                    mod_descriptions,
                    upgrades,
                    seen_defs,
                    indent="  ",
                )
            )

    # New generated previews and mechanisms
    if not combat_exit and not run_entered_combat:
        new_gens: list[str] = []
        for gref, gitems in generated.items():
            for raw in _as_list(gitems):
                g = _as_dict(raw)
                gc = _as_dict(g.get("card"))
                gsig = _hash(gc)
                if gsig not in seen_gen:
                    seen_gen.add(gsig)
                    cost = _fmt_cost(gc.get("cost"))
                    up = gc.get("upgrades")
                    up_s = f"+{up}" if isinstance(up, int) and up > 0 else ""
                    gmeta = [_str(gc.get("card_type"))]
                    if cost:
                        gmeta.append(f"cost({cost})")
                    gkws = _as_list(gc.get("keywords"))
                    if gkws:
                        gmeta.append("kw(" + ",".join(_str(k) for k in gkws) + ")")
                    gench = _fmt_modifier("ench", gc.get("enchantment"), None)
                    gaff = _fmt_modifier("aff", gc.get("affliction"), None)
                    gmods = " ".join(x for x in (gench, gaff) if x)
                    gmods_s = f" {gmods}" if gmods else ""
                    new_gens.append(
                        f"  from {gref}: {gc.get('name')}{up_s} ({gc.get('definition_id')}) "
                        f"[{' '.join(x for x in gmeta if x)}]{gmods_s} "
                        f'"{_one_line(gc.get("text"))}"'
                    )
        if new_gens:
            lines.append("GENERATED (new):")
            lines.extend(new_gens)

        grouped_mechs: dict[str, tuple[JsonObj, list[str]]] = {}
        for raw in mechanisms:
            m = _as_dict(raw)
            mid = m.get("definition_id")
            identity = mid if isinstance(mid, str) and mid else m.get("name")
            msig = _hash(
                {
                    "kind": m.get("kind"),
                    "identity": identity,
                    "text": m.get("text"),
                }
            )
            if msig in seen_mechs:
                continue
            entry = grouped_mechs.get(msig)
            if entry is None:
                owners: list[str] = []
                grouped_mechs[msig] = (m, owners)
            else:
                owners = entry[1]
            owner_val = m.get("owner")
            if isinstance(owner_val, str) and owner_val and owner_val not in owners:
                owners.append(owner_val)

        new_mechs: list[str] = []
        for msig, (m, owners) in grouped_mechs.items():
            seen_mechs.add(msig)
            owner_s = f" owner={','.join(owners)}" if owners and m.get("kind") != "keyword" else ""
            did = f"({m.get('definition_id')})" if m.get("definition_id") else ""
            new_mechs.append(
                f'  [{m.get("kind")}] {m.get("name")}{did}{owner_s}: "{_one_line(m.get("text"))}"'
            )
        if new_mechs:
            lines.append("MECHANISMS (new):")
            lines.extend(new_mechs)

    # Map (deduplicate act topology across turns; always show legal_destinations)
    data_dict = _as_dict(data)
    map_obj = top_map or _as_dict(data_dict.get("map"))
    if map_obj and not combat_exit and not run_entered_combat:
        topo_hash = _hash(
            {
                "bosses": map_obj.get("bosses"),
                "elite_pool": map_obj.get("elite_pool"),
                "nodes": map_obj.get("nodes"),
            }
        )
        show_topo = (
            full or force_map_topology or "map" in data_dict or topo_hash != last_map_topo_hash
        )
        if show_topo:
            lines.extend(_fmt_map(map_obj))
            last_map_topo_hash = topo_hash
        else:
            legal_s = json.dumps(map_obj.get("legal_destinations"), ensure_ascii=False)
            lines.append(f"MAP: unchanged topology; legal_destinations={legal_s}")

    # Query data (other than already-rendered map)
    if data is not None and not (isinstance(data, dict) and set(data_dict.keys()) == {"map"}):
        lines.append("DATA: " + json.dumps(data, ensure_ascii=False))

    cache["defs"] = seen_defs
    cache["mechs"] = sorted(seen_mechs)
    cache["gen"] = sorted(seen_gen)
    cache["relics"] = seen_relics
    cache["potions_hash"] = last_potions_hash
    cache["piles_hash"] = last_piles_hash
    cache["piles_node"] = last_piles_node
    cache["deck_counts"] = last_deck_counts
    cache["map_topo_hash"] = last_map_topo_hash
    return "\n".join(lines)


def cmd_tools(url: str) -> int:
    code, text = _http_get(url.rstrip("/") + "/tools")
    if code == 0:
        print(text, file=sys.stderr)
        return 1
    if code != 200:
        print(f"HTTP {code}: {text}", file=sys.stderr)
        return 1
    try:
        data = _as_dict(json.loads(text))
    except json.JSONDecodeError as err:
        print(f"Invalid JSON from /tools: {err}", file=sys.stderr)
        return 1
    roles = _as_dict(data.get("roles"))
    for role_name in ("run", "combat"):
        print(f"[{role_name}]")
        for item in _as_list(roles.get(role_name)):
            td = _as_dict(item)
            print(f"  - {td.get('name')}: {_one_line(td.get('description'))}")
    return 0


def _parsed_arg_flags(
    tool_name: str,
    arg_text: str,
    *,
    confirmed_steps: int | None = None,
) -> tuple[bool, bool, set[str]]:
    """Return (is_state_inspect, contains_empty_select_step, skipped_interact_actions)."""
    try:
        parsed = _as_dict(json.loads(arg_text))
    except json.JSONDecodeError:
        return False, False, set()
    is_state = tool_name == "run_inspect" and parsed.get("area") == "state"
    steps = [_as_dict(s) for s in _as_list(parsed.get("steps"))]
    if confirmed_steps is not None:
        steps = steps[: max(0, confirmed_steps)]
    has_empty_select = any(s.get("kind") == "select" and not _as_list(s.get("refs")) for s in steps)
    skipped_interacts: set[str] = set()
    if tool_name == "run_act":
        # Record interact actions preceding an empty select in the confirmed prefix so
        # skipped Prayer Wheel rewards stay guarded even if a later step stops.
        for idx, step in enumerate(steps):
            if step.get("kind") == "select" and not _as_list(step.get("refs")):
                for prev in steps[:idx]:
                    if prev.get("kind") == "interact" and prev.get("action") is not None:
                        skipped_interacts.add(_str(prev.get("action")))
    return is_state, has_empty_select, skipped_interacts


def cmd_call(
    url: str,
    tool_name: str,
    arg_text: str,
    *,
    session: str | None,
    full: bool,
    raw: bool,
    auto: bool = False,
    handoff_trim: bool = False,
    force_map_topology: bool = False,
) -> int:
    endpoint = f"{url.rstrip('/')}/tools/{tool_name}"
    code, text = _http_post(endpoint, arg_text)
    if code == 0:
        print(text, file=sys.stderr)
        return 1
    if raw or code != 200:
        print(f"HTTP {code}" if code != 200 else "", end="\n" if code != 200 else "")
        print(text)
        return 0 if code == 200 else 1
    try:
        payload = _as_dict(json.loads(text))
    except json.JSONDecodeError:
        print(text)
        return 1
    cache = _load_cache(session)
    caller_outcome = _as_dict(payload.get("outcome"))
    caller_status = _str(caller_outcome.get("status"))
    confirmed_prefix: int | None = None
    if caller_status == "stopped":
        confirmed_prefix = _int(caller_outcome.get("completed_steps"))
    elif caller_status != "completed":
        confirmed_prefix = 0
    is_state_inspect, has_select, skipped_interacts = _parsed_arg_flags(
        tool_name, arg_text, confirmed_steps=confirmed_prefix
    )
    if caller_status in ("completed", "stopped"):
        _record_observed_auto_guards(payload, cache, skipped_card_actions=skipped_interacts)
    auto_log: list[str] = []
    can_auto = (
        auto
        and not (handoff_trim and tool_name.startswith("combat_"))
        and (tool_name in ("run_act", "combat_act") or is_state_inspect)
    )
    if can_auto:
        payload, auto_log = _run_auto_advance(
            url,
            payload,
            cache,
            allow_stateful_auto=session is not None,
            skip_card_open=has_select,
        )
    print(
        render_reply(
            payload,
            cache,
            full=full,
            tool_name=tool_name,
            force_map_topology=force_map_topology,
            handoff_trim=handoff_trim,
            auto_log=auto_log,
            caller_outcome=caller_outcome,
        )
    )
    _save_cache(session, cache)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--url", default=argparse.SUPPRESS, help="External backend base URL.")
    shared.add_argument(
        "--session",
        default=argparse.SUPPRESS,
        help="Session ID for deduplicating unchanged descriptions/relics/deck/mechanisms.",
    )
    shared.add_argument(
        "--full",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print a full compact snapshot and reset this session's delta baseline.",
    )
    shared.add_argument(
        "--auto",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Auto-advance strictly mechanical zero-branch Run steps "
            "(gold/relic/free potion/open sole card/open shop/chest/proceed/sole map path)."
        ),
    )
    shared.add_argument(
        "--handoff",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Trim cross-phase payload on combat entry/exit for multi-agent handoffs.",
    )
    shared.add_argument(
        "--raw",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print raw JSON response.",
    )

    parser = argparse.ArgumentParser(
        description="Compact CLI client for STS2 Harness external HTTP tools."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="External backend base URL.")
    parser.add_argument(
        "--session",
        default=None,
        help="Session ID for deduplicating unchanged descriptions/relics/deck/mechanisms.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print a full compact snapshot and reset this session's delta baseline.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "Auto-advance strictly mechanical zero-branch Run steps "
            "(gold/relic/free potion/open sole card/open shop/chest/proceed/sole map path)."
        ),
    )
    parser.add_argument(
        "--handoff",
        action="store_true",
        help="Trim cross-phase payload on combat entry/exit for multi-agent handoffs.",
    )
    parser.add_argument("--raw", action="store_true", help="Print raw JSON response.")

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools", parents=[shared], help="List available run and combat tools.")

    p_call = sub.add_parser(
        "call", parents=[shared], help="Invoke POST /tools/<name> with JSON arguments."
    )
    p_call.add_argument("tool", help="Tool name (e.g., run_inspect, combat_act).")
    p_call.add_argument("json_arg", nargs="?", default="{}", help="JSON argument object.")

    p_state = sub.add_parser("state", parents=[shared], help="Inspect current state.")
    p_state.add_argument("role", choices=["run", "combat"])

    p_map = sub.add_parser("map", parents=[shared], help="Inspect act map.")
    p_map.add_argument("role", choices=["run", "combat"])

    p_act = sub.add_parser("act", parents=[shared], help="Submit run_act or combat_act steps.")
    p_act.add_argument("role", choices=["run", "combat"])
    p_act.add_argument(
        "payload",
        help='JSON steps array "[...]" or full argument object \'{"steps":[...]}\'.',
    )
    p_act.add_argument("--reason", default=None, help="Optional short reason string.")

    p_notes = sub.add_parser("notes", parents=[shared], help="Read or write run_notes.")
    p_notes.add_argument("mode", choices=["read", "write"])
    p_notes.add_argument("payload", nargs="?", default="{}", help="JSON fields for mode=write.")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    url = _str(args.url)
    session = args.session if isinstance(args.session, str) else None
    full = bool(args.full)
    raw = bool(args.raw)
    auto = bool(args.auto)
    handoff = bool(args.handoff)
    cmd = _str(args.cmd)

    if cmd == "tools":
        return cmd_tools(url)
    if cmd == "call":
        return cmd_call(
            url,
            _str(args.tool),
            _str(args.json_arg),
            session=session,
            full=full,
            raw=raw,
            auto=auto,
            handoff_trim=handoff,
        )
    if cmd == "state":
        return cmd_call(
            url,
            f"{args.role}_inspect",
            '{"area":"state"}',
            session=session,
            full=full,
            raw=raw,
            auto=auto,
            handoff_trim=handoff,
        )
    if cmd == "map":
        return cmd_call(
            url,
            f"{args.role}_inspect",
            '{"area":"map"}',
            session=session,
            full=full,
            raw=raw,
            auto=False,
            handoff_trim=handoff,
            force_map_topology=True,
        )
    if cmd == "act":
        try:
            parsed: object = json.loads(_str(args.payload))
        except json.JSONDecodeError as err:
            print(f"Invalid JSON for act payload: {err}", file=sys.stderr)
            return 2
        if isinstance(parsed, list):
            body: JsonObj = {"steps": _as_list(cast(object, parsed))}
            if args.reason:
                body["reason"] = _str(args.reason)
        elif isinstance(parsed, dict):
            body = _as_dict(cast(object, parsed))
            if args.reason and "reason" not in body:
                body["reason"] = _str(args.reason)
        else:
            print("act payload must be a JSON array of steps or object.", file=sys.stderr)
            return 2
        return cmd_call(
            url,
            f"{args.role}_act",
            json.dumps(body, ensure_ascii=False),
            session=session,
            full=full,
            raw=raw,
            auto=auto,
            handoff_trim=handoff,
        )
    if cmd == "notes":
        if args.mode == "read":
            body_str = '{"mode":"read"}'
        else:
            try:
                parsed_notes: object = json.loads(_str(args.payload))
            except json.JSONDecodeError as err:
                print(f"Invalid JSON for notes write: {err}", file=sys.stderr)
                return 2
            if not isinstance(parsed_notes, dict):
                print("notes write payload must be a JSON object.", file=sys.stderr)
                return 2
            fields = _as_dict(cast(object, parsed_notes))
            fields["mode"] = "write"
            body_str = json.dumps(fields, ensure_ascii=False)
        return cmd_call(
            url,
            "run_notes",
            body_str,
            session=session,
            full=full,
            raw=raw,
            auto=False,
            handoff_trim=handoff,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
