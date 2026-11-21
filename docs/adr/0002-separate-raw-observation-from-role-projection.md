# Separate Raw Observation from Role Projection

The Mod publishes self-contained public facts as semantic plain text. The core preserves that raw observation for callers that do not share conversation history, while each Role Context owns a role-specific projection and its delivery ledger.

Run projections exclude combat-internal state and retain only Run-owned facts plus encounter and transition outcomes. Stable content may be retained separately from node-scoped instance state so a role projection can append only meaningful changes. The Journal records evidence but is not a second context.

This keeps the public observation contract self-contained and concentrates cache-aware reuse behind the Role Context seam. Projection changes require fresh replay and evaluation evidence; they do not require a compatibility layer for superseded internal formats.
