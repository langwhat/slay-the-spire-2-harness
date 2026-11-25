## Making the card choice

Compare all available reward groups as one decision. Cards across different groups can complete a link if they can be taken together. Compare the full feasible joint plan, its strongest useful subsets, and skipping all groups; evaluate links across every group rather than pairwise. Default to skipping additions that fail this comparison. Each added circulating card expands the draw pool and can delay key pieces in fights that draw far enough into it; judge the actual cost by what it replaces in ordinary hands and whether it exhausts or leaves the pile before later cycles.

Before calling `run_deck_choice`, check in order:

1. **Main gap** (from `deck`, `deck_stats`, `fights`, `progress.route`, and Boss/Elite rules, before reading candidates): urgent frontload/multi-target for rooms ahead, missing Boss growth or multiplier, slow or over-costed hands (high `circulating`, stacked 2+ cost cards without energy help, or chosen cards going unplayed), or short opening/peak-turn mitigation?
2. **Link or clear upgrade** (for each candidate): Does its text form or multiply a real link with pieces you own or can take in the same plan, add genuine battle-long scaling/draw/energy, or directly fix step 1's gap? If standalone flat numbers, does it clearly beat your **best owned card** in that job?
3. **Weak grounds for taking** — lean toward `skip` when the case for a candidate rests mainly on:
   - Beating basic starters while duplicating a job already covered by stronger owned cards;
   - A flat card leaning on generic attack-wide buffs or enemy debuffs, minor play-count relics, or serving as plain exhaust or discard fodder;
   - Stacking another 2+ cost card into hands already crowded with expensive cards without energy help, or adding low-impact `0`-cost / self-copying filler without draw or per-play scaling;
   - Relying on triggers you barely produce yet, adding redundant triggers without payoffs, or duplicating low-impact flat utility/defense.
