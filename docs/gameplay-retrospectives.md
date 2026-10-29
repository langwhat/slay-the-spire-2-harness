# Gameplay retrospectives

Use this workflow to investigate completed runs and turn supported findings into reproducible evaluations. Review the Run's decisions across an act before treating an individual battle as the root cause. A defeated run is an investigation trigger, not proof of a bad decision, and the number of times a choice recurs is not a diagnosis.

## 1. Freeze the evidence

Retain the closed recording, recording identity, actual model configuration, prompts and tool definitions, knowledge revision and loading settings, core and native build revisions, game version, and run recipe. Confirm the run's natural result or identify its technical interruption. Verify that owned processes and temporary resources were cleaned up.

Extract one chronological public trace per recording with a single versioned extractor shared by every recording in the batch. Use `Journal.operations()` for confirmed actions, rejections, observations, and inspected public details, and `Journal.summaries()` for model-request positions, roles, streams, delivery, and failures. Query usage and timing metadata separately. Under a restricted review, keep recorded model messages, notes, handoff reports, and opaque continuation content out of reviewer material. Existing replay machinery may consume those records without displaying them.

Separate three kinds of action:

- **Model decisions:** actions linked to a delivered request of the same role within the same execution unit. Use admission and batch boundaries; the nearest earlier request alone is not sufficient.
- **Automatic progression:** actions the core takes without a model request, such as a single reachable map destination, a lone proceed control, or a single chest. Never attribute these to model judgment. Leave attribution unknown when execution boundaries or the decision source cannot establish it.
- **Native consequences:** state changes that follow from game rules after an action.

An observation proves what the game exposed to the harness; it does not by itself prove what the model received. `replay.reach` at the recording's own core revision checks that the recorded game, material, and model dependencies reassemble typed content equal to the recorded model input and tool definitions up to an anchor. Serialized bytes, including mapping key order, are not compared. A successful replay verifies assembly of the recorded request; it does not make a separate rendering of one observation equivalent to its delivered content. Verify any claimed public fragment against the same Context history and projection boundaries programmatically, without exposing restricted message bodies. Model-authored content in the same input (earlier replies, notes, handoff reports) stays unknown, and provider-side request conversion is outside this check. Investigate a mismatch at a different revision before classifying it as a delivery defect; it can reflect changed assembly behavior. A replay rejection can also come from nondeterminism in assembly or in the replay check itself; reproduce it under varied process conditions before treating it as evidence about the recording or its delivery. Treat any reconstructed deck plan as the reviewer's hypothesis, not a quotation of the agent's intent.

**Done when:** every sampled decision has a recording ID, sequence, action kind, trusted preceding facts, confirmed effects, delivery status (verified, unverified, or unknown parts), and an explicit list of missing evidence. Technical interruptions are separate from defeats.

## 2. Build the Run account

For each act, record from public observations: deck changes, upgrades, removals, transformations, potion acquisition and use, shop spending, event choices, route commitments, rest-site actions, and battle-entry and battle-exit HP. Use the observation's full map and boss identity, not only the immediate destination choices, when reviewing routes. Distinguish enemy damage, self-inflicted HP loss, and healing when evidence permits; a net HP change alone cannot separate them.

Track previously published public descriptions and previews up to the boundary within the applicable role and Context lifetime; a later snapshot can omit supplementary information already delivered earlier. Do not import a later version into an earlier decision. Treat temporarily missing cards or resources as observations to explain through native mechanics, not automatically as permanent removals.

Mark which facts were visible to the agent and which the reviewer derived. For example, a rest-site observation may list the deck and each card's upgradability without upgrade previews. If neither earlier public delivery in that Context nor inspection supplied the preview, the concrete upgraded effect is a reviewer derivation; upgradability alone does not prove that effect was delivered.

Review four connected questions:

- **Deck capability:** What feasible damage, defense, scaling, and setup lines do the currently known cards and relics support against the known boss and plausible elites? Does a pick add capability, improve cycling, or delay important cards? Account for draw, filtering, retrieval, exhaustion, energy, and status disruption rather than deck size alone.
- **Route:** Which visible challenges and resource opportunities were reachable? Compare their risks against the current deck and resources. A hidden event outcome or later random drop was not available to the decision maker.
- **Spending and events:** Compare keeping resources with available purchases, removals, and combinations. Check marginal value, actual acquisition triggers, and known future opportunities. Evaluate an event using its public costs, choices, and uncertainty.
- **Rest and upgrades:** Compare the survival value of healing at that HP against the next visible threats with the repeated value of concrete upgradable cards. Trace why healing became necessary: earlier routing, spending, potion allocation, deck limitations, or avoidable combat losses.

Use Combat to test the explanation. Separate a deck that lacked a capability, one whose key cards started too slowly, one that possessed the capability but did not use it, and a misunderstanding of enemy mechanics or resources. Investigate card access, setup time, resource use, and native mechanics before recommending a deck change. Check secondary effects and counters before calling an apparently redundant action wasteful.

The current public game state remains observable across acts and roles. Strategic intent can also pass through model-authored notes and battle reports. When those bodies are unavailable, a failure to carry a plan across that boundary is unresolved, not inferred.

Include reasonable decisions as controls. Sample across ordinary and difficult situations, not only terminal mistakes. Record how samples were chosen; an initial triage made after seeing outcomes is outcome-aware.

**Done when:** each act has a capability/resource account, plausible pressure points, and at least one reasonable control. Unknown deck contents, unavailable inspections, and unreadable notes remain explicit.

## 3. Investigate a decision without hindsight

First assess a candidate using only facts available at its decision boundary. Build a boundary packet from the same extractor: the boundary observation, public history up to it, and the legal actions. Exclude later operations, the final floor and result, and the variant identity. Give the packet to an assessor who has not seen the run, and ask for the acceptable choices and the reasons for each before revealing the actual action. Use a fresh assessor for each boundary when later packets from the same trajectory could reveal an earlier choice or its consequences. Keep each assessment locked before exposing its continuation. Then examine the actual continuation to investigate mechanism and impact. If a reviewer already saw later facts, report that limitation rather than claiming blinding.

For each candidate, record:

1. Evidence location, action kind, and the observed behavior.
2. Information available then, uncertainty, and delivery status.
3. A legal, affordable, reachable alternative or set of alternatives.
4. The proposed failure mechanism and the horizon over which it matters.
5. Evidence that would refute the explanation.
6. The smallest reproducible check and its present capability limits.

Investigate upstream layers before assigning a strategy error: native legality and timing; state identity and observation consistency; projection and delivery; tool rules and prompts; knowledge correctness and relevance; then strategic judgment. Multiple causes may contribute. Never infer the model's private reasoning from its action.

Classify the result as a **confirmed defect**, **testable strategy hypothesis**, **reasonable tradeoff**, or **unresolved**. "Unresolved" includes insufficient public evidence and unsupported recovery. Agreement between reviewers does not convert a hypothesis into a native fact. Rank findings by supported impact and tractability, not by how strongly an evaluator phrases them.

**Done when:** every proposed fix has a reproducible failure signal and a falsifiable explanation. Candidates without that evidence remain hypotheses; they do not become unconditional strategy rules.

## 4. Select the appropriate evaluation

Use the smallest experiment that can establish the claim:

| Check | What it can establish | What it cannot establish |
| --- | --- | --- |
| Fake HTTP tests | Execution, projection, recovery, and tool handling under scripted model replies | Any model decision quality |
| Public-state probe | A real model's first response under current materials at a recorded public observation, in a fresh context | The historical conversation and notes, native continuation, or downstream effect |
| Native laboratory construction | Specific native mechanics in the constructed scenario | Identical hidden random state or natural-history recovery |
| Verified case and live takeover | A continuation from a verified natural decision point to the case endpoint | Unsupported Run screens, earlier strategic decisions, or effects past the endpoint |
| Full runs | Integrated behavior, operational stability, and observed outcomes | Causality from a small number of stochastic trajectories |

Verify current support with `admit_case` before choosing an endpoint; an unsupported admission is a capability result, not a low-quality sample. Current support must be checked against the actual recording and implementation:

- Admission requires a headless recording with its seed and template, no recording gaps through the endpoint, and a ready map boundary reached from a monster, elite, boss, or event room. A target whose most recent map screen was reached from a rest site, shop, or treasure room is not admissible; record that selection bias.
- A natural Run case requires an already open ordinary card selection (choose at most one card, skip allowed) with a one-decision endpoint; it measures that pick, not the pick's later effect. A reward overview followed by opening and selecting in one batch is a different starting boundary.
- Combat cases end after a decision count or at the battle's end.
- `SceneStart` starts a fresh Combat request with explicit equal initial notes. It is not valid for a Run reward case.

Validate batch compatibility too: completed receipts and batch-boundary observations do not imply that admission supports the recording. A requirement for unrecorded intermediate observations is an implementation gap to investigate. Never fabricate those observations from the final batch state. Replayed intermediate identity reads are derived evidence, not original publications. Check assessment evidence extraction against the same real recording shape; test fixtures that attach observations to every write can hide this gap.

Route, shop, event, and rest-site checks remain public-state probes unless native restoration for them has been implemented and verified. A laboratory experiment can test a related mechanic but does not restore the historical Run decision.

Keep verified recovery facts immutable in `CaseStore`. Append findings with public evidence references in `AnnotationStore`. Preserve the true human or agent origin of annotations, rubrics, and judgments. Append reviews separately: accepting an agent finding never changes its origin. Do not create human records without an actual human decision.

Historical takeover replays the recorded prefix and changes only declared materials. Prompts and tool descriptions delivered before the anchor come from the recording, so test a change to them with a `MaterialVariant` that declares every recorded source version and the complete replacement text. Supplying a different prompt reader to an arm affects only reads after the anchor.

**Done when:** the selected experiment can test the proposed claim, or the claim is explicitly blocked by a documented capability gap.

## 5. Compare a targeted change

Before any model call, fix in an external plan: baseline and candidate revisions, model settings, knowledge configuration, starting mode, initial notes, cases and their partition, endpoints, finite per-arm request and time budgets, repetition count and order (use an even number of pairs when balancing AB and BA), the behavioral criterion, and the promotion rule. Keep non-target conditions equal. Preserve original cases and create separate candidate branches. A model-visible text change still follows the project's review rule for runtime model text before it is promoted.

Write criteria that permit multiple valid choices. For a card reward, store a `CardRewardRubric` with the acceptable card set and whether skipping is acceptable; for a battle, describe observable outcomes and prohibited behaviors. Record its author permanently.

Use `run_suite` for supported paired cases. Bind each assessment to the returned recording ID and actual branch. Keep all planned samples, failed starts, incomplete executions, and adverse outcomes; do not replace unfavorable samples with additional attempts. Network retry policy remains separate from decision budgets.

Without a judge, sample quality stays `Unassessed`. Do not convert it into a pass, a zero, or a single score. Report a mechanical rubric check, such as whether the recorded reward choice is in the acceptable set, separately as a derived measure computed from the assessment evidence. A human judgment can be appended later and selected with `read_suite`. If a semantic judge is used, make its inputs and output explicit and check it against independent judgments first.

Report separately: legality, rejections, and completion; endpoint attainment; criterion results; HP and potion outcomes where supported; requests, tokens, cache, and time from the branch recordings. Report counts per case and partition, not rates implying more precision than the sample supports. Historical takeover can resend equal content with different mapping key order from the original process; disclose this limitation when comparing serialized input or prompt-cache costs across processes or batches.

Promote a change only when the predefined criterion improves on development cases, held-out cases show no relevant regression, and no new incomplete or unsupported result is attributable to the change. A local reduction in rejected actions does not establish better deck construction, and a better pick does not prove a higher run win rate.

**Done when:** the comparison has complete identities and evidence, the claimed improvement matches its predefined criterion, and relevant regressions are resolved or block promotion.

## 6. Recheck the method in integrated runs

After validated fixes, freeze a new integrated batch and run the authorized scenario set. Use it to check operational stability, look for regressions, and test whether this workflow produces auditable diagnoses; it is not promotion evidence for a strategy change. Keep the original recordings and compare corresponding runs. Disclose every changed revision, including native builds and knowledge, alongside prompt changes.

Repeated seeds are development data. Decision points from a new trajectory on a repeated seed are weaker held-out cases than different seeds; label them that way, and retire any case from held-out status once it informs a change.

Reapply this workflow. Amend the method only when it missed a failure class visible in public evidence, produced an attribution later refuted, or could not produce a testable next step where one existed. Change gameplay behavior only for supported findings; do not invent a patch to complete a cycle.

Stop iterating when a reapplication finds no such method defect and the remaining findings are gameplay variance, reasonable tradeoffs, or documented capability limits. Winning every run, zero rejections, and optimal stochastic choices are not stopping requirements.

## Related code

- Public recordings: `sts2_harness.journal.Journal.operations` and `Journal.summaries`.
- Delivery reproduction: `sts2_harness_eval.replay.reach`.
- Natural case admission: `sts2_harness_eval.cases.admit_case` and `CaseStore`.
- Verified live continuation: `sts2_harness_eval.takeover.take_over`.
- Material variants: `sts2_harness_eval.variants.MaterialVariant`, `HistoricalStart`, and `SceneStart`.
- Append-only annotations, rubrics, and assessments: `sts2_harness_eval.annotations.AnnotationStore`.
- Paired comparisons and reassessment selection: `sts2_harness_eval.suite.run_suite` and `read_suite`.

The evaluation design follows task-specific objectives, representative cases, calibrated judgments, and explicit comparisons described in [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices).
