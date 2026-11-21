# Role agent and context lifetimes

Each role agent owns its context outright; the context has no task or lifecycle of its own. The lifetimes are:

- **Run agent: one native act.** A confirmed act change replaces it with a new agent that starts from current public state, the strategy notes, and the act's guidance. Boss reward completion does not end the act.
- **Run context: one map node.** The Run agent rebuilds its context on the first decision after a confirmed node change, once the map is complete and action feedback has settled. The new context carries the current observation, map, route plan, guidance, latest battle evidence, and (in ordinary mode) the notes. Inside a node, every query and action stays in one conversation, even if the decision owner changes.
- **Deck agent: one card offer or card-adding selection.**
- **Combat agent and context: one battle**, through its review and handoff. A terminal battle ends the run with no review.

Run's input stays bounded by what matters now instead of growing across the act. Continuity between nodes comes from the notes, the route plan, and the battle evidence, not from old dialogue. The cost is that cached prefixes are not reused across nodes, and earlier-node dialogue is visible only in the archive. Archives record this policy as `run_context_policy=node_local_v1`.

Strategy notes belong to the run session and outlive every agent. Pending combat handoffs belong to orchestration.
