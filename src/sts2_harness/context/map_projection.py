"""Project the native act map and accepted route into connected stretches."""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import MISSING

from sts2_harness.client import models as native
from sts2_harness.results import SemanticValue
from sts2_harness.route_plan import Edge, PlanRejection, PlanStatus, PlanUpdate, RoutePlan


class StretchView(SemanticValue):
    id: str
    floors: str
    rooms: str
    next: tuple[str, ...]


class MapPosition(SemanticValue):
    node: str
    stretch: str
    room: int


class MapView(SemanticValue):
    boss: tuple[native.NamedDefinition, ...]
    at: MapPosition | None
    next: tuple[str, ...] | None
    stretches: tuple[StretchView, ...]
    unknown_odds: native.MapOdds | dict[str, dict[str, native.MapOdds | None]] | None
    plan: tuple[str, ...] | None


class MapRoom(StrEnum):
    MONSTER = "monster"
    ELITE = "elite"
    REST = "restsite"
    SHOP = "shop"
    TREASURE = "treasure"
    UNKNOWN = "unknown"
    BOSS = "boss"
    ANCIENT = "ancient"


class MapUnavailable(ValueError):
    """The supplied native map cannot support a trustworthy stretch projection."""


_ROOM_CODES = {
    MapRoom.MONSTER: "M",
    MapRoom.ELITE: "E",
    MapRoom.REST: "R",
    MapRoom.SHOP: "$",
    MapRoom.TREASURE: "T",
    MapRoom.UNKNOWN: "?",
    MapRoom.BOSS: "B",
    MapRoom.ANCIENT: "A",
}


def _coordinates(position: str) -> tuple[int, int]:
    row, col = position.removeprefix("map:").split(":")
    return int(row), int(col)


@dataclass(frozen=True, slots=True)
class Stretch:
    id: str
    nodes: tuple[native.MapNode, ...]
    next: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StretchMap:
    stretches: tuple[Stretch, ...]
    by_node: dict[str, Stretch]


def split_stretches(public_map: native.PublicMap) -> StretchMap:
    nodes = {node.position: node for node in public_map.nodes}
    if len(nodes) != len(public_map.nodes):
        raise MapUnavailable("The native map contains duplicate positions.")
    try:
        for node in public_map.nodes:
            _coordinates(node.position)
            MapRoom(node.room_type)
    except ValueError as error:
        raise MapUnavailable("The native map has an unknown position or room type.") from error
    incoming: dict[str, list[str]] = {position: [] for position in nodes}
    for node in public_map.nodes:
        for child in node.children:
            if child not in incoming:
                raise MapUnavailable("The native map has a missing destination.")
            incoming[child].append(node.position)
    if public_map.legal_destinations is not None and any(
        destination not in nodes for destination in public_map.legal_destinations
    ):
        raise MapUnavailable("The native map has an unknown legal destination.")
    counts = {position: len(parents) for position, parents in incoming.items()}
    pending = [position for position, count in counts.items() if count == 0]
    visited = 0
    while pending:
        position = pending.pop()
        visited += 1
        for child in nodes[position].children:
            counts[child] -= 1
            if counts[child] == 0:
                pending.append(child)
    if visited != len(nodes):
        raise MapUnavailable("The native map contains a cycle.")
    starts = sorted(
        (
            node.position
            for node in public_map.nodes
            if len(incoming[node.position]) != 1
            or len(nodes[incoming[node.position][0]].children) != 1
        ),
        key=_coordinates,
    )
    chains: list[tuple[native.MapNode, ...]] = []
    assigned: set[str] = set()
    for start in starts:
        chain = [nodes[start]]
        assigned.add(start)
        while len(chain[-1].children) == 1:
            child = chain[-1].children[0]
            if len(incoming[child]) != 1:
                break
            if child in assigned:
                raise MapUnavailable("The native map contains a cycle.")
            chain.append(nodes[child])
            assigned.add(child)
        chains.append(tuple(chain))
    if assigned != nodes.keys():
        raise MapUnavailable("The native map contains a cycle.")
    ids = {node.position: f"s{index}" for index, chain in enumerate(chains, 1) for node in chain}
    stretches = tuple(
        Stretch(
            id=f"s{index}",
            nodes=chain,
            next=tuple(ids[child] for child in chain[-1].children if child in ids),
        )
        for index, chain in enumerate(chains, 1)
    )
    return StretchMap(
        stretches=stretches,
        by_node={node.position: stretch for stretch in stretches for node in stretch.nodes},
    )


def _entry(graph: StretchMap, position: str | None, destination: str) -> str:
    stretch = graph.by_node.get(destination)
    if stretch is None:
        return destination
    if destination == stretch.nodes[0].position:
        return stretch.id
    current = graph.by_node.get(position) if position is not None else None
    if current == stretch and any(
        node.position == position and destination in node.children for node in stretch.nodes
    ):
        return stretch.id
    return destination


def _route_nodes(plan: RoutePlan | None, position: str | None) -> tuple[str, ...] | None:
    if plan is None or plan.position != position:
        return None
    outgoing: dict[str | None, list[str]] = {}
    for edge in plan.edges:
        outgoing.setdefault(edge.source, []).append(edge.destination)
    route: list[str] = []
    current = position
    while current in outgoing:
        destinations = outgoing[current]
        if len(destinations) != 1 or destinations[0] in route:
            return None
        current = destinations[0]
        route.append(current)
    return tuple(route) if route else None


def replace_stretches(
    previous: RoutePlan | None,
    public_map: native.PublicMap,
    position: str | None,
    reason: str,
    stretches: tuple[str, ...],
) -> PlanUpdate:
    if not stretches:
        return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.EMPTY)
    if public_map.legal_destinations is None:
        return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.DESTINATIONS_UNKNOWN)
    graph = split_stretches(public_map)
    if position is not None and position not in graph.by_node:
        return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.UNKNOWN_POSITION)
    legal = [
        destination
        for destination in public_map.legal_destinations
        if _entry(graph, position, destination) == stretches[0]
    ]
    if not legal:
        return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.TRAVEL_NOT_AVAILABLE)
    if len(legal) != 1:
        return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.AMBIGUOUS_NEXT)
    first = graph.by_node.get(legal[0])
    if first is None:
        return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.UNKNOWN_NODE)
    by_id = {stretch.id: stretch for stretch in graph.stretches}
    selected = [first]
    for identity in stretches[1:]:
        stretch = by_id.get(identity)
        if stretch is None:
            return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.UNKNOWN_NODE)
        if identity not in selected[-1].next:
            return PlanUpdate(PlanStatus.REJECTED, previous, PlanRejection.DISCONNECTED)
        selected.append(stretch)
    first_index = next(i for i, node in enumerate(first.nodes) if node.position == legal[0])
    nodes = [node.position for node in first.nodes[first_index:]]
    for stretch in selected[1:]:
        nodes.extend(node.position for node in stretch.nodes)
    sources = (position, *nodes[:-1])
    edges = tuple(
        Edge(source, destination) for source, destination in zip(sources, nodes, strict=True)
    )
    return PlanUpdate(PlanStatus.ACCEPTED, RoutePlan(position, reason, edges))


def restore_stretches(
    public_map: native.PublicMap, position: str | None, stretches: tuple[str, ...]
) -> PlanUpdate:
    if not stretches:
        return PlanUpdate(PlanStatus.REJECTED, None, PlanRejection.EMPTY)
    graph = split_stretches(public_map)
    selected = next((item for item in graph.stretches if item.id == stretches[0]), None)
    if selected is None:
        destination = stretches[0]
    elif position is not None and graph.by_node.get(position) == selected:
        index = next(i for i, node in enumerate(selected.nodes) if node.position == position) + 1
        if index >= len(selected.nodes):
            return PlanUpdate(PlanStatus.REJECTED, None, PlanRejection.DISCONNECTED)
        destination = selected.nodes[index].position
    else:
        destination = selected.nodes[0].position
    # The recorded accepted plan can remain active while current travel is blocked.
    restored_map = public_map.model_copy(update={"legal_destinations": (destination,)})
    return replace_stretches(None, restored_map, position, "", stretches)


def planned_stretches(
    graph: StretchMap, position: str | None, plan: RoutePlan | None
) -> tuple[str, ...] | None:
    route = _route_nodes(plan, position)
    if route is None:
        return None
    result: list[str] = []
    expected: list[str] = []
    previous: Stretch | None = None
    for index, node in enumerate(route):
        stretch = graph.by_node.get(node)
        if stretch is None:
            return None
        if stretch != previous:
            result.append(_entry(graph, position, node) if index == 0 else stretch.id)
            start = next(i for i, item in enumerate(stretch.nodes) if item.position == node)
            if index and start != 0:
                return None
            if previous is not None and stretch.id not in previous.next:
                return None
            expected.extend(item.position for item in stretch.nodes[start:])
        previous = stretch
    return tuple(result) if tuple(expected) == route else None


def planned_route(
    public_map: native.PublicMap, progress: native.Progress, plan: RoutePlan | None
) -> tuple[str, ...] | MISSING:
    route = _route_nodes(plan, progress.position)
    if route is None:
        return MISSING
    graph = split_stretches(public_map)
    groups: list[str] = []
    previous: Stretch | None = None
    for node in route:
        stretch = graph.by_node.get(node)
        if stretch is None:
            return MISSING
        room = next(item.room_type for item in stretch.nodes if item.position == node)
        code = _ROOM_CODES[MapRoom(room)]
        if stretch != previous:
            groups.append(code)
        else:
            groups[-1] += code
        previous = stretch
    return tuple(groups)


def project_map(
    public_map: native.PublicMap, progress: native.Progress, plan: RoutePlan | None
) -> MapView:
    graph = split_stretches(public_map)
    nodes = {node.position: node for node in public_map.nodes}
    position = progress.position
    current = graph.by_node.get(position) if position is not None else None
    if position is not None and current is None:
        raise MapUnavailable("The current position is absent from the native map.")
    at = (
        MapPosition(
            node=position,
            stretch=current.id,
            room=next(
                index for index, node in enumerate(current.nodes, 1) if node.position == position
            ),
        )
        if position is not None and current is not None
        else None
    )
    children = {child for node in public_map.nodes for child in node.children}
    roots = (
        list(nodes[position].children)
        if position is not None and position in nodes
        else [node.position for node in public_map.nodes if node.position not in children]
        if position is None
        else []
    )
    if public_map.legal_destinations is not None:
        roots.extend(public_map.legal_destinations)
    reached: set[str] = set()
    pending = roots
    while pending:
        node = pending.pop()
        if node in reached or node not in nodes:
            continue
        reached.add(node)
        pending.extend(nodes[node].children)
    visible = tuple(
        stretch
        for stretch in graph.stretches
        if any(node.position in reached for node in stretch.nodes)
    )
    odds: dict[str, dict[str, native.MapOdds | None]] = {}
    for stretch in visible:
        values = {
            str(index): node.odds if isinstance(node.odds, native.MapOdds) else None
            for index, node in enumerate(stretch.nodes, 1)
            if node.room_type == MapRoom.UNKNOWN and node.position in reached
        }
        if values:
            odds[stretch.id] = values
    all_odds = [value for values in odds.values() for value in values.values()]
    summary = (
        all_odds[0]
        if all_odds and all(value == all_odds[0] for value in all_odds)
        else odds or None
    )
    return MapView(
        boss=public_map.bosses,
        at=at,
        next=(
            tuple(_entry(graph, position, node) for node in public_map.legal_destinations)
            if public_map.legal_destinations is not None
            else None
        ),
        stretches=tuple(
            StretchView(
                id=stretch.id,
                floors=(
                    str(_coordinates(stretch.nodes[0].position)[0])
                    if len(stretch.nodes) == 1
                    else f"{_coordinates(stretch.nodes[0].position)[0]}-"
                    f"{_coordinates(stretch.nodes[-1].position)[0]}"
                ),
                rooms="".join(_ROOM_CODES[MapRoom(node.room_type)] for node in stretch.nodes),
                next=stretch.next,
            )
            for stretch in visible
        ),
        unknown_odds=summary,
        plan=planned_stretches(graph, position, plan),
    )
