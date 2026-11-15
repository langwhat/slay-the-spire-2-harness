from sts2_harness.route_plan import Edge, RoutePlan


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
    plan = RoutePlan("0", "Keep options", edges)

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

    plan = RoutePlan("0", "Go", (Edge("0", "1"),))
    assert reconcile_plan(plan, None).status is MovementStatus.UNOBSERVED
    assert reconcile_plan(plan, None).plan == plan
    assert reconcile_plan(plan, "0").status is MovementStatus.UNCHANGED
    assert reconcile_plan(plan, "0").plan == plan
    off_plan = reconcile_plan(plan, "2")
    assert off_plan.status is MovementStatus.INVALIDATED
    assert off_plan.plan is None
    assert reconcile_plan(None, "2").status is MovementStatus.NO_PLAN
