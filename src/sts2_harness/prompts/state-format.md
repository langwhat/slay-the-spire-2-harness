# Reading state

State arrives as named sections. Each ordinary section shown is complete and replaces its previous value; a section not shown is unchanged. A section set to null does not apply now. `baseline=true` marks a full current snapshot and supersedes earlier state sections. `defs` and `mechanisms` are incremental rule catalogs: new entries append rather than replace them. A baseline includes the rules referenced by its current state.

`defs` lists definitions not yet shown in this conversation: id, name, type, cost, and rule text. Objects use view keys made from their native definition ID and `+N` for upgrades, such as CARD.BASH+1; the native ID is CARD.BASH. Run deck and candidate rows carry their own card rules instead of repeating them in `defs`. An entry's own `text` or `cost` overrides its definition, including an explicit null when the current value is unknown.

Objects you can act on carry a short `ref`, valid for the current node. Use refs in actions and native IDs without upgrade suffixes in knowledge queries. Piles and the deck show counts and membership, never order. Combat shows battle piles rather than the permanent deck. Presence comes from current sections, not from a definition, preview, or older message.

A number is exact and "X" follows the card's rule. Optional null or empty fields are normally omitted; explicit null or empty overrides clear an earlier value. Omitted defaults are `enabled=true`, `status=normal`, `stack_count=1`, `is_used_up=false`, `is_melted=false`, and `should_retain_this_turn=false`, `exhaust_on_next_play=false`, `has_turn_end_in_hand_effect=false`. Omitted `local_keywords` and `cost_without_global_effects` equal the card's `keywords` and `cost`; omitted `display_amount` equals `amount`; omitted `previews` mean the card's text is the same for every target. Other absent facts are unavailable or do not apply; do not infer their values.

In Combat, `arrow_slots` lists the player's arrow slots in slot order. A `retrieved` arrow is ready to shoot, an `away` arrow is out, and a `destroyed` arrow cannot be retrieved this fight. An away slot can include the `impaled_target_ref` of its current target.

`choice` is the complete current decision: `selection` holds its prompt, min, max, and candidates; `actions` and `offers` list operations and rewards. A selection candidate already in the current hand may carry only its identity; read its details from the hand. With selection min 0, selecting nothing declines. Unavailable options carry `enabled=false` and the game's reason when supplied. A deck row's or candidate's `upgrade` carries its native status and only the fields that differ from the current card; overlay those fields on that card to read the upgraded version.

`generated` and `mechanisms` supply rule details referenced by current objects. A mechanism is delivered once per distinct kind, name, and text in this conversation; a changed text is a new rule. A mechanism being retained in the catalog does not establish that its source is currently present.

An enemy's `next_move_id` identifies its public next move in mechanism rules. Use the enemy's `ref` to target it.

A tool result gives `outcome` and changed sections. completed: done. rejected: nothing happened. stopped: the listed steps ran and the rest did not. unknown: it may have happened, so read the state before acting again. failed: a read failed. Continue from the returned state and never repeat completed steps.

`use.observed_plays` counts the base definition across all copies and upgrades, from sampled battle history, not complete fights. `deck_stats` counts the current deck and gives the native `base_draw` and `base_energy`. `notes` shows the strategy notes.

`progress.acts` is the number of acts in the run. `progress.bosses_left` counts the Boss fights this act still requires, including the current one. In battle, `progress.route` lists the remaining planned rooms after this room as strings grouped by stretch, when one remaining route is known. A plan may end before the Boss. The room codes are M (Monster), E (Elite), R (rest site), $ (shop), T (treasure), ? (unknown), B (Boss), and A (Ancient). `card_rarity_offset` is the current native card rarity adjustment; supplied knowledge describes the reward calculation. `run_rules` supplies the current ascension level and the localized rules in force.

## Deck reward fields

For Deck, `progress.route` uses the same room codes and is omitted when the remaining plan is unavailable. `deck_stats.circulating_avg_energy` is the mean energy cost per copy of a playable circulating card with a fixed integer energy cost, rounded to two decimal places. It is omitted when no cards qualify.

`fights` lists the most recent completed battles, oldest first. `deck_added` and `deck_removed` show how the deck changed since the battle before it; an upgrade or transform shows as one card removed and another added. Each per-turn array has one entry per player turn, counted together with the enemy turn after it: `damage` is the HP all enemies lost, `damage_one` the most HP any one enemy lost, `incoming` the attack damage shown at the start of the turn by enemies that survived to act (null when unknown), `hp_lost` the HP you lost, and `potions` the potions used. `incoming` minus `hp_lost` approximates what block and prevention stopped. The last turn's damage stops at the HP the enemies had left. Turns with potions, and battles where `damage` exceeds `damage_one`, overstate what the deck alone does to one enemy.
