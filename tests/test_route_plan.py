import pytest

from sts2_harness.route_plan import Edge, PlanStatus, replace_plan


def test_accepts_rooted_plan_with_later_branches_and_merge() -> None:
    edges = (
        Edge("0", "1"),
        Edge("1", "2"),
        Edge("1", "3"),
        Edge("2", "4"),
        Edge("3", "4"),
    )
    result = replace_plan(None, "0", frozenset({"0", "1", "2", "3", "4"}), "Compare", edges)

    assert result.status is PlanStatus.ACCEPTED
    assert result.plan is not None
    assert result.plan.edges == edges
    assert result.plan.next_destination == "1"


@pytest.mark.parametrize(
    ("position", "edges"),
    [
        (None, (Edge("0", "1"),)),
        ("unknown", (Edge("0", "1"),)),
        ("0", ()),
        ("0", (Edge("0", "missing"),)),
        ("0", (Edge("0", "1"), Edge("0", "1"))),
        ("0", (Edge("0", "0"),)),
        ("0", (Edge("0", "1"), Edge("0", "2"))),
        ("0", (Edge("0", "1"), Edge("2", "3"))),
        ("0", (Edge("0", "1"), Edge("1", "2"), Edge("2", "1"))),
    ],
)
def test_invalid_replacement_keeps_previous_plan(
    position: str | None, edges: tuple[Edge, ...]
) -> None:
    previous = replace_plan(
        None, "0", frozenset({"0", "1", "2", "3"}), "Old", (Edge("0", "1"),)
    ).plan
    assert previous is not None

    result = replace_plan(previous, position, frozenset({"0", "1", "2", "3"}), "New", edges)

    assert result.status is PlanStatus.REJECTED
    assert result.plan == previous


def test_observed_movement_prunes_only_unreachable_edges_then_completion() -> None:
    from sts2_harness.route_plan import MovementStatus, reconcile_plan

    edges = (
        Edge("0", "1"),
        Edge("1", "2"),
        Edge("1", "3"),
        Edge("2", "4"),
        Edge("3", "4"),
        Edge("4", "5"),
    )
    plan = replace_plan(
        None, "0", frozenset({"0", "1", "2", "3", "4", "5"}), "Keep options", edges
    ).plan
    assert plan is not None

    first = reconcile_plan(plan, "1")
    assert first.status is MovementStatus.ADVANCED
    assert first.plan is not None
    assert first.plan.next_destination is None
    assert first.plan.edges == edges[1:]

    branch = reconcile_plan(first.plan, "2")
    assert branch.status is MovementStatus.ADVANCED
    assert branch.plan is not None
    assert branch.plan.reason == "Keep options"
    assert branch.plan.edges == (Edge("2", "4"), Edge("4", "5"))

    merged = reconcile_plan(branch.plan, "4")
    assert merged.plan is not None
    final = reconcile_plan(merged.plan, "5")
    assert final.status is MovementStatus.COMPLETED
    assert final.plan is None


def test_unseen_or_unchanged_position_preserves_plan_but_off_plan_invalidates() -> None:
    from sts2_harness.route_plan import MovementStatus, reconcile_plan

    plan = replace_plan(None, "0", frozenset({"0", "1", "2"}), "Go", (Edge("0", "1"),)).plan
    assert plan is not None
    assert reconcile_plan(plan, None).status is MovementStatus.UNOBSERVED
    assert reconcile_plan(plan, None).plan == plan
    assert reconcile_plan(plan, "0").status is MovementStatus.UNCHANGED
    assert reconcile_plan(plan, "0").plan == plan
    off_plan = reconcile_plan(plan, "2")
    assert off_plan.status is MovementStatus.INVALIDATED
    assert off_plan.plan is None
    assert reconcile_plan(None, "2").status is MovementStatus.NO_PLAN


def test_accepts_speculative_cross_branch_and_non_monotonic_edges() -> None:
    # The graph has no complete knowledge of special movement, so it cannot certify legality.
    result = replace_plan(
        None,
        "map:2:3",
        frozenset({"map:2:3", "map:3:8", "map:1:1"}),
        "Consider a special move",
        (Edge("map:2:3", "map:3:8"), Edge("map:3:8", "map:1:1")),
    )

    assert result.status is PlanStatus.ACCEPTED
    assert result.plan is not None
    assert result.plan.next_destination == "map:3:8"


def test_accepted_replacement_replaces_all_of_the_prior_plan() -> None:
    nodes = frozenset({"0", "1", "2", "3"})
    old = replace_plan(None, "0", nodes, "Old", (Edge("0", "1"),)).plan
    assert old is not None

    replacement = replace_plan(old, "0", nodes, "New", (Edge("0", "2"), Edge("2", "3")))

    assert replacement.status is PlanStatus.ACCEPTED
    assert replacement.plan is not None
    assert replacement.plan.reason == "New"
    assert replacement.plan.edges == (Edge("0", "2"), Edge("2", "3"))
