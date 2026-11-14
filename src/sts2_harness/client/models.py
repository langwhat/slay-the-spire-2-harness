"""Concrete public Mod values; omitted native facts stay distinct from null."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    MISSING,
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


def _distinct[T](values: tuple[T, ...]) -> tuple[T, ...]:
    if len(values) != len(set(values)):
        raise ValueError("Duplicate values are not permitted.")
    return values


def _public_intent_type(value: str) -> str:
    if value == HiddenIntentType.UNKNOWN:
        raise ValueError("Hidden intents cannot contain public details.")
    return value


def _non_card_category(value: str) -> str:
    if value == NativeCategory.CARD:
        raise ValueError("Card matches require card metadata.")
    return value


type Nonempty = Annotated[str, Field(min_length=1)]
type Number = int | float


def _freeze_directory[T](values: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(values))


def _directory_items[T](values: Mapping[str, T]) -> dict[str, T]:
    return dict(values)


type Directory[T] = Annotated[
    Mapping[Nonempty, T], AfterValidator(_freeze_directory), PlainSerializer(_directory_items)
]


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_unset=True)


class StateControl(StrEnum):
    READY = "ready"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


class Phase(StrEnum):
    RUN = "run"
    COMBAT = "combat"
    TERMINAL = "terminal"


class RunScreen(StrEnum):
    MAP = "map"
    REWARDS = "rewards"
    SHOP = "shop"
    EVENT = "event"
    REST = "rest"
    TREASURE = "treasure"
    SELECTION = "selection"
    TRANSITION = "transition"
    OTHER = "other"


class CombatScreen(StrEnum):
    COMBAT = "combat"
    CARD_SELECTION = "card_selection"
    TRANSITION = "transition"
    OTHER = "other"


class TerminalScreen(StrEnum):
    RUN_COMPLETE = "run_complete"


class DecisionOwner(StrEnum):
    RUN = "run"
    COMBAT = "combat"


class InputReadiness(StrEnum):
    READY = "ready"
    SELECTION = "selection"
    BUSY = "busy"
    TERMINAL = "terminal"
    UNAVAILABLE = "unavailable"


class ControlIntent(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"


class ResultStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class UpgradeStatus(StrEnum):
    AVAILABLE = "available"
    NOT_UPGRADABLE = "not_upgradable"
    UNAVAILABLE = "unavailable"


class ObjectKind(StrEnum):
    CARD = "card"
    RELIC = "relic"
    POTION = "potion"
    OFFER = "offer"
    EVENT = "event"
    OPTION = "option"
    BUNDLE = "bundle"
    ENEMY = "enemy"
    ALLY = "ally"


class OfferKind(StrEnum):
    CARD = "card"
    RELIC = "relic"
    POTION = "potion"
    GOLD = "gold"
    SERVICE = "service"
    CHOICE = "choice"
    BUNDLE = "bundle"


class MechanismKind(StrEnum):
    KEYWORD = "keyword"
    POWER = "power"
    ORB = "orb"
    ENCHANTMENT = "enchantment"
    AFFLICTION = "affliction"
    RELIC = "relic"
    POTION = "potion"
    OTHER = "other"


class HpDisplay(StrEnum):
    NUMERIC = "numeric"
    INFINITE = "infinite"
    UNKNOWN = "unknown"


class TerminalResult(StrEnum):
    VICTORY = "victory"
    DEFEAT = "defeat"
    ESCAPED = "escaped"
    UNKNOWN = "unknown"


class InspectArea(StrEnum):
    MAP = "map"
    COMBAT_HISTORY = "combat_history"
    HAND = "hand"
    DRAW = "draw"
    DISCARD = "discard"
    EXHAUST = "exhaust"
    OTHER = "other"
    DECK = "deck"
    OPTIONS = "options"
    REWARDS = "rewards"
    SHOP = "shop"
    RELICS = "relics"
    POTIONS = "potions"
    ENEMIES = "enemies"
    POWERS = "powers"


class CatalogMode(StrEnum):
    CATALOG = "catalog"
    SEARCH = "search"
    LOOKUP = "lookup"


class NativeCategory(StrEnum):
    CARD = "card"
    RELIC = "relic"
    POTION = "potion"
    POWER = "power"
    ENEMY = "enemy"
    ORB = "orb"
    KEYWORD = "keyword"
    EVENT = "event"
    ENCOUNTER = "encounter"
    CHARACTER = "character"


class CostSymbol(StrEnum):
    X = "X"
    NONE = "none"


class HiddenIntentType(StrEnum):
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NO_RUN = "no_run"
    READ_UNAVAILABLE = "read_unavailable"
    IDENTITY_UNREGISTERED = "identity_unregistered"
    INVALID_REFS = "invalid_refs"
    AREA_UNAVAILABLE = "area_unavailable"
    OBSERVATION_CHANGED = "observation_changed"
    UNSUPPORTED_CATEGORY = "unsupported_category"
    INVALID_FILTER = "invalid_filter"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    CATALOG_IDENTITY_CONFLICT = "catalog_identity_conflict"
    NATIVE_PREVIEW_FAILED = "native_preview_failed"
    ROUTE_CHANGED = "route_changed"
    NATIVE_BUSY = "native_busy"
    WRONG_OWNER = "wrong_owner"
    ACTION_UNAVAILABLE = "action_unavailable"
    SELECTION_REQUIRED = "selection_required"
    SELECTION_UNAVAILABLE = "selection_unavailable"
    DUPLICATE_OPERATION = "duplicate_operation"
    INVALID_TARGET = "invalid_target"
    INVALID_SELECTION = "invalid_selection"
    DISPATCH_FAILED = "dispatch_failed"
    EXECUTION_UNCERTAIN = "execution_uncertain"
    INTERNAL_ERROR = "internal_error"
    CONTROL_UNAVAILABLE = "control_unavailable"


class NamedDefinition(WireModel):
    name: Nonempty
    definition_id: Nonempty | MISSING = MISSING


class ResourceCosts(WireModel):
    energy: Annotated[int, Field(ge=0)] | CostSymbol | None | MISSING = MISSING
    stars: Annotated[int, Field(ge=0)] | CostSymbol | None | MISSING = MISSING

    @model_validator(mode="after")
    def require_resource(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one resource must apply.")
        return self


class Resources(WireModel):
    energy: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    stars: Annotated[int, Field(ge=0)] | None | MISSING = MISSING


class ActionCost(WireModel):
    gold: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    hp: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    energy: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    stars: Annotated[int, Field(ge=0)] | None | MISSING = MISSING

    @model_validator(mode="after")
    def require_resource(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one resource must apply.")
        return self


class Progress(WireModel):
    acts: Annotated[int, Field(ge=1)]
    act: Annotated[int, Field(ge=0)] | None
    floor: Annotated[int, Field(ge=0)] | None
    position: Nonempty | None
    room_type: Nonempty | None
    name: Nonempty | MISSING = MISSING
    definition_id: Nonempty | MISSING = MISSING
    bosses_left: Annotated[int, Field(ge=0)] | MISSING = MISSING


class AscensionRule(WireModel):
    level: Annotated[int, Field(ge=1)]
    name: Nonempty
    text: Nonempty


class RunRules(WireModel):
    ascension: Annotated[int, Field(ge=0)]
    rules: tuple[AscensionRule, ...]


class Power(WireModel):
    name: Nonempty
    definition_id: Nonempty
    amount: Number | None | MISSING = MISSING
    display_amount: Number | None | MISSING = MISSING
    type: Nonempty | None | MISSING = MISSING


class HiddenIntent(WireModel):
    type: HiddenIntentType


class PublicIntent(WireModel):
    type: Annotated[Nonempty, AfterValidator(_public_intent_type)]
    name: str | MISSING = MISSING
    damage_per_hit: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    hits: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    amount: Number | None | MISSING = MISSING
    description: str | None | MISSING = MISSING


class Player(WireModel):
    base_draw: Annotated[int, Field(ge=0)] | MISSING = MISSING
    base_energy: Annotated[int, Field(ge=0)] | MISSING = MISSING
    name: Nonempty
    definition_id: Nonempty
    hp: Annotated[int, Field(ge=0)]
    max_hp: Annotated[int, Field(ge=0)]
    gold: Annotated[int, Field(ge=0)]
    potion_slot_count: Annotated[int, Field(ge=0)]
    empty_potion_slot_count: Annotated[int, Field(ge=0)]
    block: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    resources: Resources | MISSING = MISSING
    powers: tuple[Power, ...] | MISSING = MISSING


class CombatPlayer(WireModel):
    name: Nonempty
    definition_id: Nonempty
    hp: Annotated[int, Field(ge=0)]
    max_hp: Annotated[int, Field(ge=0)]
    gold: Annotated[int, Field(ge=0)]
    potion_slot_count: Annotated[int, Field(ge=0)]
    empty_potion_slot_count: Annotated[int, Field(ge=0)]
    block: Annotated[int, Field(ge=0)] | None
    resources: Resources
    powers: tuple[Power, ...]


class RunPlayer(WireModel):
    base_draw: Annotated[int, Field(ge=0)] | MISSING = MISSING
    base_energy: Annotated[int, Field(ge=0)] | MISSING = MISSING
    name: Nonempty
    definition_id: Nonempty
    hp: Annotated[int, Field(ge=0)]
    max_hp: Annotated[int, Field(ge=0)]
    gold: Annotated[int, Field(ge=0)]
    potion_slot_count: Annotated[int, Field(ge=0)]
    empty_potion_slot_count: Annotated[int, Field(ge=0)]


class Enemy(WireModel):
    ref: Nonempty
    name: Nonempty
    hp: Annotated[int, Field(ge=0)] | None
    max_hp: Annotated[int, Field(ge=0)] | None
    block: Annotated[int, Field(ge=0)] | None
    powers: tuple[Power, ...]
    definition_id: Nonempty
    intents: tuple[Intent, ...]
    hp_display: HpDisplay | MISSING = MISSING


class Ally(WireModel):
    ref: Nonempty
    name: Nonempty
    hp: Annotated[int, Field(ge=0)] | None
    max_hp: Annotated[int, Field(ge=0)] | None
    block: Annotated[int, Field(ge=0)] | None
    powers: tuple[Power, ...]
    alive: bool | None
    definition_id: Nonempty | MISSING = MISSING
    hp_display: HpDisplay | MISSING = MISSING


class Orb(WireModel):
    name: Nonempty
    passive: Number | None
    evoke: Number | None
    definition_id: Nonempty | MISSING = MISSING


class ActionOperation(StrEnum):
    OPEN_SHOP = "open_shop"
    CLOSE_SHOP = "close_shop"
    LEAVE_SHOP = "leave_shop"
    OPEN_CHEST = "open_chest"
    PROCEED = "proceed"


class Action(WireModel):
    ref: Nonempty
    label: Nonempty
    enabled: bool
    reason: str | MISSING = MISSING
    subject: Nonempty | MISSING = MISSING
    destination: Nonempty | MISSING = MISSING
    operation: ActionOperation | MISSING = MISSING
    cost: ActionCost | MISSING = MISSING
    targets: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)] | MISSING = MISSING


class Selection(WireModel):
    prompt: str
    source: NamedDefinition | None
    min: Annotated[int, Field(ge=0)]
    max: Annotated[int, Field(ge=0)]
    candidates: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    selected: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]


class Offer(WireModel):
    ref: Nonempty
    kind: OfferKind
    contents: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    actions: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    price: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    stocked: bool | None | MISSING = MISSING
    on_sale: bool | None | MISSING = MISSING
    amount: Number | None | MISSING = MISSING
    requires_selection: bool | MISSING = MISSING


class EventState(WireModel):
    ref: Nonempty
    name: Nonempty
    ancient: bool | None
    definition_id: Nonempty | MISSING = MISSING


class RunState(WireModel):
    battle_turns: int | None | MISSING = MISSING
    card_rarity_offset: float
    control: StateControl
    progress: Progress
    player: RunPlayer
    relics: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    potions: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    actions: tuple[Action, ...]
    selection: Selection | None
    phase: Literal[Phase.RUN]
    screen: RunScreen
    deck: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    offers: tuple[Offer, ...]
    event: EventState | None


class CombatState(WireModel):
    control: StateControl
    progress: Progress
    player: CombatPlayer
    relics: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    potions: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    actions: tuple[Action, ...]
    selection: Selection | None
    phase: Literal[Phase.COMBAT]
    screen: CombatScreen
    deck: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    turn: Annotated[int, Field(ge=0)] | None
    piles: Piles
    enemies: tuple[Enemy, ...]
    inactive_enemies: tuple[Enemy, ...]
    orbs: Orbs | MISSING = MISSING
    allies: tuple[Ally, ...] | MISSING = MISSING


class Piles(WireModel):
    hand: tuple[Nonempty, ...]
    draw: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    discard: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    exhaust: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    other: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]


class Orbs(WireModel):
    capacity: Annotated[int, Field(ge=0)] | None
    slots: tuple[Orb, ...]


class TerminalState(WireModel):
    control: Literal[StateControl.UNAVAILABLE]
    progress: Progress
    player: Player
    relics: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    potions: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    actions: tuple[()]
    selection: None
    phase: Literal[Phase.TERMINAL]
    screen: TerminalScreen
    result: TerminalResult
    deck: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    last_observed_turn: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    enemies: tuple[Enemy, ...] | MISSING = MISSING


class Modifier(WireModel):
    definition_id: Nonempty
    name: Nonempty
    amount: Number | None
    status: Nonempty | MISSING = MISSING


class CardEntry(WireModel):
    definition_id: Nonempty
    name: Nonempty
    upgrades: Annotated[int, Field(ge=0)] | None
    card_type: Nonempty | None
    card_pool: Nonempty | None
    rarity: Nonempty | None
    is_upgradable: bool | None
    is_removable: bool | None
    keywords: tuple[Nonempty, ...]
    should_retain_this_turn: bool | None
    exhaust_on_next_play: bool | None
    has_turn_end_in_hand_effect: bool | None
    enchantment: Modifier | None
    affliction: Modifier | None
    cost: ResourceCosts
    kind: Literal[ObjectKind.CARD]
    local_keywords: tuple[Nonempty, ...] | MISSING = MISSING
    cost_without_global_effects: ResourceCosts | MISSING = MISSING
    playable: bool | None | MISSING = MISSING
    previews: tuple[TargetPreview, ...] | MISSING = MISSING


class TargetPreview(WireModel):
    target: Nonempty
    text: str


class CardDefinitionPreview(WireModel):
    definition_id: Nonempty
    name: Nonempty
    upgrades: Annotated[int, Field(ge=0)] | None
    card_type: Nonempty | None
    card_pool: Nonempty | None
    rarity: Nonempty | None
    is_upgradable: bool | None
    is_removable: bool | None
    keywords: tuple[Nonempty, ...]
    should_retain_this_turn: bool | None
    exhaust_on_next_play: bool | None
    has_turn_end_in_hand_effect: bool | None
    enchantment: Modifier | None
    affliction: Modifier | None
    cost: ResourceCosts
    text: str | None
    local_keywords: tuple[Nonempty, ...] | MISSING = MISSING
    cost_without_global_effects: ResourceCosts | MISSING = MISSING


class RelicEntry(WireModel):
    kind: Literal[ObjectKind.RELIC]
    definition_id: Nonempty
    name: Nonempty
    counter: Number | None | MISSING = MISSING
    status: Nonempty | None | MISSING = MISSING
    is_used_up: bool | None | MISSING = MISSING
    is_melted: bool | None | MISSING = MISSING
    used_this_combat: bool | None | MISSING = MISSING
    stack_count: Annotated[int, Field(ge=0)] | None | MISSING = MISSING


class PotionEntry(WireModel):
    kind: Literal[ObjectKind.POTION]
    definition_id: Nonempty
    name: Nonempty
    usable: bool | None
    needs_target: bool | None
    target_type: Nonempty | None
    usage: Nonempty | None


class OfferEntry(WireModel):
    kind: Literal[ObjectKind.OFFER]
    name: Nonempty
    definition_id: Nonempty | MISSING = MISSING


class OptionEntry(WireModel):
    kind: Literal[ObjectKind.OPTION]
    name: Nonempty
    definition_id: Nonempty | MISSING = MISSING


class EventEntry(WireModel):
    kind: Literal[ObjectKind.EVENT]
    name: Nonempty
    ancient: bool | None
    definition_id: Nonempty | MISSING = MISSING


class BundleEntry(WireModel):
    kind: Literal[ObjectKind.BUNDLE]
    name: Nonempty
    members: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    definition_id: Nonempty | MISSING = MISSING


class EnemyEntry(WireModel):
    name: Nonempty
    hp: Annotated[int, Field(ge=0)] | None
    max_hp: Annotated[int, Field(ge=0)] | None
    block: Annotated[int, Field(ge=0)] | None
    powers: tuple[Power, ...]
    definition_id: Nonempty
    intents: tuple[Intent, ...]
    hp_display: HpDisplay | MISSING = MISSING
    kind: Literal[ObjectKind.ENEMY]


class AllyEntry(WireModel):
    name: Nonempty
    hp: Annotated[int, Field(ge=0)] | None
    max_hp: Annotated[int, Field(ge=0)] | None
    block: Annotated[int, Field(ge=0)] | None
    powers: tuple[Power, ...]
    alive: bool | None
    definition_id: Nonempty | MISSING = MISSING
    hp_display: HpDisplay | MISSING = MISSING
    kind: Literal[ObjectKind.ALLY]


class UpgradeAvailable(WireModel):
    status: Literal[UpgradeStatus.AVAILABLE]
    card: CardDefinitionPreview


class UpgradeNotUpgradable(WireModel):
    status: Literal[UpgradeStatus.NOT_UPGRADABLE]


class UpgradeUnavailable(WireModel):
    status: Literal[UpgradeStatus.UNAVAILABLE]


class GeneratedCard(WireModel):
    source: NamedDefinition
    card: CardDefinitionPreview


class MechanismView(WireModel):
    kind: MechanismKind
    definition_id: Nonempty | None
    name: Nonempty
    text: str | None
    owner: Nonempty | MISSING = MISSING


class MapOdds(WireModel):
    monster: Annotated[Number, Field(ge=0, le=1)] | MISSING = MISSING
    elite: Annotated[Number, Field(ge=0, le=1)] | MISSING = MISSING
    event: Annotated[Number, Field(ge=0, le=1)] | MISSING = MISSING
    shop: Annotated[Number, Field(ge=0, le=1)] | MISSING = MISSING
    treasure: Annotated[Number, Field(ge=0, le=1)] | MISSING = MISSING

    @model_validator(mode="after")
    def require_probability(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one room probability must be present.")
        return self


class PublicMap(WireModel):
    bosses: tuple[NamedDefinition, ...]
    nodes: tuple[MapNode, ...]
    legal_destinations: tuple[Nonempty, ...] | None


class MapNode(WireModel):
    position: Nonempty
    room_type: Nonempty
    children: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    odds: MapOdds | MISSING = MISSING


class DetailData(WireModel):
    objects: Directory[ObjectEntry]
    descriptions: Directory[str | None]
    modifier_descriptions: Directory[ModifierDescriptions]
    generated: Directory[tuple[GeneratedCard, ...]]
    mechanisms: tuple[MechanismView, ...]
    upgrades: Directory[UpgradePreview] | MISSING = MISSING


class ModifierDescriptions(WireModel):
    enchantment: str | None
    affliction: str | None


class MapData(WireModel):
    map: PublicMap


class HistoryData(WireModel):
    cards_played_this_combat: tuple[NamedDefinition, ...]
    cards_played_this_turn: tuple[NamedDefinition, ...]
    cards_exhausted_this_turn: tuple[NamedDefinition, ...]


class Location(WireModel):
    game_instance_id: Nonempty
    run_id: Nonempty | None
    node_id: Nonempty | None
    owner: DecisionOwner | None

    @model_validator(mode="after")
    def check_owner(self) -> Self:
        if self.owner is not None and (self.run_id is None or self.node_id is None):
            raise ValueError("A decision owner requires a current run and node.")
        return self


class Stamp(WireModel):
    game_instance_id: Nonempty
    observation_sequence: Annotated[int, Field(ge=0)]


class Observation(WireModel):
    run_rules: RunRules
    game_language: str
    objects: Directory[ObjectEntry]
    descriptions: Directory[str | None]
    modifier_descriptions: Directory[ModifierDescriptions]
    generated: Directory[tuple[GeneratedCard, ...]]
    mechanisms: tuple[MechanismView, ...]
    stamp: Stamp
    location: Location
    input_readiness: InputReadiness
    state: State
    upgrades: Directory[UpgradePreview]
    map: PublicMap | MISSING = MISSING
    encounter: NamedDefinition | MISSING = MISSING

    @model_validator(mode="after")
    def check_publication(self) -> Self:
        if self.stamp.game_instance_id != self.location.game_instance_id:
            raise ValueError("Observation identities must describe one game instance.")
        expected_control = {
            InputReadiness.READY: StateControl.READY,
            InputReadiness.SELECTION: StateControl.READY,
            InputReadiness.BUSY: StateControl.BUSY,
            InputReadiness.TERMINAL: StateControl.UNAVAILABLE,
            InputReadiness.UNAVAILABLE: StateControl.UNAVAILABLE,
        }[self.input_readiness]
        if self.state.control is not expected_control:
            raise ValueError("Input readiness and public control disagree.")
        terminal = isinstance(self.state, TerminalState)
        if terminal != (self.input_readiness is InputReadiness.TERMINAL):
            raise ValueError("Terminal readiness requires a terminal state.")
        if self.input_readiness is InputReadiness.SELECTION and self.state.selection is None:
            raise ValueError("Selection readiness requires a published selection.")
        if self.location.run_id is None or self.location.node_id is None:
            raise ValueError("A complete observation requires a known run and node.")
        expected_owner = None if terminal else DecisionOwner(self.state.phase)
        if self.location.owner is not expected_owner:
            raise ValueError("The observation owner and phase disagree.")
        return self


class MapQuery(WireModel):
    area: Literal[InspectArea.MAP, InspectArea.COMBAT_HISTORY]


class CardQuery(WireModel):
    area: Literal[
        InspectArea.HAND,
        InspectArea.DRAW,
        InspectArea.DISCARD,
        InspectArea.EXHAUST,
        InspectArea.OTHER,
        InspectArea.DECK,
        InspectArea.OPTIONS,
        InspectArea.REWARDS,
        InspectArea.SHOP,
    ]
    refs: (
        Annotated[tuple[Nonempty, ...], Field(min_length=1), AfterValidator(_distinct)] | MISSING
    ) = MISSING
    upgrade: bool | MISSING = MISSING


class ObjectQuery(WireModel):
    area: Literal[InspectArea.RELICS, InspectArea.POTIONS, InspectArea.ENEMIES]
    refs: (
        Annotated[tuple[Nonempty, ...], Field(min_length=1), AfterValidator(_distinct)] | MISSING
    ) = MISSING


class PowersQuery(WireModel):
    area: Literal[InspectArea.POWERS]


class CatalogQuery(WireModel):
    mode: Literal[CatalogMode.CATALOG]


class SearchQuery(WireModel):
    mode: Literal[CatalogMode.SEARCH]
    category: Nonempty | MISSING = MISSING
    text: str | MISSING = MISSING
    filters: CardFilters | MISSING = MISSING
    offset: Annotated[int, Field(ge=0)] | MISSING = MISSING
    limit: Annotated[int, Field(ge=1, le=20)] | MISSING = MISSING

    @model_validator(mode="after")
    def check_filter_category(self) -> Self:
        if self.filters is not MISSING and self.category != NativeCategory.CARD:
            raise ValueError("Card filters require category=card.")
        return self


class CardFilters(WireModel):
    pools: (
        Annotated[tuple[Nonempty, ...], Field(min_length=1), AfterValidator(_distinct)] | MISSING
    ) = MISSING
    types: (
        Annotated[tuple[Nonempty, ...], Field(min_length=1), AfterValidator(_distinct)] | MISSING
    ) = MISSING
    rarities: (
        Annotated[tuple[Nonempty, ...], Field(min_length=1), AfterValidator(_distinct)] | MISSING
    ) = MISSING
    energy_costs: (
        Annotated[
            tuple[Annotated[int, Field(ge=0)] | Literal[CostSymbol.X], ...],
            Field(min_length=1),
            AfterValidator(_distinct),
        ]
        | MISSING
    ) = MISSING


class LookupQuery(WireModel):
    mode: Literal[CatalogMode.LOOKUP]
    ids: Annotated[
        tuple[Nonempty, ...], Field(min_length=1, max_length=20), AfterValidator(_distinct)
    ]


class InspectRequest(WireModel):
    query: InspectQuery


class RequestValidationError(WireModel):
    code: Literal[ErrorCode.INVALID_REQUEST]
    message: str
    fields: tuple[Nonempty, ...]


class ReadCause(WireModel):
    code: Literal[ErrorCode.NO_RUN, ErrorCode.READ_UNAVAILABLE, ErrorCode.IDENTITY_UNREGISTERED]
    message: str


class ReadError(WireModel):
    status: Literal[ResultStatus.FAILED]
    error: ReadCause


class InspectRejected(WireModel):
    status: Literal[ResultStatus.REJECTED]
    error: RequestValidationError | InvalidRefs | AreaUnavailable
    observation: Observation | None


class InvalidRefs(WireModel):
    code: Literal[ErrorCode.INVALID_REFS]
    message: str
    invalid_refs: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]


class AreaUnavailable(WireModel):
    code: Literal[ErrorCode.AREA_UNAVAILABLE]
    message: str


class InspectFailed(WireModel):
    status: Literal[ResultStatus.FAILED]
    error: InspectFailure
    observation: Observation | None


class InspectFailure(WireModel):
    code: Literal[
        ErrorCode.OBSERVATION_CHANGED, ErrorCode.READ_UNAVAILABLE, ErrorCode.IDENTITY_UNREGISTERED
    ]
    message: str


class CatalogRejected(WireModel):
    status: Literal[ResultStatus.REJECTED]
    error: RequestValidationError | CatalogRejection


class CatalogRejection(WireModel):
    code: Literal[ErrorCode.UNSUPPORTED_CATEGORY, ErrorCode.INVALID_FILTER]
    message: str


class CatalogFailed(WireModel):
    status: Literal[ResultStatus.FAILED]
    error: CatalogFailure


class CatalogFailure(WireModel):
    code: Literal[
        ErrorCode.CATALOG_UNAVAILABLE,
        ErrorCode.CATALOG_IDENTITY_CONFLICT,
        ErrorCode.NATIVE_PREVIEW_FAILED,
    ]
    message: str


class RouteChanged(WireModel):
    code: Literal[ErrorCode.ROUTE_CHANGED]
    message: str
    actual_location: Location


class ActionUnavailable(WireModel):
    code: Literal[
        ErrorCode.NATIVE_BUSY,
        ErrorCode.WRONG_OWNER,
        ErrorCode.ACTION_UNAVAILABLE,
        ErrorCode.SELECTION_REQUIRED,
        ErrorCode.SELECTION_UNAVAILABLE,
        ErrorCode.DUPLICATE_OPERATION,
    ]
    message: str


class InvalidTarget(WireModel):
    code: Literal[ErrorCode.INVALID_TARGET]
    message: str
    target: Nonempty | None
    legal_targets: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]


class InvalidSelection(WireModel):
    code: Literal[ErrorCode.INVALID_SELECTION]
    message: str
    min: Annotated[int, Field(ge=0)]
    max: Annotated[int, Field(ge=0)]


class ActionFailure(WireModel):
    code: Literal[ErrorCode.DISPATCH_FAILED, ErrorCode.IDENTITY_UNREGISTERED]
    message: str


class UnknownExecution(WireModel):
    code: Literal[ErrorCode.EXECUTION_UNCERTAIN]
    message: str


class ReceiptPending(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.PENDING]


class ReceiptCompleted(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.COMPLETED]
    boundary: Literal[InputReadiness.READY, InputReadiness.SELECTION, InputReadiness.TERMINAL]


class ReceiptRejected(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.REJECTED]
    error: ActionRejection


class ReceiptFailed(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.FAILED]
    error: ActionFailure


class ReceiptUnknown(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.UNKNOWN]
    error: UnknownExecution


class PresentationPhase(StrEnum):
    RUNNING = "running"
    SETTLED_UNVERIFIED = "settled_unverified"
    VERIFYING = "verifying"
    RESTORED = "restored"


class PresentationDrawing(StrEnum):
    PENDING = "pending"
    SHOWN = "shown"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PresentationRestoration(StrEnum):
    PENDING = "pending"
    RESTORED = "restored"
    NOT_NEEDED = "not_needed"
    UNKNOWN = "unknown"
    FAILED = "failed"


class PresentationGate(StrEnum):
    LOCKED = "locked"
    OPEN = "open"


class Presentation(WireModel):
    operation_id: Nonempty
    phase: PresentationPhase
    drawing: PresentationDrawing
    restoration: PresentationRestoration
    gate: PresentationGate

    @model_validator(mode="after")
    def check_phase(self) -> Self:
        if self.phase is PresentationPhase.RUNNING:
            valid = (
                self.restoration is PresentationRestoration.PENDING
                and self.gate is PresentationGate.LOCKED
            )
        elif self.phase is PresentationPhase.VERIFYING:
            valid = (
                self.drawing is not PresentationDrawing.PENDING
                and self.restoration is PresentationRestoration.PENDING
                and self.gate is PresentationGate.LOCKED
            )
        elif self.phase is PresentationPhase.SETTLED_UNVERIFIED:
            valid = (
                self.drawing is not PresentationDrawing.PENDING
                and self.restoration
                in (PresentationRestoration.UNKNOWN, PresentationRestoration.FAILED)
                and self.gate is PresentationGate.LOCKED
            )
        else:
            valid = (
                self.drawing is not PresentationDrawing.PENDING
                and self.restoration
                in (PresentationRestoration.RESTORED, PresentationRestoration.NOT_NEEDED)
                and self.gate is PresentationGate.OPEN
            )
        if not valid:
            raise ValueError("Inconsistent presentation phase and outcomes.")
        return self


class Status(WireModel):
    ascension: Annotated[int, Field(ge=0)] | None = None
    location: Location
    control_intent: ControlIntent
    input_readiness: InputReadiness
    receipt: StepReceipt | None
    presentation: Presentation | None

    @model_validator(mode="after")
    def check_readiness(self) -> Self:
        if (
            isinstance(self.receipt, ReceiptPending)
            and self.input_readiness is not InputReadiness.BUSY
        ):
            raise ValueError("An accepted pending operation owns native input.")
        if self.presentation is not None:
            if isinstance(self.receipt, ReceiptPending):
                raise ValueError("A pending action cannot coexist with a presentation.")
            phase = self.presentation.phase
            if (
                phase in (PresentationPhase.RUNNING, PresentationPhase.VERIFYING)
                and self.input_readiness is not InputReadiness.BUSY
            ):
                raise ValueError("A running presentation owns native input.")
            if (
                phase is PresentationPhase.SETTLED_UNVERIFIED
                and self.input_readiness is not InputReadiness.UNAVAILABLE
            ):
                raise ValueError("An unverified presentation keeps native input unavailable.")
            if (
                phase is PresentationPhase.RESTORED
                and self.input_readiness is InputReadiness.TERMINAL
            ):
                raise ValueError("A presentation cannot remain restored after the run ends.")
        if self.input_readiness is InputReadiness.TERMINAL and (
            self.location.owner is not None
            or self.location.run_id is None
            or self.location.node_id is None
        ):
            raise ValueError("Terminal input requires the last run/node and no decision owner.")
        return self


class TechnicalError(WireModel):
    status: Literal[ResultStatus.FAILED]
    error: InternalError


class InternalError(WireModel):
    code: Literal[ErrorCode.INTERNAL_ERROR]
    message: str


class InvalidRequestReply(WireModel):
    status: Literal[ResultStatus.REJECTED]
    error: RequestValidationError


class StateCompleted(WireModel):
    status: Literal[ResultStatus.COMPLETED]
    observation: Observation


class InspectCompleted(WireModel):
    status: Literal[ResultStatus.COMPLETED]
    observation: Observation
    data: InspectData | MISSING = MISSING


class NativeRelic(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    rarity: Nonempty | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativePotion(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    target_type: Nonempty | None
    usage: Nonempty | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativePower(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    type: Nonempty | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativeEnemy(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativeOrb(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativeKeyword(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativeEvent(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativeEncounter(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class NativeCharacter(WireModel):
    definition_id: Nonempty
    name: Nonempty
    text: str | None
    mechanisms: tuple[MechanismView, ...] | MISSING = MISSING
    generated: tuple[GeneratedCard, ...] | MISSING = MISSING


class CardDefinition(WireModel):
    category: Literal[NativeCategory.CARD]
    definition_id: Nonempty
    name: Nonempty
    native: CardDefinitionPreview
    upgrade: UpgradePreview


class RelicDefinition(WireModel):
    category: Literal[NativeCategory.RELIC]
    definition_id: Nonempty
    name: Nonempty
    native: NativeRelic


class PotionDefinition(WireModel):
    category: Literal[NativeCategory.POTION]
    definition_id: Nonempty
    name: Nonempty
    native: NativePotion


class PowerDefinition(WireModel):
    category: Literal[NativeCategory.POWER]
    definition_id: Nonempty
    name: Nonempty
    native: NativePower


class EnemyDefinition(WireModel):
    category: Literal[NativeCategory.ENEMY]
    definition_id: Nonempty
    name: Nonempty
    native: NativeEnemy


class OrbDefinition(WireModel):
    category: Literal[NativeCategory.ORB]
    definition_id: Nonempty
    name: Nonempty
    native: NativeOrb


class KeywordDefinition(WireModel):
    category: Literal[NativeCategory.KEYWORD]
    definition_id: Nonempty
    name: Nonempty
    native: NativeKeyword


class EventDefinition(WireModel):
    category: Literal[NativeCategory.EVENT]
    definition_id: Nonempty
    name: Nonempty
    native: NativeEvent


class EncounterDefinition(WireModel):
    category: Literal[NativeCategory.ENCOUNTER]
    definition_id: Nonempty
    name: Nonempty
    native: NativeEncounter


class CharacterDefinition(WireModel):
    category: Literal[NativeCategory.CHARACTER]
    definition_id: Nonempty
    name: Nonempty
    native: NativeCharacter


class LookupData(WireModel):
    entries: tuple[NativeDefinition, ...]
    missing_ids: tuple[Nonempty, ...]


class CardMatch(WireModel):
    category: Literal[NativeCategory.CARD]
    definition_id: Nonempty
    name: Nonempty
    description: str | None
    card_type: Nonempty | None
    rarity: Nonempty | None
    cost: ResourceCosts


class NonCardMatch(WireModel):
    category: Annotated[Nonempty, AfterValidator(_non_card_category)]
    definition_id: Nonempty
    name: Nonempty
    description: str | None


class SearchData(WireModel):
    matches: tuple[NativeMatch, ...]
    total: Annotated[int, Field(ge=0)]
    next_offset: Annotated[int, Field(ge=0)] | None


class CatalogData(WireModel):
    categories: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]
    card_filters: CardFiltersCatalog


class CardFiltersCatalog(WireModel):
    pools: tuple[PoolFilter, ...]
    types: tuple[NamedFilter, ...]
    rarities: tuple[NamedFilter, ...]
    energy_costs: Annotated[
        tuple[Annotated[int, Field(ge=0)] | Literal[CostSymbol.X], ...], AfterValidator(_distinct)
    ]


class PoolFilter(WireModel):
    value: Nonempty
    name: Nonempty
    character: NamedDefinition | MISSING = MISSING


class NamedFilter(WireModel):
    value: Nonempty
    name: Nonempty


class CatalogCompleted(WireModel):
    status: Literal[ResultStatus.COMPLETED]
    data: CatalogData | SearchData | LookupData


type Intent = HiddenIntent | PublicIntent


type State = RunState | CombatState | TerminalState


type ObjectEntry = (
    CardEntry
    | RelicEntry
    | PotionEntry
    | OfferEntry
    | OptionEntry
    | EventEntry
    | BundleEntry
    | EnemyEntry
    | AllyEntry
)


type UpgradePreview = UpgradeAvailable | UpgradeNotUpgradable | UpgradeUnavailable


type InspectData = DetailData | MapData | HistoryData


type InspectQuery = MapQuery | CardQuery | ObjectQuery | PowersQuery


type CatalogRequest = CatalogQuery | SearchQuery | LookupQuery


type InspectError = InspectRejected | InspectFailed


type CatalogError = CatalogRejected | CatalogFailed


type ActionRejection = (
    RequestValidationError
    | RouteChanged
    | ActionUnavailable
    | InvalidRefs
    | InvalidTarget
    | InvalidSelection
)


type StepReceipt = (
    ReceiptPending | ReceiptCompleted | ReceiptRejected | ReceiptFailed | ReceiptUnknown
)


type StateReply = StateCompleted | ReadError


type InspectReply = InspectCompleted | InspectError


type NativeDefinition = Annotated[
    CardDefinition
    | RelicDefinition
    | PotionDefinition
    | PowerDefinition
    | EnemyDefinition
    | OrbDefinition
    | KeywordDefinition
    | EventDefinition
    | EncounterDefinition
    | CharacterDefinition,
    Field(discriminator="category"),
]


type NativeMatch = CardMatch | NonCardMatch


type CatalogReply = CatalogCompleted | CatalogError


class RouteKey(WireModel):
    game_instance_id: Nonempty
    run_id: Nonempty
    node_id: Nonempty
    owner: DecisionOwner


class PresentationEdge(WireModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, populate_by_name=True)
    source: Nonempty = Field(alias="from")
    destination: Nonempty = Field(alias="to")


class PresentRequest(WireModel):
    operation_id: Nonempty
    expected_route: RouteKey
    edges: Annotated[tuple[PresentationEdge, ...], Field(min_length=1), AfterValidator(_distinct)]

    def to_json(self) -> str:
        return self.model_dump_json(exclude_unset=True, by_alias=True)


class VerifyPresentationRequest(WireModel):
    operation_id: Nonempty
    expected_route: RouteKey


class PresentationRejection(WireModel):
    code: Literal[
        ErrorCode.NATIVE_BUSY,
        ErrorCode.WRONG_OWNER,
        ErrorCode.ACTION_UNAVAILABLE,
        ErrorCode.DUPLICATE_OPERATION,
    ]
    message: str


class PresentationPending(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.PENDING]


class PresentationRejected(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.REJECTED]
    error: PresentationRejection | RouteChanged


class PresentationCompleted(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.COMPLETED]
    drawing: Literal[
        PresentationDrawing.SHOWN, PresentationDrawing.FAILED, PresentationDrawing.UNKNOWN
    ]
    restoration: Literal[
        PresentationRestoration.RESTORED,
        PresentationRestoration.NOT_NEEDED,
        PresentationRestoration.UNKNOWN,
        PresentationRestoration.FAILED,
    ]
    gate: PresentationGate

    @model_validator(mode="after")
    def check_gate(self) -> Self:
        restored = self.restoration in (
            PresentationRestoration.RESTORED,
            PresentationRestoration.NOT_NEEDED,
        )
        if restored != (self.gate is PresentationGate.OPEN):
            raise ValueError("Presentation restoration and gate disagree.")
        return self


class PresentationVerificationCompleted(WireModel):
    operation_id: Nonempty
    status: Literal[ResultStatus.COMPLETED]
    restoration: Literal[
        PresentationRestoration.RESTORED,
        PresentationRestoration.NOT_NEEDED,
        PresentationRestoration.UNKNOWN,
        PresentationRestoration.FAILED,
    ]
    gate: PresentationGate

    @model_validator(mode="after")
    def check_gate(self) -> Self:
        restored = self.restoration in (
            PresentationRestoration.RESTORED,
            PresentationRestoration.NOT_NEEDED,
        )
        if restored != (self.gate is PresentationGate.OPEN):
            raise ValueError("Presentation restoration and gate disagree.")
        return self


type PresentReply = Annotated[
    PresentationPending | PresentationRejected | PresentationCompleted,
    Field(discriminator="status"),
]
type VerifyPresentationReply = Annotated[
    PresentationPending | PresentationRejected | PresentationVerificationCompleted,
    Field(discriminator="status"),
]


class StepKind(StrEnum):
    PLAY_CARD = "play_card"
    USE_POTION = "use_potion"
    DISCARD_POTION = "discard_potion"
    END_TURN = "end_turn"
    INTERACT = "interact"
    SELECT = "select"


class PlayCard(WireModel):
    kind: Literal[StepKind.PLAY_CARD]
    card: Nonempty
    target: Nonempty | MISSING = MISSING


class UsePotion(WireModel):
    kind: Literal[StepKind.USE_POTION]
    potion: Nonempty
    target: Nonempty | MISSING = MISSING


class DiscardPotion(WireModel):
    kind: Literal[StepKind.DISCARD_POTION]
    potion: Nonempty


class EndTurn(WireModel):
    kind: Literal[StepKind.END_TURN]


class Interact(WireModel):
    kind: Literal[StepKind.INTERACT]
    action: Nonempty


class Select(WireModel):
    kind: Literal[StepKind.SELECT]
    refs: Annotated[tuple[Nonempty, ...], AfterValidator(_distinct)]


type NativeStep = Annotated[
    PlayCard | UsePotion | DiscardPotion | EndTurn | Interact | Select, Field(discriminator="kind")
]


class StepRequest(WireModel):
    operation_id: Nonempty
    expected_route: RouteKey
    step: NativeStep


class ControlRequest(WireModel):
    mode: ControlIntent


class ControlReply(WireModel):
    mode: ControlIntent


class ControlFailure(WireModel):
    code: Literal[ErrorCode.CONTROL_UNAVAILABLE]
    message: str


class ControlFailed(WireModel):
    status: Literal[ResultStatus.FAILED]
    error: ControlFailure
