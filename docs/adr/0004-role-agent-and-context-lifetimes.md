# Role agent and context lifetimes

Each role agent owns its context outright; the context has no task or lifecycle of its own. The lifetimes are:

- **Run agent and context: one native act.** A confirmed act change replaces the Run agent and its context with a fresh one starting from current public state, the strategy notes, and the act's guidance. Boss reward completion does not end the act. Within an act, Run keeps one append-only conversation across map nodes so stable definitions, mechanisms, and prefix caches are reused, while changed sections, notes, guidance, and battle evidence append incrementally. Capacity compaction handles long acts.
- **Deck agent: one node's visible card reward groups or card-adding selection.** Deck plans all visible card reward groups in the node jointly; execution resolves them sequentially with per-group re-validation and keeps the same Deck context if partial completion or state changes require re-decision.
- **Combat agent and context: one battle**, through its review and handoff. A terminal battle ends the run with no review.

Keeping Run's context across nodes in an act preserves prefix cache reuse and avoids resending full act baselines at every room, while capping growth at act boundaries and via capacity compaction. Archives record this policy as `run_context_policy=act_local_v1` (older archives retain `node_local_v1`).

Strategy notes belong to the run session and outlive every agent. Pending combat handoffs and node reward progress belong to orchestration.
