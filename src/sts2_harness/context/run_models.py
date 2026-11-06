"""Typed Run observation sections and context-local catalog entries."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import MISSING, ConfigDict, Field, model_validator

from sts2_harness.client import models as native
from sts2_harness.context.map_projection import DynamicMap, MapTopology
from sts2_harness.results import SemanticValue


class Reference(SemanticValue):
    ref: native.Nonempty
    name: native.Nonempty | MISSING = MISSING
    definition_id: native.Nonempty | MISSING = MISSING


class ModifierIdentity(SemanticValue):
    definition_id: native.Nonempty
    name: native.Nonempty


class ObjectProfile(SemanticValue):
    name: native.Nonempty
    definition_id: native.Nonempty | MISSING = MISSING


class CardProfile(SemanticValue):
    definition_id: native.Nonempty
    name: native.Nonempty
    upgrades: Annotated[int, Field(ge=0)] | None
    card_type: str | None
    card_pool: str | None
    rarity: str | None
    enchantment: ModifierIdentity | MISSING = MISSING
    affliction: ModifierIdentity | MISSING = MISSING


class MechanismBody(SemanticValue):
    kind: native.MechanismKind
    definition_id: str | None
    name: native.Nonempty
    text: str | None


class ProfileKind(StrEnum):
    MECHANISM = "mechanism"


class ProfileSchema(StrEnum):
    CARD = "card/1"
    RELIC = "relic/1"
    POTION = "potion/1"
    OFFER = "offer/1"
    EVENT = "event/1"
    OPTION = "option/1"
    BUNDLE = "bundle/1"
    MECHANISM = "mechanism/1"
    ENEMY = "enemy/1"
    ALLY = "ally/1"


type RunObjectKind = Literal[
    native.ObjectKind.CARD,
    native.ObjectKind.RELIC,
    native.ObjectKind.POTION,
    native.ObjectKind.OFFER,
    native.ObjectKind.EVENT,
    native.ObjectKind.OPTION,
    native.ObjectKind.BUNDLE,
    native.ObjectKind.ENEMY,
    native.ObjectKind.ALLY,
]


class Profile(SemanticValue):
    model_config = ConfigDict(serialize_by_alias=True)

    kind: RunObjectKind | ProfileKind
    language: str
    schema_: ProfileSchema = Field(alias="schema")
    content: CardProfile | ObjectProfile | MechanismBody

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.schema_.value != f"{self.kind}/1":
            raise ValueError("Profile kind and schema must match.")
        if self.kind == native.ObjectKind.CARD:
            if not isinstance(self.content, CardProfile):
                raise ValueError("Card profiles require card content.")
        elif self.kind == ProfileKind.MECHANISM:
            if not isinstance(self.content, MechanismBody):
                raise ValueError("Mechanism profiles require a mechanism body.")
        elif type(self.content) is not ObjectProfile:
            raise ValueError("Object profiles require object content.")
        return self


class Binding(Reference):
    kind: RunObjectKind
    profile: native.Nonempty


class Scene(SemanticValue):
    game_instance_id: str
    run_id: str | None
    node_id: str | None
    bindings: tuple[Binding, ...]
    reset: Literal[True] | MISSING = MISSING
    removed: tuple[native.Nonempty, ...] | MISSING = MISSING


class Inventory(SemanticValue):
    relics: tuple[Reference, ...]
    potions: tuple[Reference, ...]


class TargetPreview(SemanticValue):
    target: Reference
    text: str


class Card(SemanticValue):
    ref: native.Nonempty
    definition_id: native.Nonempty
    name: native.Nonempty
    keywords: tuple[str, ...]
    local_keywords: tuple[str, ...] | MISSING = MISSING
    is_upgradable: bool | None
    is_removable: bool | None
    cost: native.ResourceCosts
    cost_without_global_effects: native.ResourceCosts | MISSING = MISSING
    playable: bool | None | MISSING = MISSING
    should_retain_this_turn: bool | None | MISSING = MISSING
    exhaust_on_next_play: bool | None | MISSING = MISSING
    has_turn_end_in_hand_effect: bool | None | MISSING = MISSING
    enchantment: native.Modifier | MISSING = MISSING
    affliction: native.Modifier | MISSING = MISSING


class Relic(native.RelicEntry):
    ref: native.Nonempty


class Potion(native.PotionEntry):
    ref: native.Nonempty


class OfferObject(native.OfferEntry):
    ref: native.Nonempty


class Option(native.OptionEntry):
    ref: native.Nonempty


class Event(native.EventEntry):
    ref: native.Nonempty


class Bundle(SemanticValue):
    ref: native.Nonempty
    kind: Literal[native.ObjectKind.BUNDLE]
    name: native.Nonempty
    definition_id: native.Nonempty | MISSING = MISSING
    members: tuple[Reference, ...]


type NonCard = Relic | Potion | OfferObject | Option | Event | Bundle


class Description(Reference):
    text: str | None


class ModifierDescription(Reference):
    enchantment: str | None | MISSING = MISSING
    affliction: str | None | MISSING = MISSING


class MechanismBinding(SemanticValue):
    owner: Reference | None
    profile: str
    definition_id: str | None
    name: native.Nonempty


class CardPreview(SemanticValue):
    definition_id: native.Nonempty
    name: native.Nonempty
    upgrades: int | None
    card_type: native.Nonempty | None
    card_pool: native.Nonempty | None
    rarity: native.Nonempty | None
    is_upgradable: bool | None
    is_removable: bool | None
    keywords: tuple[native.Nonempty, ...]
    local_keywords: tuple[native.Nonempty, ...] | MISSING = MISSING
    should_retain_this_turn: bool | None | MISSING = MISSING
    exhaust_on_next_play: bool | None | MISSING = MISSING
    has_turn_end_in_hand_effect: bool | None | MISSING = MISSING
    enchantment: native.Modifier | MISSING = MISSING
    affliction: native.Modifier | MISSING = MISSING
    cost: native.ResourceCosts
    cost_without_global_effects: native.ResourceCosts | MISSING = MISSING
    text: str | None


class UpgradeAvailable(SemanticValue):
    status: Literal[native.UpgradeStatus.AVAILABLE]
    source: Reference
    card: CardPreview


class UpgradeNotUpgradable(native.UpgradeNotUpgradable):
    source: Reference


class UpgradeUnavailable(native.UpgradeUnavailable):
    source: Reference


type Upgrade = UpgradeAvailable | UpgradeNotUpgradable | UpgradeUnavailable


class GeneratedPreview(SemanticValue):
    source: native.NamedDefinition
    card: CardPreview


class Generated(SemanticValue):
    source: Reference
    previews: tuple[GeneratedPreview, ...]


class HandCard(Reference):
    playable: bool | None | MISSING = MISSING
    previews: tuple[TargetPreview, ...] | MISSING = MISSING


class Actors(SemanticValue):
    enemies: tuple[native.Enemy, ...]
    inactive_enemies: tuple[native.Enemy, ...]
    allies: tuple[native.Ally, ...] | MISSING = MISSING


class RemovedEntries(SemanticValue):
    cards: tuple[native.Nonempty, ...] | MISSING = MISSING
    noncard_objects: tuple[native.Nonempty, ...] | MISSING = MISSING
    object_descriptions: tuple[native.Nonempty, ...] | MISSING = MISSING
    card_descriptions: tuple[native.Nonempty, ...] | MISSING = MISSING
    modifier_descriptions: tuple[native.Nonempty, ...] | MISSING = MISSING
    mechanisms: tuple[MechanismBinding, ...] | MISSING = MISSING
    upgrades: tuple[native.Nonempty, ...] | MISSING = MISSING
    generated: tuple[native.Nonempty, ...] | MISSING = MISSING


class Current(SemanticValue):
    removed: RemovedEntries | MISSING = MISSING
    map_topology: MapTopology | None | MISSING = MISSING
    dynamic_map: DynamicMap | None | MISSING = MISSING
    progress: native.Progress | MISSING = MISSING
    player: native.CombatPlayer | native.Player | native.RunPlayer | MISSING = MISSING
    turn: int | None | MISSING = MISSING
    hand: tuple[HandCard, ...] | None | MISSING = MISSING
    draw: tuple[Reference, ...] | None | MISSING = MISSING
    discard: tuple[Reference, ...] | None | MISSING = MISSING
    exhaust: tuple[Reference, ...] | None | MISSING = MISSING
    other: tuple[Reference, ...] | None | MISSING = MISSING
    actors: Actors | None | MISSING = MISSING
    orbs: native.Orbs | None | MISSING = MISSING
    inventory: Inventory | MISSING = MISSING
    deck: tuple[Reference, ...] | MISSING = MISSING
    cards: tuple[Card, ...] | MISSING = MISSING
    noncard_objects: tuple[NonCard, ...] | MISSING = MISSING
    object_descriptions: tuple[Description, ...] | MISSING = MISSING
    card_descriptions: tuple[Description, ...] | MISSING = MISSING
    modifier_descriptions: tuple[ModifierDescription, ...] | MISSING = MISSING
    mechanisms: tuple[MechanismBinding, ...] | MISSING = MISSING
    upgrades: tuple[Upgrade, ...] | MISSING = MISSING
    generated: tuple[Generated, ...] | MISSING = MISSING
    encounter: native.NamedDefinition | None | MISSING = MISSING
    result: native.TerminalResult | None | MISSING = MISSING
    last_observed_turn: int | None | MISSING = MISSING


class Action(SemanticValue):
    ref: native.Nonempty
    label: native.Nonempty
    enabled: bool
    reason: str | MISSING = MISSING
    subject: Reference | str | MISSING = MISSING
    destination: native.Nonempty | MISSING = MISSING
    operation: native.ActionOperation | MISSING = MISSING
    cost: native.ActionCost | MISSING = MISSING
    targets: tuple[Reference, ...] | MISSING = MISSING


class Selection(SemanticValue):
    prompt: str
    source: native.NamedDefinition | None
    min: Annotated[int, Field(ge=0)]
    max: Annotated[int, Field(ge=0)]
    candidates: tuple[Reference, ...]
    selected: tuple[Reference, ...]


class Offer(SemanticValue):
    ref: Reference
    kind: native.OfferKind
    contents: tuple[Reference, ...]
    actions: tuple[str, ...]
    price: Annotated[int, Field(ge=0)] | None | MISSING = MISSING
    stocked: bool | None | MISSING = MISSING
    on_sale: bool | None | MISSING = MISSING
    amount: native.Number | None | MISSING = MISSING


class Decision(SemanticValue):
    phase: native.Phase
    screen: native.RunScreen | native.CombatScreen | native.TerminalScreen
    control: native.StateControl
    actions: tuple[Action, ...] | MISSING = MISSING
    selection: Selection | None
    offers: tuple[Offer, ...] | MISSING = MISSING
    event: native.EventState | None | MISSING = MISSING


class RunObservation(SemanticValue):
    catalog_additions: dict[str, Profile] | MISSING = MISSING
    scene: Scene | MISSING = MISSING
    current: Current | MISSING = MISSING
    decision: Decision
