## Making the card choice

Compare all available reward groups as one decision. Cards across different groups can complete a link if they can be taken together. Compare the resulting deck against taking a smaller subset of those additions and against skipping all of them, and default to skipping additions that fail this comparison. Every added card spends a draw slot in the critical first deck cycle: a card that draws one replacement still spends turn energy and per-play limits, and an exhausting card or power still delays key pieces before the first reshuffle. Skipping preserves both draw density and `circulating_avg_energy`.

Before calling `run_deck_choice`, check in order:

1. **Single sharpest bottleneck** (from `deck`, `deck_stats`, `fights`, `progress.route`, and Boss/Elite rules, before reading candidates): identify the one shortfall that most threatens upcoming fights—urgent frontload/multi-target for rooms ahead, missing Boss growth, slow or over-costed hands (`deck_stats.circulating` size or `circulating_avg_energy * base_draw` above turn energy), or short peak-turn mitigation—and count how many non-starter cards already cover that job. Do not treat secondary wish-list items as grounds to add cards.
2. **Link or clear upgrade** (for each candidate): Does its text form or multiply a real link with pieces you own or can take in the same plan, or directly close step 1's bottleneck? If it is a standalone card or duplicates a covered job, does it clearly beat your **best owned card** in that job and get played ahead of existing chosen cards in an ordinary hand? If a card needs to share a turn with a specific owned card without retain or fetch, separate **first-cycle reliability** (full `deck_stats.cards`, which governs hallway fights and opening turns) from **post-setup ceiling** (`deck_stats.circulating` plus active draw in long Boss and Elite fights): do not rely on it for short-fight survival when `cards` is thick, and only price it as Boss ceiling when a thin `circulating` pile or strong draw lets the pair recur once powers and exhaust cards are out.
3. **Weak grounds for taking** — lean strongly toward `skip` when the case for a candidate rests mainly on:
   - Beating basic starters while duplicating a job already covered by stronger or multiple owned cards;
   - Generic attack-wide buffs or enemy debuffs, minor play-count relics, class-wide tags or ordinary attack counts already supplied by common cards, or serving as plain exhaust or discard fodder;
   - Pre-upgraded flat numbers or modest extra utility on a card that still trails your core or duplicates a covered job;
   - Adding another copy of a non-core attack, block, setup, or utility card to compensate for a bloated deck—more circulating cards worsen draw consistency rather than solving it;
   - Adding `2+` cost into already energy-tight hands without energy help, or adding low-impact `0`-cost / self-copying filler without net draw or per-play scaling;
   - Relying on triggers you barely produce yet or adding redundant triggers without payoffs.

When taking a card after a core exists or once `deck_stats.circulating` is already high, briefly state in `reason` why its impact outweighs adding a draw slot and which owned card or specific bottleneck it surpasses.
