# Combat guide

Play each Slay the Spire 2 battle so the run can be won: first guarantee victory in the battle, then minimize total HP and lasting resource loss. HP not needed to survive this fight or the fights before the next recovery is a spendable resource. When `state.progress.bosses_left > 1`, another Boss follows with no inter-act rest or full heal between (normal combat victory effects still trigger).

## Tools

- `combat_act`: Execute ordered battle `steps` (`play_card`, `use_potion`, `discard_potion`, `end_turn`, `interact`, `select`) plus an optional short `reason` (using names, not refs). Each step is validated when reached, so earlier steps can enable later ones (e.g., playing a card drawn or generated earlier if its ref is known). Queue the whole turn line—including `end_turn`—in one call whenever no later step depends on an unrevealed draw/random result; stop at revealing steps and re-evaluate. Forced single-candidate selections resolve automatically. Omit `target` on targeted actions only when exactly one legal target exists; never pass `target` on untargeted actions.
- `combat_inspect`: Read-only. `{"area":"state"}` refreshes battle state; `{"area":"map"}` reads the act map; `{"area":"combat_history"}` reads this battle's card history while the battle is active.
- `combat_knowledge`: Read-only base definitions (`mode="catalog"`, `"search"`, `"lookup"`). Live values always come from `state`.
- `combat_skill`: No guidance documents are configured in external mode (`list` always returns empty); skip calling it.

## Execution loop and recovery

Use `scripts/sts2_cli.py --session <id> --auto ...` in minimal-delta mode (omit `--auto` if delegating combat separately with `--handoff`). If your context is compacted or you need full piles/potions/relics/rules, run `--full state combat` to refresh and reset your delta baseline. Set shell wait timeout >= 10s for `act` calls.

1. **Start of battle:** In direct single-agent mode, act straight from the Turn 1 combat state returned on entry. In multi-agent worker mode (`--handoff`), start each assigned battle with `sts2_cli.py --session combat-<k> --handoff state combat`—never assume stale refs from any briefing or earlier battle.
2. **Play:** Use only `combat_*` tools while `state.phase == "combat"` and `state.control == "ready"`. Treat any Run `core`, `plan`, or potion reservation as advice, not an unbreakable rule—override a potion reservation whenever saving the run or preventing major route-breaking HP loss warrants it.
3. **Recovery and exit:**
   - If an action returns `unknown`, or any response returns `state: null`, call `sts2_cli.py --session <id> state combat` once to verify live state: continue if `state.phase == "combat"` and `state.control == "ready"`, transition/exit if `state.phase != "combat"`, or stop and report a blocker if `state` remains `null` or `control != "ready"`.
   - If blocked by pause (`control_unavailable`), stop and report the blocker.
   - **Direct single-agent mode (`--auto`):** When `state.phase == "run"`, `--auto` has already claimed deterministic rewards and opened any sole card reward—switch directly to [Run and Deck guide](run.md). When `state.phase == "terminal"`, stop and report the final run outcome.
   - **Multi-agent worker mode (`--handoff`):** As soon as `state.phase != "combat"`, **stop immediately without calling any `run_*` tool** and return a compact 3–5 line battle summary (localized names + native `definition_id`s only, never temporary `ref`s):
     - **Outcome:** `phase` (`run` or `terminal`, plus `state.result` if terminal), `room_type`, enemies faced (`name` + `definition_id`), turns taken, HP start -> end, potions used/gained, any permanent deck/relic changes, and `session_calls` / `ROTATE_RECOMMENDED` flag.
     - **Diagnosis (1–2 lines max):** Core arrival turn & main bottleneck (draw/energy/block/multi-target/status), dead cards drawn repeatedly, or reusable sequencing findings (facts vs hypotheses).

## Battle rules

Read every live number from the current `state` (see [Wire protocol](protocol.md) for field details); these native rules fill in mechanics not spelled out per turn:

- Energy, costs, block, and hand are whatever `state` shows; do not assume fixed starting resets. `"X"` follows card text; some resources (such as stars) persist across turns.
- Type-counting effects check native `card_type` only (an Attack that gains block is still an Attack). Play-triggered bonuses apply from when they resolve, not retroactively. An unplayable card can still be a legal selection candidate.
- Block stops damage, not direct HP loss. Target previews already include active modifiers and single-hit HP-loss caps against the target's current Block and remaining cap (each hit of a multi-hit card is previewed independently, without carrying cap depletion across hits). Enemy intents include active damage modifiers but not late HP-loss caps (such as `POWER.BUFFER_POWER` or post-Block HP-loss caps). Adjust for changes afterward, and count every hit of multi-hit intents.
- **Native enemy move rules:** An enemy's `next_move_id` identifies its public next move in `mechanisms` rules (always use the enemy's `ref` to target it). A move's effects happen in the listed order, so Strength gained after an attack does not add to that attack. An effect without an `if` happens even when the attack is fully blocked. Unpowered damage ignores attack modifiers such as `POWER.STRENGTH_POWER`, `POWER.WEAK_POWER`, and `POWER.VULNERABLE_POWER`, and is still blockable unless marked unblockable.
- An enemy at 0 HP may revive or shift phases. The battle has ended only when `state.phase != "combat"`.
- A power's `amount` may be intensity, duration, charges, or a counter; read its mechanism text before assuming decay or permanence.
- Drawing takes from the top of the draw pile within hand capacity and draw restrictions. If a draw is still needed after the draw pile empties, the entire discard pile shuffles into a new draw pile (a currently resolving card is not yet in discard). Emptying the draw pile without needing another card does not shuffle. Cards placed on top are drawn next, including during turn end. Non-hand pile arrays show membership, never order. Putting a card into hand via non-draw effects is not a draw. Adding or returning a card to a full hand (10 cards) sends the overflow to the discard pile.
- **Turn-end order:** Automatic end-of-turn plays and their triggers -> end-of-turn-in-hand effects -> discarding all unretained hand cards (including cards drawn during this sequence).
- **Retain & Ethereal:** Retain does **not** suppress end-of-turn-in-hand effects (e.g., `CARD.BURN`, `CARD.DECAY`, `CARD.DOUBT`, `CARD.SHAME`). An Ethereal card with such an effect resolves it and then exhausts; Ethereal exhausts at turn end even when retained. The Exhaust keyword applies only when played.
- **Orbs and Arrows:** A character whose base orb slot count is 0 and current orb capacity is 0 gains 1 orb slot when channeling an orb (characters with nonzero base orb slots whose capacity was reduced to 0 during combat do not); channeling into a full nonempty queue evokes the front orb first. For `arrow_slots`, `retrieved` arrows are ready and `away` arrows are out (check `impaled_target_ref`).
- Battle piles are temporary; creating, modifying, or exhausting a card in battle is not permanent unless native text states otherwise.

## Decision procedure

At every decision, work in this order:

1. **Threat & deadlines:** Sum HP lost to the next enemy action after current block, enemy by enemy and hit by hit. Note non-attack intents (scaling, debuffs, status pollution). Read every enemy power and mechanism for a hard deadline (countdown, phase shift, escape, or lethal scaling)—note which turn it hits and where your answers are.
2. **Kill / cancel check:** In exact play order—accounting for energy gains, buffs/debuffs, and consumed one-use bonuses as they resolve—check whether hand + potions can kill, stun, or phase-shift a dangerous enemy before it acts. Never assume two cards showing the same one-use bonus both receive it.
3. **Compare materially different lines:** Compare killing the biggest attacker, preventing damage, and setting up the core through the same future enemy action, including the next hand each leaves. Use known pile membership, draw counts, and shuffle timing to judge draw odds and verify an affordable fallback if key cards miss.
4. **Choose:** A line that meets every deadline beats one that does not, regardless of short-term HP saved; never discard/exhaust a deadline answer unless another answer is guaranteed in time. Among winning lines, minimize total battle HP and resource loss. Weigh any nonzero chance of death separately and ahead of average HP.

### Key tactical principles

- **Order for information and scaling:** Reveal draws and random outcomes before committing dependent choices (while energy remains to use them). Apply buffs/debuffs before the hits they scale. A draw that spends the energy needed to play what it finds is not real access.
- **Target by prevented actions, not raw HP:** Crossing a kill, shield, stun, or phase threshold matters; overkill or damage that changes no enemy action and no kill turn is wasted. In long fights against a target that must die, total damage determines the kill turn and removes future attack turns.
- **Setup vs. damage taken:** Play scaling/setup and clear bad cards on quiet or covered turns; under attack, weigh immediate HP lost against future attacks prevented. Once lethal is available, finish—unless a **safe delay** sets up a cross-battle relic counter (a relic counter whose text does not reset per turn/battle) just short of triggering for the next fight without risking extra HP, potions, or enemy scaling.
- **Phase transitions and revivals:** Read native enemy rules for death/phase timing. Trigger phase shifts that start a big attack or countdown on a turn whose hand (or next hand) can answer it.
- **Potions:** Evaluate a potion by the outcome it changes now (preventing death, killing to cancel a big attack, making a key setup turn safe, saving large HP) versus a concrete upcoming threat. Let draws/random reveals resolve first when the potion window stays open.
- **Recompute after every reveal:** After a partial batch or reveal, re-evaluate from actual hand, energy, and powers before continuing.
