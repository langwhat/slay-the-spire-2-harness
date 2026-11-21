# Separate raw observation from role projection

The Mod publishes complete public facts as self-contained semantic text. The core keeps that raw observation for callers that do not share a conversation, such as external agents. Each role context builds its own projection from it and keeps its own record of what it has delivered.

Run's projection leaves out combat-internal state and keeps Run-owned facts plus battle and transition outcomes. Stable content can be kept apart from node-scoped instance state, so a projection only appends what actually changed. The Journal records evidence; it is not a second context.

This keeps the public observation contract simple and puts all cache-aware reuse behind the context. Projection changes are checked with fresh replay and evaluation. Old internal formats get no compatibility layer.
