# Run and Deck guide

Manage the Slay the Spire 2 run to victory across all non-combat rooms: routing, card rewards, shops, rest sites, events, relics, potions, and strategy notes.

## Tools

- `run_act`: Submit ordered non-combat `steps` plus an optional short `reason` (using names, not refs):
  - `interact` (`action`): execute a listed map travel, reward, shop, event, rest-site, or special-interface operation from `state.actions`.
  - `select` (`refs`): submit the complete final set of chosen candidate refs for an open `state.selection`.
  - `use_potion` (`potion`, optional `target`) and `discard_potion` (`potion`): omit `target` on targeted potions only when exactly one legal target exists; never pass `target` when untargeted.
  - Each step is checked when reached, so earlier steps can make later ones legal. Queue multiple steps when nothing unrevealed can change later choices; stop at revealing steps and decide again. Event/reward screens may close automatically after the last reward—do not queue an extra skip/close unless the screen stays open. Visible Run operations keep refs even when `enabled: false`; an earlier step may enable them.
- `run_inspect`: Read-only. `{"area":"state"}` refreshes current state; `{"area":"map"}` reads the act map.
- `run_knowledge`: Read-only base definitions (`mode="catalog"`, `"search"`, or `"lookup"` for up to 20 native IDs; card lookups include next-upgrade definitions). Live prices, costs, and instance values always come from `state`.
- `run_notes`: Read (`{"mode":"read"}`) or update (`{"mode":"write", ...}`) structured run notes. Writes require at least one of `core`, `plan`, or `other`, and succeed only while unpaused with an exposed `state.control == "ready"` observation:
  - `core`: array of at most 5 distinct native `definition_id`s of owned cards/relics the deck is built around.
  - `plan`: string (max 300 chars) stating how the deck beats the next Boss and its most urgent remaining gap.
  - `other`: string for potion pairings/reservations and durable one-time facts not visible in state.
  - Omitted fields stay unchanged. Do **not** pass `summary` in external mode (it is rejected with `wrong_role`).
- `run_skill`: No guidance documents are configured in external mode (`list` always returns empty); skip calling it.

## Execution loop

Use `scripts/sts2_cli.py --session <id> --auto ...` (see `SKILL.md`) in minimal-delta mode. If your context is compacted or you need full deck/relics/map, run `--full state run` (or `map run`) to refresh and reset your delta baseline. Set shell wait timeout >= 10s for `act` calls and batch deterministic non-revealing steps in one `act` call.

1. Start (and refresh after any pause, reconnect, or external transition) with `sts2_cli.py --session <id> --auto state run` (omit `--auto` when inspecting to recover from an `unknown` outcome).
   - If `phase == "terminal"`: stop immediately and report the final outcome (`state.result`, act/floor, HP, brief deck/relic summary) without calling `run_act` or writing `run_notes`.
   - If `phase == "run"` and `control == "ready"`: after a battle, assess HP, potions, and deck changes (distinguishing enemy damage, self-inflicted HP loss, and healing; tracking fought Elites), update `run_notes` when `core`/`plan`/`other` changes, and play non-combat rooms.
2. When an action enters battle (`phase == "combat"`):
   - **Direct single-agent mode (default):** switch directly to [Combat guide](combat.md) and play Turn 1 from the returned state (no extra `state combat` call needed when not using `--handoff`).
   - **Multi-agent worker mode (`--handoff`):** stop immediately without calling `combat_*`, and return a compact 3–5 line handoff (floor, room type, HP/potions, `core`/`plan`/`other`, upcoming route constraints, and whether `ROTATE_RECOMMENDED` or an Act change was reached).

## Run basics

- HP, max HP, gold, permanent deck, relics, and potions carry across rooms. Battle piles are temporary: discarding or exhausting in battle is not permanent removal.
- Rewards, shops, rest sites, and events offer exactly the operations shown; decline only when offered. Potion slots are limited—a full belt may require discarding a bottle before taking another.
- The run is won after every required Boss fight in the final act is won (`progress.acts`, `progress.bosses_left`). Nothing has value after the final fight. A deck change is worth the remaining fights in which it will be drawn.

## Strategy notes discipline

- **`core`**: Record the strongest linked group once two owned cards or relics genuinely feed each other; leave empty until then. Include only pieces the core's turn depends on (not a full deck inventory). Rewrite `core` freely if a stronger group forms.
- **`plan` (<= 300 chars)**: State which cards/relics deal Boss-winning damage, roughly which turn they come online, and which of the four deck demands is still most urgent. After a painful fight, keep its gap in `plan` until the deck answers it. If advising target priority for Combat, state the concrete condition (turns/actions required and attacks taken) so Combat knows when to pivot.
- **`other`**: Record pairings for potions that act on the next card (e.g., `POTION.DUPLICATOR`, `POTION.GIGANTIFICATION_POTION`)—intended card, target fight/turn, and fallback—plus revisable potion reservations (plans, never rigid prohibitions against saving a run).

## Deck construction

Judge every permanent card addition, upgrade, removal, or transform by how the whole deck plays across upcoming fights and Bosses.

### What a winning deck answers

1. **Early frontload:** Immediate damage to end early fights and Elites before hits pile up.
2. **Multi-enemy reach:** Area or multi-target output so groups do not deal combined damage while dying one by one.
3. **Peak-turn mitigation:** Block or damage reduction scaled to opening spikes, setup turns, and large Boss attacks.
4. **Rising growth:** Output per turn that compounds during a fight so high-HP Bosses die before outgrowing your defense. Flat printed numbers fall short in long fights.

Owned relics cover demands at zero draw and energy cost. Read Elite and Boss rules for fight constraints (turn clocks, play caps, per-card taxes, status floods, multi-enemy phases).

### Core, support, and transition

- **Core:** A compact linked group whose output rises turn after turn (sources producing repeatable/stacking fuel + payoffs cashing it in + multipliers scaling matching plays). A link exists when one owned piece's output—including costs like HP loss or exhaust—feeds what another counts. Broad generic bonuses (plain Strength/Vulnerable, minor flat triggers, generic exhaust fodder) are not links by themselves; cards leaning only on them still compete as standalone cards against your best owned card in that job.
- **Support:** Energy, net draw, targeted fetch/topdecking, and mitigation that bring the core online earlier and survive setup.
- **Transition:** Strong flat-number standalone cards that carry early fights over basic starters, then lose value once a core forms because flat output dilutes draws.

### Turn economy and pile quality

- Every circulating card lengthens reshuffles and delays key cards. Compare a standalone card against your **best owned card** in that job, not basic starters; upgrading the owned card often beats adding a second weaker copy.
- Compare typical hand energy cost against your turn's energy (base energy plus reliable card/relic energy). When average hands already exceed turn energy, another 2+ cost card without energy help leaves cards unplayed.
- Zero-cost cards save energy, not draw slots; low-impact 0-cost cards or self-copying discard-pile cards dilute reshuffles unless relics/powers scale per play.
- Favor energy when hands hold unplayed cards; favor net draw or fetch when energy goes unspent or key pieces arrive late.
- Count a required upgrade as part of a card's cost; several cards cannot all claim the same upcoming rest site.
- Removing or transforming basic starter attacks and blocks usually raises deck quality sharply by accelerating reshuffles (unless owned effects rely on them).

### Stage thresholds for card rewards

Default to **skipping** when no offered card clearly improves the deck over its draw/dilution cost.
- **Act 1:** Early on, strong frontload, multi-enemy reach, and efficient block that clearly beat basic starters justify taking so Act 1 Elites pay out relics safely. Also take rare early growth engines, reliable energy, and net draw. Once 2–3 transition cards plus early growth cover Act 1 threats, lean toward skipping extra flat-number cards. Avoid stacking 2+ cost cards without energy or narrow payoffs with zero triggers before an imminent Elite.
- **Middle acts:** Merely beating a basic starter is rarely enough. Lean toward skipping flat standalone cards weaker than your best owned option; prioritize completing links, multiplying an active core, adding net draw/energy, or covering multi-enemy waves and peak Boss hits.
- **Final act:** Few fights remain, and the end may require back-to-back Bosses with no inter-act rest or full heal between (normal combat victory effects still trigger). Strongly favor skipping; take only cards that directly change the final Boss fights (finishing growth multiplication, answering a Boss constraint/peak hit, or cheap draw/energy enabling setup + defense on the same turn).

## Route, rest sites, shops, and events

A run is a race between deck power (relics, cards, upgrades, removals, potions) and unavoidable escalating Bosses. HP and gold are currencies to convert into lasting power.

- **Route:** Compare paths to the act Boss by total value (Elites, treasure, shops, events, rest sites) and order, then verify survivability against current HP and potions before each certain recovery.
  - **Act 1** is the cheapest place to trade HP for power: Elites taken with HP for a bad opening and recovery/easy rooms afterward usually pay off across the entire run.
  - **Elite rounds:** In standard non-tutorial runs, an act's Elites appear in rounds—each round contains every act Elite once in random order, and a new round never starts with the Elite that closed the previous round. Check `map.elite_pool` (when present) for this act's candidate Elites and `mechanisms` for pre-combat Boss and Elite encounter rules; track fought Elites from earlier battles and price an upcoming Elite against the worst unfought candidate in the current round.
  - **Later/final acts:** Take Elites when normal fights end quickly with low HP loss and post-Elite HP reaches recovery; route around Elites your deck cannot answer, and in the final act preserve HP needed for the Bosses.
  - Before committing at a fork, count forced rooms through any non-branching stretch to the next choice or recovery, and check fallback exits if a fight goes badly. Do not follow fixed HP thresholds or always take the safest line.
- **Rest sites:** Upgrade when HP covers upcoming fights before the next recovery under plausible play (around half HP or higher is normally healthy; count actual deck block and potions rather than resting out of caution). Rest when HP does not cover the planned path or unlocks a substantially stronger route (e.g., an Elite). Prioritize upgrades that shift kill turns, block thresholds, energy/draw, or core scaling.
- **Shops:** Compare affordable combinations of cards, relics, removal, and potions against saving gold for a later reachable shop. A discount alone does not justify a weak card; removing a basic starter (or outdated transition card) is often the highest-value purchase. Remember potions that generate random cards in combat draw evenly from their pool without rarity weighting. Spending all gold is not a goal, and an empty slot is not a reason to buy a potion.
- **Relic choices and events:** Prefer effects that act *inside* fights (energy, draw, damage, block, prevention) over between-fight healing unless current HP cannot survive the next fight. For events, price curses, HP loss, or lost cards across every fight until a reachable remedy, and verify the next unavoidable fight stays survivable if a gamble fails.
