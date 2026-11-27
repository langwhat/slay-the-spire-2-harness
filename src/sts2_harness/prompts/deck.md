# Deck

Judge a permanent deck change by how the whole deck plays across upcoming fights and Bosses, read from card and relic text and current numbers rather than printed values alone. Supplied knowledge, when present, gives exact object rules, Elite and Boss mechanics, and candidate cores; treat documented cores as conditional options rather than orders, and read unlisted links the same way from text.

## What a winning deck answers

Later Bosses bring high HP and relentless damage, and the final act can require two Bosses back to back without healing between them. A winning deck covers four demands:

- Early frontload: immediate damage to end early fights and Elites before hits pile up.
- Multi-enemy reach: area or multi-target output so groups do not deal combined damage while dying one by one.
- Peak-turn mitigation: block or damage reduction (reducing enemy attack damage, preventing hits) scaled to opening spikes, setup turns, and large Boss attacks.
- Rising growth and endgame ceiling: output per turn that compounds during a fight so high-HP Bosses die before outgrowing your defense. Fixed printed values deal flat damage per turn and fall short in long fights. Closing final Bosses requires either a **fast-kill ceiling** (compounding or multiplied burst that ends the Boss while finite spike mitigation holds) or a **sustained-endurance ceiling** (scaling or repeatable mitigation that holds through long fights and outpaces any self-inflicted costs).

Owned relics cover these demands at zero draw and energy cost. Read supplied Elite and Boss rules for fight constraints—turn limits, per-card or per-type taxes, caps on plays per turn, status floods, or multi-enemy phases: tight turn clocks favor fast compounding damage over slow defense, while per-card taxes or play caps favor high value per card over low-impact chains.

## Core, support, and transition

- Core: a compact linked group or self-scaling engine whose output rises turn after turn.
- Support: energy, net draw, targeted fetch or putting cards on top of the draw pile, and mitigation—bringing the core online earlier and surviving setup.
- Transition: a standalone card with strong flat numbers. It carries early fights among basic starters, then loses value once a core exists because flat output delays the core on every draw.

### Links from card and relic text

- Sources produce repeatable or stacking fuel: a stacking stat, a card that grows per play, a compounding per-turn power, or recurring events (HP lost, cards exhausted, drawn, discarded, or created, attacks played, block gained, debuffs applied).
- Payoffs cash in a source ("whenever", "for each", pile or stat counts, turn-start triggers). Price a payoff by triggers your current deck and relics already produce in an ordinary fight; if its own text supplies most of them, judge that standalone effect.
- Multipliers scale matching plays: per-hit bonuses favor multi-hit attacks, percentage bonuses favor big hits, and replay, copy, or cost reduction scales with matching density.

A link forms when an owned piece's output—including costs like losing HP or exhausting cards—feeds what another piece counts. A self-growing source can carry fights alone; a power repeating a fixed number each turn does not compound and is worth that flat effect over remaining turns minus setup cost. Once triggers are plentiful, another pure trigger source adds less than a payoff or draw; a lone half needing rare or multiple missing pieces is a wish, not current strength. Pivot cores freely if a stronger group forms, but do not bolt a second unrelated engine onto a working core—split piles delay both.

Broad bonuses or minor side-triggers do not make every matching flat card a linked payoff: an owned stat or enemy debuff that boosts every attack, a relic giving a small flat bonus every few plays, triggering one secondary card's minor conditional bonus when drawn together, or serving merely as fodder to be exhausted or discarded applies to almost any card of that kind. A battle-long power that permanently scales combat output, defense, or debuff/resource multipliers is genuine Boss growth on its own, whereas another flat card merely leaning on an owned bonus still competes as a standalone card against your best owned card in that job—unless its own multi-hit count, play rate, or when-exhausted effect converts that bonus far better than owned options.

## Turn economy and pile changes

Each turn draws `base_draw` cards and spends `base_energy` plus reliable combat energy (or other card resources). Every added card—including Powers and Exhaust cards (`deck_stats.non_circulating`)—stretches `deck_stats.first_pass_turns` and delays core arrival and opening defense on Cycle 1; every circulating card also stretches `deck_stats.cycle_turns` on later cycles. Adding mediocre cards lowers `basic_and_curse_pct` on paper while making the deck slower; fix basic-card and curse dilution by skipping and removing, not by adding filler.

- Weigh a standalone attack or block card against the best owned card already doing that job, not basic starters; upgrading the owned card usually beats adding a weaker second option.
- Watch `deck_stats.high_cost_circulating` and `deck_stats.circulating_avg_energy` against `base_energy` plus reliable energy or cost reduction: stacking multiple 2+ cost cards without energy help leaves cards unplayed and crowds out defense.
- Zero-cost cards save energy, not draw: without extra draw, a low-impact 0-cost card still spends one of `base_draw` slots, and self-copying cards multiply reshuffle dilution unless owned relics or powers scale per play. A pre-upgraded (`+1`) card still occupies a permanent draw slot; judge its upgraded numbers against your best owned cards and Boss ceiling, not against its unupgraded version.
- Same-turn combo cards without retain, fetch, or heavy draw become unreliable as `deck_stats.cards` grows.
- Distinguish true net draw and unconditional fetch from energy-costing zero-net-draw cantrips or conditional draw/filter cards: paying energy to draw 1 or filter does not shorten core arrival, and a conditional utility card bricks when its condition is absent. Favor true net draw or unconditional fetch when energy goes unspent or key pieces arrive late, and favor energy when hands hold unplayed cards.
- Price cards that add unplayable, retained cards into hand severely unless owned effects reliably convert, discard, or exhaust them: each retained unplayable copy shrinks usable hand size on later turns.
- Reach opening and peak-turn mitigation through high-impact defense plus true net draw, fetch, and thinning—not by piling extra mediocre block, redundant attack debuffs, or single-fight insurance cards into a diluted deck.
- Price conditional cards by how often owned `deck` and `relics` satisfy the condition in ordinary fights.
- With `base_orb_slots` 0, channeling while orb capacity is 0 grants one orb slot; channeling into a full nonempty queue evokes its front orb first.
- Judge card generation, exhaustion, removal, or transformation by net pile quality on critical turns. Exhausting or removing basic starters thins reshuffles unless an owned effect still counts them; broad replacement risks breaking your sole block, damage, or fuel.

## Evidence from state

- The planned route, when shown, lists rooms ahead (`E` Elites, `R` rest sites, `$` shops, `B` Boss): imminent Elites reward immediate impact; nearby rest sites or shops shape upgrade and removal room.
- When Elite or Boss rules are known, test a candidate against the full remaining run—not just one encounter: a narrow tech card justified for one upcoming Elite or Boss should still beat keeping the draw pool thinner across all remaining fights (including back-to-back final Bosses).
- In `fights`, read the deck's own output from `damage_one` on turns without potions, and its defense from `incoming` against `hp_lost`; `deck_added` and `deck_removed` show which deck changes those numbers reflect. Compare them with the next Boss: its HP over the turns its deadline or growing attacks allow, and its peak hits against what the deck has stopped. The larger shortfall is the main gap. Long battles point to slow core arrival or weak growth; heavy HP loss in short kills points to short peak-turn mitigation. Treat a battle review, when present, as one fight's sample rather than grounds for a narrow one-enemy counter.
- Owned playable cards showing `use.observed_plays` of 0 (or very low plays across `deck_stats.battles_observed` fights) while basic starters still see play signal dead weight, cost-heavy hands, or deck bloat—tighten additions further. Do not invent unseen enemy numbers.
