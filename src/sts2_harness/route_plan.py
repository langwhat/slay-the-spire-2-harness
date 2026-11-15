"""Partial route plans over nodes of the current act."""

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Edge:
    source: str | None
    destination: str


@dataclass(frozen=True, slots=True)
class RoutePlan:
    position: str | None
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
    AMBIGUOUS_NEXT = "ambiguous_next"
    DISCONNECTED = "disconnected"
    DESTINATIONS_UNKNOWN = "destinations_unknown"
    TRAVEL_NOT_AVAILABLE = "travel_not_available"


class PlanStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PlanUpdate:
    status: PlanStatus
    plan: RoutePlan | None
    rejection: PlanRejection | None = None


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
    outgoing: dict[str | None, list[str]] = {}
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
