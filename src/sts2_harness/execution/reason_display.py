"""Sanitize viewer-facing reason text using verified localized game names."""

import re
from collections.abc import Callable, Mapping
from enum import StrEnum

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.client.http import CatalogResult, InspectResult


class ViewerReasonKind(StrEnum):
    ACTION = "action"
    ROUTE = "route"
    CARD_CHOICE = "card_choice"


class ViewerReason(native.WireModel):
    kind: ViewerReasonKind
    display_reason: str
    game_language: str
    role: native.DecisionOwner | None = None
    node_id: str | None = None
    offer_ref: str | None = None
    call_id: str | None = None
    batch_index: int | None = None
    native_card_preview: bool = False


_DEF_ID = (
    r"(?:CARD|RELIC|POTION|POWER|ENCHANTMENT|AFFLICTION|EVENT|ENCOUNTER|"
    r"MONSTER|CHARACTER|ACT|ORB|ANCIENT)\.[A-Z0-9_]+\+?"
)
_EPHEMERAL_REF = (
    r"(?:card|offer|action|interaction|potion|relic|enemy|monster|orb|"
    r"bundle|reward|choice|option|target|ally):[A-Za-z0-9_:-]+"
)
_INTERNAL_TOKEN = rf"(?:{_DEF_ID}|{_EPHEMERAL_REF})"
_WRAPPED_TOKEN = (
    rf"(?:\[\[{_INTERNAL_TOKEN}\]\]|`{_INTERNAL_TOKEN}`|"
    rf"(?<![A-Za-z0-9_]){_INTERNAL_TOKEN}(?![A-Za-z0-9_]))"
)
_PAREN_INTERNAL = rf"[(\（]\s*{_WRAPPED_TOKEN}(?:\s*[,/，、]\s*{_WRAPPED_TOKEN})*\s*[)\）]"

_COMPOUND_RE = re.compile(rf"({_WRAPPED_TOKEN})\s*({_PAREN_INTERNAL})")
_PAREN_RE = re.compile(rf"\s*({_PAREN_INTERNAL})")
_TOKEN_RE = re.compile(_WRAPPED_TOKEN)
_RAW_TOKEN_RE = re.compile(_INTERNAL_TOKEN)
_EMPTY_DELIM_RE = re.compile(r"\[\[\s*\]\]|\(\s*\)|（\s*）|``")
_DANGLING_OPEN_SEP_RE = re.compile(r"([(\（])\s*[,/，、]\s*")
_DANGLING_CLOSE_SEP_RE = re.compile(r"\s*[,/，、]\s*([)\）])")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?，。；：！？、])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([(\（\[【「『])[ \t]+")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def _unwrap(token: str) -> str:
    if token.startswith("[[") and token.endswith("]]"):
        return token[2:-2]
    if token.startswith("`") and token.endswith("`"):
        return token[1:-1]
    return token


def _already_named(prefix: str, name: str) -> bool:
    stripped = prefix.rstrip()
    return stripped.endswith(name) or stripped.endswith(f"{name}+")


def _sub_with_prefix(
    pattern: re.Pattern[str],
    text: str,
    choose: Callable[[str, re.Match[str]], str],
) -> str:
    parts: list[str] = []
    position = 0
    for match in pattern.finditer(text):
        parts.append(text[position : match.start()])
        prefix = "".join(parts)
        parts.append(choose(prefix, match))
        position = match.end()
    parts.append(text[position:])
    return "".join(parts)


class ReasonDisplaySanitizer:
    """Resolve verified localized names and strip leaked internal IDs/refs from viewer reasons."""

    def __init__(self) -> None:
        self._language: str | None = None
        self._definition_names: dict[str, str] = {}
        self._ref_names: dict[str, str] = {}

    @property
    def language(self) -> str:
        return self._language or ""

    def _record_definition(
        self, definition_id: str | None | MISSING, name: str | None | MISSING
    ) -> None:
        if definition_id is MISSING or name is MISSING or not definition_id or not name:
            return
        self._definition_names[definition_id] = name

    def _index_objects(self, objects: Mapping[str, native.ObjectEntry]) -> None:
        for ref, entry in objects.items():
            self._ref_names[ref] = entry.name
            if isinstance(entry, native.CardEntry):
                if entry.upgrades == 0 or entry.definition_id not in self._definition_names:
                    self._definition_names[entry.definition_id] = entry.name
                if entry.enchantment is not None:
                    self._record_definition(entry.enchantment.definition_id, entry.enchantment.name)
                if entry.affliction is not None:
                    self._record_definition(entry.affliction.definition_id, entry.affliction.name)
                continue
            self._record_definition(entry.definition_id, entry.name)
            if isinstance(entry, (native.EnemyEntry, native.AllyEntry)):
                for power in entry.powers:
                    self._record_definition(power.definition_id, power.name)

    def _index_generated(self, generated: Mapping[str, tuple[native.GeneratedCard, ...]]) -> None:
        for entries in generated.values():
            for item in entries:
                self._record_definition(item.card.definition_id, item.card.name)
                self._record_definition(item.source.definition_id, item.source.name)

    def _index_upgrades(self, upgrades: Mapping[str, native.UpgradePreview]) -> None:
        for upgrade in upgrades.values():
            if isinstance(upgrade, native.UpgradeAvailable):
                if upgrade.card.definition_id not in self._definition_names:
                    self._record_definition(upgrade.card.definition_id, upgrade.card.name)

    def _index_mechanisms(self, mechanisms: tuple[native.MechanismView, ...]) -> None:
        for mechanism in mechanisms:
            self._record_definition(mechanism.definition_id, mechanism.name)

    def _index_map(self, public_map: native.PublicMap) -> None:
        for boss in public_map.bosses:
            self._record_definition(boss.definition_id, boss.name)
        if public_map.elite_pool is not MISSING:
            for elite in public_map.elite_pool:
                self._record_definition(elite.definition_id, elite.name)

    def observe(self, observation: native.Observation) -> None:
        if (
            self._language is not None
            and observation.game_language
            and observation.game_language != self._language
        ):
            self._definition_names.clear()
            self._ref_names.clear()
        if observation.game_language:
            self._language = observation.game_language
        self._index_objects(observation.objects)
        self._index_generated(observation.generated)
        self._index_upgrades(observation.upgrades)
        self._index_mechanisms(observation.mechanisms)
        if observation.encounter is not MISSING:
            self._record_definition(observation.encounter.definition_id, observation.encounter.name)
        if observation.map is not MISSING:
            self._index_map(observation.map)
        state = observation.state
        if isinstance(state, (native.RunState, native.CombatState)):
            self._record_definition(state.player.definition_id, state.player.name)
            self._record_definition(state.progress.definition_id, state.progress.name)
            if isinstance(state, native.CombatState):
                for power in state.player.powers:
                    self._record_definition(power.definition_id, power.name)

    def observe_inspect(self, result: InspectResult) -> None:
        if not isinstance(result, native.InspectCompleted):
            return
        data = result.data
        if isinstance(data, native.DetailData):
            self._index_objects(data.objects)
            self._index_generated(data.generated)
            self._index_mechanisms(data.mechanisms)
            if data.upgrades is not MISSING:
                self._index_upgrades(data.upgrades)
        elif isinstance(data, native.HistoryData):
            for group in (
                data.cards_played_this_combat,
                data.cards_played_this_turn,
                data.cards_exhausted_this_turn,
            ):
                for item in group:
                    self._record_definition(item.definition_id, item.name)
        elif isinstance(data, native.MapData):
            self._index_map(data.map)

    def observe_catalog(self, result: CatalogResult) -> None:
        if not isinstance(result, native.CatalogCompleted):
            return
        data = result.data
        if isinstance(data, native.LookupData):
            for entry in data.entries:
                self._record_definition(entry.definition_id, entry.name)
        elif isinstance(data, native.SearchData):
            for match in data.matches:
                self._record_definition(match.definition_id, match.name)

    def _lookup(self, raw_token: str) -> str | None:
        token = _unwrap(raw_token)
        if token in self._ref_names:
            return self._ref_names[token]
        if token in self._definition_names:
            return self._definition_names[token]
        if token.endswith("+"):
            base = self._definition_names.get(token[:-1])
            if base is not None:
                return base if base.endswith("+") else f"{base}+"
        return None

    def _lookup_first(self, tokens: list[str]) -> str | None:
        # Prefer exact ephemeral instance refs first (e.g. upgraded card instance names).
        for token in tokens:
            if token in self._ref_names:
                return self._ref_names[token]
        for token in tokens:
            name = self._lookup(token)
            if name is not None:
                return name
        return None

    def sanitize(self, reason: str) -> str:
        text = reason.strip()
        if not text:
            return ""

        def replace_compound(prefix: str, match: re.Match[str]) -> str:
            tokens = _RAW_TOKEN_RE.findall(match.group(0))
            name = self._lookup_first(tokens)
            if name is None or _already_named(prefix, name):
                return ""
            return name

        def replace_paren(prefix: str, match: re.Match[str]) -> str:
            tokens = _RAW_TOKEN_RE.findall(match.group(1))
            name = self._lookup_first(tokens)
            if name is None or _already_named(prefix, name):
                return ""
            leading = match.group(0)[: match.start(1) - match.start(0)]
            return f"{leading}{name}" if prefix.strip() and leading else name

        def replace_token(prefix: str, match: re.Match[str]) -> str:
            name = self._lookup(match.group(0))
            if name is None or _already_named(prefix, name):
                return ""
            return name

        text = _sub_with_prefix(_COMPOUND_RE, text, replace_compound)
        text = _sub_with_prefix(_PAREN_RE, text, replace_paren)
        text = _sub_with_prefix(_TOKEN_RE, text, replace_token)
        text = _DANGLING_OPEN_SEP_RE.sub(r"\1", text)
        text = _DANGLING_CLOSE_SEP_RE.sub(r"\1", text)
        text = _EMPTY_DELIM_RE.sub("", text)
        text = _MULTI_SPACE_RE.sub(" ", text)
        text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
        text = _SPACE_AFTER_OPEN_RE.sub(r"\1", text)
        return text.strip()
