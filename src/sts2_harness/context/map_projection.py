"""Project native map facts and the accepted route into complete sections."""

from enum import StrEnum

from pydantic import MISSING, AliasChoices, ConfigDict, Field

from sts2_harness.client import models as native
from sts2_harness.results import SemanticValue
from sts2_harness.route_plan import RoutePlan


class MapNode(SemanticValue):
    id: native.Nonempty
    room_type: native.Nonempty
    children: tuple[native.Nonempty, ...]


class MapTopology(SemanticValue):
    bosses: tuple[native.NamedDefinition, ...]
    nodes: tuple[MapNode, ...]


class SpecialMovement(SemanticValue):
    free_travel_active: bool | None
    remaining_uses: int | None


class PlanEdge(SemanticValue):
    model_config = ConfigDict(serialize_by_alias=True)

    source: str = Field(validation_alias=AliasChoices("source", "from"), serialization_alias="from")
    destination: str = Field(
        validation_alias=AliasChoices("destination", "to"), serialization_alias="to"
    )


class Plan(SemanticValue):
    edges: tuple[PlanEdge, ...]


class AheadKind(StrEnum):
    FIGHTS = "fights"
    ELITES = "elites"
    UNKNOWN = "unknown"
    RESTS = "rests"
    SHOPS = "shops"


class DynamicMap(SemanticValue):
    position: str | None
    legal_destinations: tuple[str, ...] | None
    special_movement: SpecialMovement
    plan: Plan | None
    ahead: dict[AheadKind, int] | MISSING = MISSING


def _ahead(
    public_map: native.PublicMap, progress: native.Progress, plan: RoutePlan | None
) -> dict[AheadKind, int] | MISSING:
    if plan is None or plan.position != progress.position:
        return MISSING
    nodes = {node.position: node for node in public_map.nodes}
    outgoing: dict[str, list[str]] = {}
    for edge in plan.edges:
        outgoing.setdefault(edge.source, []).append(edge.destination)
    kinds = {
        "monster": AheadKind.FIGHTS,
        "elite": AheadKind.ELITES,
        "unknown": AheadKind.UNKNOWN,
        "restsite": AheadKind.RESTS,
        "shop": AheadKind.SHOPS,
    }
    counts: dict[AheadKind, int] = {}
    position = plan.position
    while True:
        destinations = outgoing.get(position, [])
        if len(destinations) != 1:
            return MISSING
        node = nodes.get(destinations[0])
        if node is None:
            return MISSING
        if node.room_type == "boss":
            return counts
        if (kind := kinds.get(node.room_type)) is not None:
            counts[kind] = counts.get(kind, 0) + 1
        position = node.position


def project_map(
    public_map: native.PublicMap, progress: native.Progress, plan: RoutePlan | None
) -> tuple[MapTopology, DynamicMap, dict[str, native.MapOdds]]:
    topology = MapTopology(
        bosses=public_map.bosses,
        nodes=tuple(
            MapNode(id=node.position, room_type=node.room_type, children=node.children)
            for node in reversed(public_map.nodes)
        ),
    )
    dynamic = DynamicMap(
        position=progress.position,
        ahead=_ahead(public_map, progress, plan),
        legal_destinations=public_map.legal_destinations,
        special_movement=SpecialMovement(free_travel_active=None, remaining_uses=None),
        plan=(
            Plan(
                edges=tuple(
                    PlanEdge(source=edge.source, destination=edge.destination)
                    for edge in plan.edges
                ),
            )
            if plan is not None
            else None
        ),
    )
    unknown_odds = {
        node.position: node.odds
        for node in public_map.nodes
        if isinstance(node.odds, native.MapOdds)
    }
    return topology, dynamic, unknown_odds
