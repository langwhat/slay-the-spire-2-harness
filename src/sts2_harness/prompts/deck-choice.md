## Making the card choice

Compare all available reward groups together—the full feasible joint plan, its strongest subsets, and skipping all groups—and default toward skipping any addition that fails to beat keeping the current deck thinner.

Before calling `run_deck_choice`, check in order:

1. **Main gap** (from `deck`, `deck_stats`, `fights`, and upcoming rooms/Bosses before reading candidates): urgent early frontload or multi-target reach, missing Boss growth/multiplier, slow Cycle 1 or reshuffles (`first_pass_turns`, `cycle_turns`, `non_circulating`, `basic_cards`, `run_added`), cost-heavy hands (`high_cost_circulating`, cards sitting at `observed_plays: 0`), or short peak-turn mitigation?
2. **Marginal draw-pool test** (for each candidate): Does adding it clearly improve core arrival, Boss ceiling, or peak survival across remaining fights more than skipping—by forming/multiplying a real core with owned or co-taken pieces, adding true net draw/unconditional fetch/reliable energy, or directly fixing step 1's gap?
3. **Weak grounds for taking** — lean toward `skip` when the case rests mainly on:
   - Beating basic starters or duplicating a job already covered by stronger owned cards;
   - Generic attack buffs, minor relic counters/triggers, one secondary card's conditional bonus, or plain exhaust/discard fodder;
   - Answering deck bloat or unremoved starters by adding middling draw, paid zero-net-draw cantrips/filters, or redundant defense;
   - Starting an unrelated side-engine once a core exists, or stacking more 2+ cost or low-impact 0-cost cards without matching energy/draw support.
