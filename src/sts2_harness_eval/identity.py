"""Translate a verified target's logical identities at the evaluation GameIO boundary."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.client.http import InspectResult
from sts2_harness.game import (
    Admitted,
    Cataloged,
    GameFailure,
    GameIO,
    GameReply,
    Inspected,
    LifetimeEnded,
    Observed,
    Resumed,
    Settlement,
)
from sts2_harness.results import Reason

_ALIAS = "b:"
_STALE = "eval-stale:"
_REF = re.compile(r"^[a-z]+:\d+:\d+$")


@dataclass(frozen=True)
class TargetIdentity:
    source: native.Location
    loaded: native.Location
    refs: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "refs", MappingProxyType(dict(self.refs)))


class IdentityGame:
    def __init__(self, game: GameIO, identity: TargetIdentity) -> None:
        self._game = game
        self._identity = identity
        self._reverse = {value: key for key, value in identity.refs.items()}

    def _out_ref(self, value: str) -> str:
        if value in self._identity.refs:
            return self._identity.refs[value]
        if value.startswith(_ALIAS):
            return value.removeprefix(_ALIAS)
        # Unbound source names must never resolve to an unrelated B registry entry.
        return _STALE + value

    def _in_ref(self, value: str) -> str:
        if value in self._reverse:
            return self._reverse[value]
        if value.startswith(_STALE):
            return value.removeprefix(_STALE)
        return _ALIAS + value

    def _out_node(self, value: str) -> str:
        if value == self._identity.source.node_id:
            assert self._identity.loaded.node_id is not None
            return self._identity.loaded.node_id
        if value.startswith(_ALIAS):
            return value.removeprefix(_ALIAS)
        return _STALE + value

    def _in_node(self, value: str) -> str:
        if value == self._identity.loaded.node_id:
            assert self._identity.source.node_id is not None
            return self._identity.source.node_id
        if value.startswith(_STALE):
            return value.removeprefix(_STALE)
        return _ALIAS + value

    def _location(self, value: native.Location) -> native.Location:
        source, loaded = self._identity.source, self._identity.loaded
        return native.Location(
            game_instance_id=(
                source.game_instance_id
                if value.game_instance_id == loaded.game_instance_id
                else value.game_instance_id
            ),
            run_id=source.run_id if value.run_id == loaded.run_id else value.run_id,
            node_id=self._in_node(value.node_id) if value.node_id is not None else None,
            owner=value.owner,
        )

    def _route(self, value: native.RouteKey) -> native.RouteKey:
        source, loaded = self._identity.source, self._identity.loaded
        assert loaded.run_id is not None
        return native.RouteKey(
            game_instance_id=(
                loaded.game_instance_id
                if value.game_instance_id == source.game_instance_id
                else value.game_instance_id
            ),
            run_id=loaded.run_id if value.run_id == source.run_id else value.run_id,
            node_id=self._out_node(value.node_id),
            owner=value.owner,
        )

    def _step(self, value: native.NativeStep) -> native.NativeStep:
        if isinstance(value, (native.PlayCard, native.UsePotion)):
            target = self._out_ref(value.target) if value.target is not MISSING else MISSING
            if isinstance(value, native.PlayCard):
                return value.model_copy(
                    update={"card": self._out_ref(value.card), "target": target}
                )
            return value.model_copy(
                update={"potion": self._out_ref(value.potion), "target": target}
            )
        if isinstance(value, native.DiscardPotion):
            return value.model_copy(update={"potion": self._out_ref(value.potion)})
        if isinstance(value, native.Interact):
            return value.model_copy(update={"action": self._out_ref(value.action)})
        if isinstance(value, native.Select):
            return value.model_copy(update={"refs": tuple(map(self._out_ref, value.refs))})
        return value

    def _mechanism(self, value: native.MechanismView) -> native.MechanismView:
        if value.owner is MISSING or _REF.fullmatch(value.owner) is None:
            return value
        return value.model_copy(update={"owner": self._in_ref(value.owner)})

    def _object(self, value: native.ObjectEntry) -> native.ObjectEntry:
        if isinstance(value, native.BundleEntry):
            return value.model_copy(update={"members": tuple(map(self._in_ref, value.members))})
        if isinstance(value, native.CardEntry) and value.previews is not MISSING:
            return value.model_copy(
                update={
                    "previews": tuple(
                        preview.model_copy(update={"target": self._in_ref(preview.target)})
                        for preview in value.previews
                    )
                }
            )
        return value

    def _directory[T](self, values: Mapping[str, T]) -> Mapping[str, T]:
        return MappingProxyType({self._in_ref(key): value for key, value in values.items()})

    def _selection(self, value: native.Selection | None) -> native.Selection | None:
        if value is None:
            return None
        return value.model_copy(
            update={
                "candidates": tuple(map(self._in_ref, value.candidates)),
                "selected": tuple(map(self._in_ref, value.selected)),
            }
        )

    def _action(self, value: native.Action) -> native.Action:
        return value.model_copy(
            update={
                "ref": self._in_ref(value.ref),
                "subject": self._in_ref(value.subject) if value.subject is not MISSING else MISSING,
                "targets": tuple(map(self._in_ref, value.targets))
                if value.targets is not MISSING
                else MISSING,
            }
        )

    def _state(self, value: native.State) -> native.State:
        relics = tuple(map(self._in_ref, value.relics))
        potions = tuple(map(self._in_ref, value.potions))
        if isinstance(value, native.TerminalState):
            return value.model_copy(
                update={
                    "relics": relics,
                    "potions": potions,
                    "deck": tuple(map(self._in_ref, value.deck)),
                    "enemies": tuple(
                        enemy.model_copy(update={"ref": self._in_ref(enemy.ref)})
                        for enemy in value.enemies
                    )
                    if value.enemies is not MISSING
                    else MISSING,
                }
            )
        actions = tuple(self._action(action) for action in value.actions)
        selection = self._selection(value.selection)
        if isinstance(value, native.RunState):
            return value.model_copy(
                update={
                    "relics": relics,
                    "potions": potions,
                    "actions": actions,
                    "selection": selection,
                    "deck": tuple(map(self._in_ref, value.deck)),
                    "event": value.event.model_copy(update={"ref": self._in_ref(value.event.ref)})
                    if value.event is not None
                    else None,
                    "offers": tuple(
                        offer.model_copy(
                            update={
                                "ref": self._in_ref(offer.ref),
                                "contents": tuple(map(self._in_ref, offer.contents)),
                                "actions": tuple(map(self._in_ref, offer.actions)),
                            }
                        )
                        for offer in value.offers
                    ),
                }
            )
        piles = value.piles
        return value.model_copy(
            update={
                "relics": relics,
                "potions": potions,
                "actions": actions,
                "selection": selection,
                "piles": native.Piles(
                    hand=tuple(map(self._in_ref, piles.hand)),
                    draw=tuple(map(self._in_ref, piles.draw)),
                    discard=tuple(map(self._in_ref, piles.discard)),
                    exhaust=tuple(map(self._in_ref, piles.exhaust)),
                    other=tuple(map(self._in_ref, piles.other)),
                ),
                "enemies": tuple(
                    enemy.model_copy(update={"ref": self._in_ref(enemy.ref)})
                    for enemy in value.enemies
                ),
                "inactive_enemies": tuple(
                    enemy.model_copy(update={"ref": self._in_ref(enemy.ref)})
                    for enemy in value.inactive_enemies
                ),
                "allies": tuple(
                    ally.model_copy(update={"ref": self._in_ref(ally.ref)}) for ally in value.allies
                )
                if value.allies is not MISSING
                else MISSING,
            }
        )

    def observation(self, value: native.Observation) -> native.Observation:
        location = self._location(value.location)
        return value.model_copy(
            update={
                "location": location,
                "stamp": value.stamp.model_copy(
                    update={"game_instance_id": location.game_instance_id}
                ),
                "state": self._state(value.state),
                "objects": MappingProxyType(
                    {self._in_ref(key): self._object(item) for key, item in value.objects.items()}
                ),
                "descriptions": self._directory(value.descriptions),
                "modifier_descriptions": self._directory(value.modifier_descriptions),
                "generated": self._directory(value.generated),
                "upgrades": self._directory(value.upgrades),
                "mechanisms": tuple(map(self._mechanism, value.mechanisms)),
            }
        )

    def _detail(self, value: native.DetailData) -> native.DetailData:
        return value.model_copy(
            update={
                "objects": MappingProxyType(
                    {self._in_ref(key): self._object(item) for key, item in value.objects.items()}
                ),
                "descriptions": self._directory(value.descriptions),
                "modifier_descriptions": self._directory(value.modifier_descriptions),
                "generated": self._directory(value.generated),
                "upgrades": self._directory(value.upgrades)
                if value.upgrades is not MISSING
                else MISSING,
                "mechanisms": tuple(map(self._mechanism, value.mechanisms)),
            }
        )

    def _inspection(self, value: InspectResult) -> InspectResult:
        if isinstance(value, native.InspectCompleted):
            data = (
                self._detail(value.data)
                if isinstance(value.data, native.DetailData)
                else value.data
            )
            return value.model_copy(
                update={"observation": self.observation(value.observation), "data": data}
            )
        if isinstance(value, native.InspectRejected):
            error = value.error
            if isinstance(error, native.InvalidRefs):
                error = error.model_copy(
                    update={"invalid_refs": tuple(map(self._in_ref, error.invalid_refs))}
                )
            return value.model_copy(
                update={
                    "observation": self.observation(value.observation)
                    if value.observation is not None
                    else None,
                    "error": error,
                }
            )
        if isinstance(value, native.InspectFailed):
            return value.model_copy(
                update={
                    "observation": self.observation(value.observation)
                    if value.observation is not None
                    else None
                }
            )
        return value

    def _receipt(self, value: native.ReceiptRejected) -> native.ReceiptRejected:
        error = value.error
        if isinstance(error, native.InvalidRefs):
            error = error.model_copy(
                update={"invalid_refs": tuple(map(self._in_ref, error.invalid_refs))}
            )
        elif isinstance(error, native.InvalidTarget):
            error = error.model_copy(
                update={
                    "target": self._in_ref(error.target) if error.target is not None else None,
                    "legal_targets": tuple(map(self._in_ref, error.legal_targets)),
                }
            )
        elif isinstance(error, native.RouteChanged):
            error = error.model_copy(
                update={"actual_location": self._location(error.actual_location)}
            )
        return value.model_copy(update={"error": error})

    def _reply[T](self, reply: GameReply[T], outcome: T | GameFailure) -> GameReply[T]:
        if isinstance(outcome, LifetimeEnded) and outcome.location is not None:
            outcome = outcome.model_copy(update={"location": self._location(outcome.location)})
        return GameReply[T](
            outcome=outcome, observations=tuple(map(self.observation, reply.observations))
        )

    async def admit(self) -> GameReply[Admitted]:
        reply = await self._game.admit()
        return self._reply(reply, reply.outcome)

    async def observe(self) -> GameReply[Observed]:
        reply = await self._game.observe()
        outcome = reply.outcome
        if isinstance(outcome, Observed):
            outcome = Observed(observation=self.observation(outcome.observation))
        return self._reply(reply, outcome)

    async def inspect(self, query: native.InspectQuery) -> GameReply[Inspected]:
        if isinstance(query, (native.CardQuery, native.ObjectQuery)) and query.refs is not MISSING:
            query = query.model_copy(update={"refs": tuple(map(self._out_ref, query.refs))})
        reply = await self._game.inspect(query)
        outcome = reply.outcome
        if isinstance(outcome, Inspected):
            outcome = Inspected(result=self._inspection(outcome.result))
        return self._reply(reply, outcome)

    async def catalog(self, request: native.CatalogRequest) -> GameReply[Cataloged]:
        reply = await self._game.catalog(request)
        return self._reply(reply, reply.outcome)

    async def settle(
        self, route: native.RouteKey, step: native.NativeStep
    ) -> GameReply[Settlement]:
        reply = await self._game.settle(self._route(route), self._step(step))
        outcome = reply.outcome
        if isinstance(outcome, Settlement):
            receipt = (
                self._receipt(outcome.receipt)
                if isinstance(outcome.receipt, native.ReceiptRejected)
                else outcome.receipt
            )
            outcome = Settlement(
                request=native.StepRequest(
                    operation_id=outcome.request.operation_id, expected_route=route, step=step
                ),
                receipt=receipt,
            )
        return self._reply(reply, outcome)

    async def pause_until_resumed(self, reason: Reason | None) -> GameReply[Resumed]:
        reply = await self._game.pause_until_resumed(reason)
        outcome = reply.outcome
        if isinstance(outcome, Resumed):
            outcome = Resumed(observation=self.observation(outcome.observation))
        return self._reply(reply, outcome)
