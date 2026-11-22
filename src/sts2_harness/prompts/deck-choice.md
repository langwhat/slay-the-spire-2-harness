## Making the card choice

Compare the available reward groups as one decision. A card in another group can complete a link if both can be taken. Compare the resulting deck with taking either card alone and with skipping both. Default to skipping additions that fail this comparison. Each added card costs a draw in every remaining fight.

Before calling `run_deck_choice`, check in order:

1. **Main gap** (from `deck`, `deck_stats`, `fights`, `progress.route`, and Boss rules, before reading candidates): urgent frontload/multi-target for rooms ahead, missing Boss growth, slow or over-costed hands (`circulating` size or `circulating_avg_energy * base_draw` above turn energy), or short peak-turn mitigation?
2. **Link or clear upgrade** (for each candidate): Does its text form or multiply a real link with pieces you own or can take in the same plan, or directly fix step 1's gap? If standalone flat numbers, does it clearly beat your **best owned card** in that job?
3. **Weak grounds for taking** — lean toward `skip` when the case for a candidate rests mainly on:
   - Beating basic starters while duplicating a job already covered by stronger owned cards;
   - Generic attack-wide buffs or enemy debuffs, minor play-count relics, or serving as plain exhaust or discard fodder;
   - Adding `2+` cost into already energy-tight hands without energy help, or adding low-impact `0`-cost / self-copying filler without draw or per-play scaling;
   - Relying on triggers you barely produce yet, adding redundant triggers without payoffs, or duplicating a flat-value or multi-energy defensive/setup card.
