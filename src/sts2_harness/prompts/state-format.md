# Reading state

State arrives as named sections. A section that is shown is complete and replaces what was shown before; a section that is not shown is unchanged. A section set to null does not apply now. `baseline=true` marks a full snapshot: every section is shown and earlier state sections are superseded.

`defs` lists the definitions not yet shown in this conversation: id, name, type, cost, and rule text. Everywhere else an object appears by a view key: its native definition ID with `+N` for upgrades, such as CARD.BASH+1. The native definition ID for that key is CARD.BASH. When an entry carries its own `text` or `cost`, that value is authoritative for that entry.

Objects you can act on carry a short `ref`, valid for the current node. Use refs in actions and native definition IDs without the upgrade suffix in knowledge queries. Piles and the deck are counts by definition ID; they show membership, never order. Presence is established by the current sections, not by a definition, a preview, or an older message.

A number is exact, "X" follows the card's rule, null is unknown, and an absent field does not apply. Unavailable options are listed with `enabled=false` and the game's reason when it gives one.

`choice` is the complete current decision: `selection` holds its prompt, min, max, and candidates; `actions` and `offers` list available operations and rewards. With a selection min of 0, selecting nothing declines. Costs retain their native resource fields, such as {"energy":1}; null is unknown. `generated` and `mechanisms` contain rule details referenced by the current objects.

A tool result gives `outcome` and the sections that changed. completed: done. rejected: nothing happened. stopped: the listed steps ran and the rest did not. unknown: it may have happened, so read the state before acting again. failed: a read failed. Continue from the returned state and never repeat completed steps.

Candidate `links` report potential event connections with owned cards or relics; their conditions and value still come from the supplied rules. `use.observed_plays` counts the base definition across all copies and upgrades, from sampled battle history, not complete fights. `deck_stats` is computed from the current deck and its draw and energy per turn, not from battle history. `notes` shows the strategy notes with `wants` numbered; a card offer's `usable_wants` lists the wants recorded before it appeared.
