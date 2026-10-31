"""Partial route plans over nodes of the current act."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Edge:
    source: str
    destination: str


@dataclass(frozen=True, slots=True)
class RoutePlan:
    position: str
    reason: str
    edges: tuple[Edge, ...]

    @property
    def next_destination(self) -> str | None:
        destinations = tuple(
            edge.destination for edge in self.edges if edge.source == self.position
        )
        return destinations[0] if len(destinations) == 1 else None


class PlanRejection(StrEnum):
    UNKNOWN_POSITION = "unknown_position"
    EMPTY = "empty"
    UNKNOWN_NODE = "unknown_node"
    DUPLICATE_EDGE = "duplicate_edge"
    SELF_LOOP = "self_loop"
    AMBIGUOUS_NEXT = "ambiguous_next"
    DISCONNECTED = "disconnected"
    CYCLE = "cycle"


class PlanStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PlanUpdate:
    status: PlanStatus
    plan: RoutePlan | None
    rejection: PlanRejection | None = None


def replace_plan(
    previous: RoutePlan | None,
    position: str | None,
    nodes: frozenset[str],
    reason: str,
    edges: tuple[Edge, ...],
) -> PlanUpdate:
    rejection = _validate(position, nodes, edges)
    if rejection is not None:
        return PlanUpdate(PlanStatus.REJECTED, previous, rejection)
    assert position is not None
    return PlanUpdate(PlanStatus.ACCEPTED, RoutePlan(position, reason, edges))


def _validate(
    position: str | None, nodes: frozenset[str], edges: tuple[Edge, ...]
) -> PlanRejection | None:
    if position is None or position not in nodes:
        return PlanRejection.UNKNOWN_POSITION
    if not edges:
        return PlanRejection.EMPTY
    if any(edge.source not in nodes or edge.destination not in nodes for edge in edges):
        return PlanRejection.UNKNOWN_NODE
    if len(set(edges)) != len(edges):
        return PlanRejection.DUPLICATE_EDGE
    if any(edge.source == edge.destination for edge in edges):
        return PlanRejection.SELF_LOOP
    if sum(edge.source == position for edge in edges) != 1:
        return PlanRejection.AMBIGUOUS_NEXT

    outgoing: dict[str, list[str]] = {}
    indegree: dict[str, int] = {position: 0}
    for edge in edges:
        outgoing.setdefault(edge.source, []).append(edge.destination)
        indegree.setdefault(edge.source, 0)
        indegree[edge.destination] = indegree.get(edge.destination, 0) + 1

    reached = {position}
    pending = [position]
    while pending:
        for destination in outgoing.get(pending.pop(), []):
            if destination not in reached:
                reached.add(destination)
                pending.append(destination)
    if reached != indegree.keys():
        return PlanRejection.DISCONNECTED

    ready = [node for node, count in indegree.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for destination in outgoing.get(node, []):
            indegree[destination] -= 1
            if indegree[destination] == 0:
                ready.append(destination)
    if visited != len(indegree):
        return PlanRejection.CYCLE
    return None


class MovementStatus(StrEnum):
    NO_PLAN = "no_plan"
    UNOBSERVED = "unobserved"
    UNCHANGED = "unchanged"
    ADVANCED = "advanced"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class MovementUpdate:
    status: MovementStatus
    plan: RoutePlan | None


def reconcile_plan(plan: RoutePlan | None, position: str | None) -> MovementUpdate:
    if plan is None:
        return MovementUpdate(MovementStatus.NO_PLAN, None)
    if position is None:
        return MovementUpdate(MovementStatus.UNOBSERVED, plan)
    if position == plan.position:
        return MovementUpdate(MovementStatus.UNCHANGED, plan)
    if Edge(plan.position, position) not in plan.edges:
        return MovementUpdate(MovementStatus.INVALIDATED, None)

    reached = {position}
    pending = [position]
    outgoing: dict[str, list[str]] = {}
    for edge in plan.edges:
        outgoing.setdefault(edge.source, []).append(edge.destination)
    while pending:
        for destination in outgoing.get(pending.pop(), []):
            if destination not in reached:
                reached.add(destination)
                pending.append(destination)
    remaining = tuple(edge for edge in plan.edges if edge.source in reached)
    if not remaining:
        return MovementUpdate(MovementStatus.COMPLETED, None)
    return MovementUpdate(MovementStatus.ADVANCED, RoutePlan(position, plan.reason, remaining))
