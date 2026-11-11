# Reading state

State arrives as named sections. A section that is shown is complete and replaces what was shown before; a section that is not shown is unchanged. A section set to null does not apply now. `baseline=true` marks a full snapshot: every section is shown and everything earlier is superseded.

`defs` lists the definitions not yet shown in this conversation: id, name, type, cost, and rule text. Everywhere else an object appears by its native definition ID, with `+N` for upgrades, such as CARD.BASH+1. When an entry carries its own `text` or `cost`, that value is authoritative for that entry.

Objects you can act on carry a short `ref`, valid for the current node. Use refs in actions and definition IDs in knowledge queries. Piles and the deck are counts by definition ID; they show membership, never order. Presence is established by the current sections, not by a definition, a preview, or an older message.

A number is exact, "X" follows the card's rule, null is unknown, and an absent field does not apply. Unavailable options are listed with `enabled=false` and the game's reason when it gives one.

`choice` is the complete current decision: its prompt, min and max, candidates, and available operations. With a min of 0, selecting nothing declines.

A tool result gives `outcome` and the sections that changed. completed: done. rejected: nothing happened. stopped: the listed steps ran and the rest did not. unknown: it may have happened, so read the state before acting again. failed: a read failed. Continue from the returned state and never repeat completed steps.
