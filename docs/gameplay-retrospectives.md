# Gameplay retrospectives

A practical workflow for auditing completed runs, separating Mod and execution defects from prompt or strategy issues, making targeted fixes that generalize, and validating them without over-fitting or bloating prompts.

Do not judge a run from its outcome alone or rewrite prompts on intuition. Aggregate batch metrics first, inspect exact delivered state side by side with the agent's recorded `reason`, fix the lowest owning layer, and verify cheaply before launching full runs.

## 1. Aggregate batch metrics first

Before reading individual fights, run a read-only script across the batch's `archive.sqlite` files and native `saves/history/*.run` files to establish a clean baseline.

### Verify recording alignment and infrastructure health

- **Check run settings first.** Read each archive's `manifest` (`minimal`, `seed`, `materials`) and per-role `model_attempts` usage (`reasoning_tokens`, `input_tokens`, `cache_read_tokens`). Never pool minimal and full-mode runs, or runs whose actual per-role reasoning effort differed, into the same comparison.
- **Clear engineering noise first.** Count `model_fail`, `model_recoveries`, `gaps`, `settle` rejections (`selection_required`, `action_unavailable`, `invalid_refs`), and Mod `Unresolved` or warning diagnostics.
- **Audit winning runs too.** Settlement races, truncated batches, missing role fields, and redundant payload bytes often hide inside victories and quietly cost losses later.
- **Fallback for older schemas.** When comparing across older archive revisions where `Journal.open` rejects evolved snapshots, open `archive.sqlite` read-only via SQLite (`manifest`, `records`, `model_requests`, `model_attempts`, `card_choices`, `blobs` with `zlib` decompression) and cross-check outcomes, removals, upgrades, and floor counts against the native `.run` file.

### Track high-signal gameplay and context metrics

Compare wins against losses **stratified by act reached**—pooling Act 1 deaths with Act 3 clears creates false survivorship signals (for example, making losing decks look thinner). Also check per-seed win clustering so a single high-roll seed or event does not mask systematic weaknesses.

High-signal metrics:

- **Deck cycling and dilution:** `circulating` card count, `cycle_turns` (`circulating / base_draw`), remaining basic attacks/blocks plus curses (count and share of circulating cards), total upgrades, per-act take/skip rate (excluding mandatory selections and stolen-card returns), and shop removals vs. purchases.
- **Snowballing and Boss readiness:** elites fought per act, final relic count, HP and potions entering Boss fights, and Boss HP remaining on Act 3 defeats.
- **Opening defense vs. late-game ceiling:** from battle histories, compare Act 2–3 Turn 1–2 block mitigation and HP lost against Turn 3+ damage per turn and sustained block on attack turns. Both low-cycle thin decks and bloated decks lose for distinct structural reasons; quality density paired with draw/energy engines separates winners from losers far better than raw deck size.
- **Common and Uncommon card funnels:** rare cards are too sparse to plan around. Track offer count, pick rate, and win rate for Common and Uncommon cards (plus which cards displaced skipped ones) to spot systematic under-drafting or over-drafting.
- **True card play rate and dead-weight tracking:** combine `settle` `play_card` records with `inspect` `combat_history` using `max(settle_plays, inspect_plays)` per battle so killing-turn plays and auto-played cards are both counted. Track plays per copy per battle, zero-play battle rate, and usage over the first 5 fights after a card is drafted to expose dead bricks and low-impact bloat.
- **Context size and cache ledger:** compute weighted token cost (`uncached_input * 1 + cache_read * 0.05 + output * 5`) and rank `observation` / `mechanisms` fields by total bytes sent across a run. Large repeated fields (such as duplicated descriptions or regenerated previews) dilute attention and inflate cost.

## 2. Inspect exact frames: pair delivered JSON with `reason`

Never guess why a choice happened from a high-level summary. Reconstruct the exact payload delivered to the role on that request (`observations`, `mechanisms`, `previews`, `descriptions`, `deck_stats`, `route`) and read it side by side with the verbatim `reason` and tool call.

- **Same-seed longitudinal diff.** When a change regresses on a shared seed, diff floor by floor (picks, shops, upgrades, routes) to locate the **first divergence point** before attributing blame.
- **Catch literal misunderstandings and misleading previews.** Comparing `reason` against the exact delivered text immediately reveals when the agent followed a literal—but misleading—extracted phrase, acted on a native preview that still showed damage despite a missing prerequisite resource, or missed a state buried in a long list (such as a pulsing active relic that the game's UI highlights visually).
- **Turn hindsight lessons into upfront facts.** Whenever a post-battle review or strategy note records a fixed game mechanic learned the hard way, ask: *why could the agent not deduce this on Turn 1?* Move deterministic engine rules and missing UI-equivalent indicators into the Mod or observation projection instead of spending extra review turns rediscovering them.
- **Verify synergy claims against combat reality.** Agents rarely invent nonexistent card text, but often justify picks with weak or negative-EV synergies (universal damage buffs, low-probability triggers, or using a mediocre card as exhaust/discard fodder). Check whether claimed synergies actually delivered value in subsequent fights.
- **Confirm legitimate tradeoffs before patching.** If the exact delivered state and mechanics were accurate and the choice reflects a defensible conservative tradeoff (for example, skipping a power in a bloated deck facing status clutter, or saving a potion slot for an upcoming shop), do not turn it into a code or prompt patch.

## 3. Classify root causes into five buckets

Assign every supported finding to the lowest owning layer before writing any fix:

| Bucket | Typical examples | Action |
| --- | --- | --- |
| **1. Mod extraction gap or native blind spot** | Misleading extracted rule wording; previews that ignore missing prerequisite resources; unexposed UI-equivalent cues (pulsing active relics, turn activity counters, character-specific states); missing killing-turn combat history; frequent `Unresolved` diagnostics. | Fix in C# Mod (`Observation/*MechanismReader.cs`, `MacroMechanismReader.cs`, previews). Keep extractors strictly fail-closed: log unresolved paths as diagnostics and omit them from model input. |
| **2. Execution, auto-progression, or projection bug** | Settlement reporting `ready` before async shop/reward animations finish; action batches not stopping at selection boundaries; auto-claims breaking relic constraints; role projections stripping needed public facts (such as `elites_fought` or upcoming route); duplicated payload bytes; unnecessary context resets hurting prefix cache. | Fix in Python harness or Mod execution/projection layer. |
| **3. Prompt bias, false anchor, or dead instruction** | Hard numeric anchors that misjudge Boss rest thresholds; contradictory or cross-file repeated drafting rules that skew attention; dead instructions in minimal mode referencing disabled notes or reviews; forced `reason` templates that encourage excuse-hunting. | Edit the owning role's system prompt or tool description directly. Remove misleading anchors, duplicates, and dead text. |
| **4. Generalizable drafting or macro anti-pattern** | Treating cycle-neutral cantrips as net draw; ignoring hand-slot tax from retained unplayable cards; adding side packages after a core exists; buying mediocre shop cards while leaving basic cards unremoved. | Expose an objective metric first (such as `deck_stats` cycle metrics or route visibility); add at most one concise, cross-character rule if the gap persists. |
| **5. One-off slip, reasonable tradeoff, or RNG variance** | An isolated arithmetic miss despite clear rules, a defensible risk tradeoff, or small-sample seed luck. | **Do not edit prompts or add card-specific rules.** If the rule is already stated clearly and better calculation alone would solve it, stop here. |

## 4. Rules for editing state, metrics, and prompts

### Prefer code fixes and objective signals over more prose

- **Expose missing public facts directly.** If a role cannot see upcoming map rooms, elites already fought, or pulsing relics, project those facts cleanly rather than asking a prompt to guess from floor numbers.
- **Precompute multi-row arithmetic—but never fabricate noisy proxies.** Objective counts and averages that take many lines of arithmetic (such as `circulating_avg_energy` excluding unplayable curses and X-cost cards, or `cycle_turns`) are high-leverage code additions. Conversely, if native structured data cannot represent a concept accurately (for example, counting every card or relic that triggers exhaust), do not ship a broken heuristic counter—let the agent read the cards directly.
- **Pair data with its reader, and decouple shared prompts.** Whenever a role receives battle reports or stats, load the matching format explanation. Keep field-name anchors inside role-specific prompts rather than shared files when only one role receives that field.
- **Sweep the entire class when you find one gap.** When one character or relic exposes a class of preview bug, reward-screen alternative, or auto-claim hazard, audit all characters and native hooks for the same pattern in one pass.

### Keep prompt edits surgical and resilient

- **Guard against patch-on-patch bloat.** Stacking fixes across multiple prompt files inflates system prompts, repeats the same warning in several places, and tilts model attention away from core priorities (often causing worse regressions than the original issue). Keep each rule in one owning file and measure net character/word growth on every edit.
- **Isolate act-specific thresholds physically.** Keep act-stage guidance in mutually exclusive act blocks (`deck-act-early`, `deck-act-mid`, `deck-act-final`) so early-act survival leniency never leaks into Act 2 or Act 3 decisions. When a late-game issue appears, edit only the final-act block rather than global drafting text.
- **Write in game invariants, never hardcoded names or fixed constants.** Avoid card names, relic names, Boss names, rare-card assumptions, and fixed numbers like `3-energy` or `5-card hand`. Use `base_energy`, `base_draw`, and universal categories (net positive draw vs. cycle-neutral cantrips; fight-long scaling multipliers vs. flat value; front-loaded kill speed vs. sustainable defense).
- **Use leaning guidance rather than rigid absolutes.** Avoid `must`, `never`, `always`, `only`, hard deck-size caps, or duplicate bans; prefer leaning phrasing (`lean toward skipping`, `weak grounds for taking`). Explicitly distinguish genuine synergies from universal buffs or "fodder" excuses that apply to nearly every card.
- **Check factual notes for unintended incentives.** Before adding a "helpful clarification" to a prompt, ask how it might be misread literally—for example, noting that self-exhaust cards are excluded from cycle averages can accidentally incentivize taking weak exhaust cards as "free."
- **Respect ownership boundaries.** Pure state schemas belong in `state-format.md`; gameplay judgment belongs in the owning role's system prompt; tool argument and batch rules belong in `tools/definitions.json`; fixed game rules belong in Mod `mechanisms`. Never smuggle behavior instructions through notes, tool results, or state prose.

## 5. Validate cheaply and prevent regressions

Use the cheapest check that can actually falsify your hypothesis before running long integrated batches:

| Stage | What it verifies | When to run |
| --- | --- | --- |
| **Static checks & Mod offline sweeps** | `./scripts/check` (Ruff, strict Pyright, fake-HTTP pytest) and `./scripts/check-mod` (Core + Native tests, Release builds, zero-RNG / fail-closed assertions across native extractors). | Every code or Mod change. |
| **Assembled runtime-text inspection** | Inspect representative assembled system prompts and JSON payloads before committing; verify no dead instructions, field leaks, or unintended prompt bloat. | Every prompt, tool description, or projection change. |
| **Independent two-axis review** | Parallel read-only review on **Standards / Model-Text** and **Spec / Correctness** (especially fail-closed IL extraction, replay/takeover compatibility, and cross-module contracts). Verify every review comment against source code before adopting or rejecting it. | Every non-trivial fix batch. |
| **Balanced offline decision replay** | Run `sts2_harness_eval` decision replay on a curated set of unambiguous recorded decisions (`50%` skip / `50%` take) that includes **anti-overcorrection guardrails** (previously passing early/mid/late picks and skips so neither "always take" nor "always skip" passes). Always sample multiple runs per case (`3–5` samples; never judge a regression from `n=1`). | Every drafting, prompt, or projection change. |
| **Targeted short runs (Act 1–2)** | Same-seed partial runs stopping after Act 1 or early Act 2 to measure weighted token cost, payload size, and prefix cache hit rate in minutes. | Context layout, caching, or compaction changes. |
| **Integrated multi-seed runs** | Parallel full runs across fixed comparison seeds (to reuse baseline data) plus fresh unseen seeds to check for over-fitting. Change one cohesive group of variables per cycle. | After offline checks pass, to verify end-to-end gameplay impact. |

### Regressions and stopping discipline

- **Isolate prompt regressions surgically.** When an offline decision benchmark drops on a previously passing case, diff the exact prompt lines loaded for that case and read the verbatim `reason` echo. Revert or narrow the offending sentence, then smoke-test the affected subset immediately.
- **Avoid low-yield tactics.** Do not add code quotas or deck-size hard limits, do not reorder tool JSON fields (`reason` before `choice`) expecting better reasoning, and do not add strict schema limits on free-text notes that risk aborting live runs on validation retries.
- **Know when to stop.** Once infrastructure/Mod defects are closed, balanced offline guardrails hold, and integrated runs show healthy process metrics (act-exit deck composition, removals, elites, and Boss readiness), accept remaining marginal variance instead of over-tuning prompts for isolated edge cases.

## Related code

- Public recordings and inspection: `sts2_harness.journal.Journal.operations`, `Journal.summaries`, and `uv run sts2 journal`.
- Delivery reproduction: `sts2_harness_eval.replay.reach`.
- Offline decision extraction and replay: `sts2_harness_eval.decision_extract` and `sts2_harness_eval.decision_replay`.
- Natural case admission and live takeover: `sts2_harness_eval.cases.admit_case` and `sts2_harness_eval.takeover.take_over`.
- Declarative prompt/material comparisons: `sts2_harness_eval.variants.MaterialVariant`, `HistoricalStart`, and `SceneStart`.
- Paired suite execution: `sts2_harness_eval.suite.run_suite` and `read_suite`.
