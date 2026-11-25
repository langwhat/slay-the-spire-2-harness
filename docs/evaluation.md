# Evaluation

Three offline evaluation paths share recorded evidence but answer different questions:

- [Offline software replay](library.md#offline-software-replay) checks that a recording's model inputs are reproduced.
- [Frozen decision evaluation](#frozen-decision-evaluation) runs recorded decisions through the current role with fresh context.
- [Decision replay](#decision-replay) rebuilds a labeled Run or Deck decision with its recorded history.

[Gameplay retrospectives](gameplay-retrospectives.md) describes how to choose among them after a run.

## Frozen decision evaluation

The stable evaluator runs a fixed set of recorded decisions through the current role and
context assembly. It starts with the current task and observation, with empty notes and no
past conversation, battle report, or route plan. It serves read-only queries from each
case's frozen data, applies in-memory note updates, and stops before the first route-plan
or game-action proposal. No game action is sent. Combat cases require a frozen history
inspect reply whose full observation matches the task anchor. An optional `combat_history`
must agree with that reply. The same facts are included in the first full task and served
to later history queries; an absent or inconsistent history is unsupported. This task field
is specific to evaluation and is not included in the production task. Missing facts and
unverifiable proposal legality are reported as `unsupported` with the unknown cause; old
recordings that cannot satisfy the current schema are also unsupported, without guessing.
Proposal status records local validation as `valid`, `invalid`, or `unknown`; `valid` does
not mean the action was executed. Trials without a captured proposal have no proposal status.
Route proposals keep their submitted stretch order. For a valid route, the report shows
whether it reaches a terminal Boss, the planned Elite count, and whether all ordinary map
continuations from the first room lead to an Elite before a certain rest site or Boss.
The low-HP flag uses 40% of maximum HP. Future special movement is not modeled, and route
metrics do not score or execute a proposal. Unverifiable metrics are reported as unknown.
At the evaluation-file boundary only, an old Run observation missing `base_orb_slots`
receives the verified native character default in memory: three for `CHARACTER.DEFECT`
and zero for `CHARACTER.IRONCLAD`, `CHARACTER.SILENT`, `CHARACTER.REGENT`,
`CHARACTER.NECROBINDER`, and `CHARACTER.FOOL`. The source file is unchanged, and the
derived field and basis appear in the trial and report. Other characters with a missing
value remain unsupported.

To enrich a historical single-task case with provable combat encounter identities, first
replay its original case using the public and private packages from the recorded run and a
compatible historical extractor. The source packages must be on `PYTHONPATH`; select the
matching interpreter and provider for that recording:

```sh
PYTHONPATH=/frozen/public/src:/frozen/private/src /path/to/source-python \
  scripts/replay_source_case.py --source-case /evidence/source-case.json \
  --extractor-directory /frozen/extractor/src/sts2_harness_eval \
  --provider-module sts2_knowledge.entry --output /evidence/replay.json

uv run python -m sts2_harness_eval.historical_fights \
  --case /evidence/frozen-case.json --source-case /evidence/source-case.json \
  --source-replay /evidence/replay.json --case-sha256 CASE_SHA256 \
  --source-replay-sha256 REPLAY_SHA256 --output /evidence/derived-case
```

The first step checks the source model inputs, tool definitions, and request digests with
recorded dependencies, and writes whether replay succeeded. The second binds that proof to
the source and frozen cases, reads the archive without changing it, verifies its anchor and
native blob digests, and writes a new case and provenance in a new directory. It preserves
the case other than newly established encounters. Missing, conflicting, repeated, or
interrupted encounter evidence remains unknown. The archive prefix must be complete and
remain within one game and run; multi-task cases and unverified source replay are unsupported.
Neither step calls a model or game. Replay rejects a nonempty archive WAL before or after
reading, and enrichment requires both recorded checks and checks again around its own read.
Use a frozen archive with no writer or a trusted frozen copy: checks before and after reading
cannot rule out transient concurrent writes. Synthetic replay evidence is
rejected. The `verified` status is not a cryptographic signature: review the replay evidence
and its source environment, and use the recorded hashes to bind it to the inputs.

Keep the plan, case files, snapshots, annotations, and output outside this repository.
A plan is a JSON file such as:

```json
{
  "configurations": [
    {"id": "current-high", "effort": "high", "prompt_version": "current"}
  ],
  "cases": [
    {
      "id": "q1506", "role": "deck", "case": "cases/q1506.json",
      "catalog": "snapshots/q1506-catalog.json",
      "documents": "snapshots/q1506-documents.json",
      "configurations": ["current-high"], "repetitions": 2
    }
  ],
  "historical_means": [
    {"role": "deck", "effort": "high", "requests_per_trial": 1.5,
     "weighted_tokens_per_request": 6851.4, "source": "prior evaluation"}
  ]
}
```

Paths are relative to the plan. Each case uses the `DecisionCase` JSON schema in
`sts2_harness_eval.decision_cases`, including its own labels and recorded native reads.
Nonempty frozen catalog search requires a complete `search_index` with a `source` and
one raw native `definition_id`, `name`, and `text` for every definition. Native search
uses these fields before display text is rendered, so rendered lookup data cannot replace
them. Results expose only rendered definitions. Search supports ASCII and common Chinese
ideographs and punctuation when all IDs are printable ASCII; other text is unknown
because native ordinal matching and ordering may differ from Python's Unicode behavior.
Roles are `run`, `deck`, and `combat`. Omit `documents` only when the case did not
enable entry documents. A catalog file uses the `FrozenCatalog` schema:
`catalog` contains the native catalog metadata, `definitions` contains native lookup
entries, and `complete` asserts the definition set covers the entire native catalog.
Only a verified complete snapshot can answer arbitrary searches. Partial snapshots can
answer lookups for included IDs, but missing IDs and searches are unknown. A verified
complete snapshot reports missing lookup IDs as absent from the native catalog. Document
snapshots use `FrozenDocuments`: declare all covered subjects (or `complete_subjects`)
and freeze the documents, requested resources, and role skill lists. Inspect queries are
answered only by matching native reads in the case.
The snapshot schemas are in `sts2_harness_eval.frozen_catalog` and `frozen_documents`.
Use `FrozenDocuments.snapshot_json()` to save document snapshots: it preserves material
IDs, content hashes, and bodies rather than the model-visible text-only serialization.

By default, each configuration snapshots the packaged prompts before the run. To compare
another version, set `prompt_directory` to a directory containing the corresponding
named Markdown files. The version label and the digest of the actual prompt set are both
recorded. The case, catalog, and document files are read into memory before calls;
their hashes are recorded per trial. Prepare full snapshots from trustworthy sources:
an exact-query cache alone is not a complete catalog or document snapshot.

Pick `--concurrency` to fit your machine's free memory and whatever else is running.
Configure the builtin model as for gameplay and run:

```sh
uv run python -m sts2_harness_eval.stable_suite \
  --plan /private/evaluation/plan.json --output /private/evaluation/result \
  --env-file .env --concurrency 4
```

The output directory must be new. This entry point accepts only the Responses protocol
and rejects others before sending anything; gameplay itself supports all three. The
evaluator adds no retries or replacement trials of its own. Before sending,
the evaluator prints estimated trial and request counts and weighted tokens from the
provided per-role, per-effort historical means. Missing means remain unknown. Estimates
are informational, never a spending limit. Weighted tokens are uncached input plus 5%
of cache-read input plus five times output; reasoning is already included in output.
Successful notes and read-only queries continue the current trial; a text-only response,
unknown tool, or rejected or failed query ends that trial as unknown without a correction
request. Other scheduled trials continue, and invalid first actions remain recorded.

`report.md` lists every completed trial's choice, reason, verdict, token use, and
reasoning tokens; unanswered usage is explicitly unknown. Unlabeled choices are not
scored. When configurations differ, the report includes both choices and reasons.
Each `trial-*.json` preserves attempt-level request and response bytes, status, failures,
usage, receipt time for `response.completed`, and stream-close time separately. Failed
attempts remain in totals when usage is available. These files may contain private model
context and should be handled with the same care as the source cases.

The evaluator's role probes (`RunProbe`, `DeckProbe`, `CombatProbe`) list `CaptureDecision` before the production agent, so its capture and query-closing overrides take precedence while the agent supplies context assembly and dispatch.

## Decision replay

Decision replay rebuilds a labeled Run decision with the installed public code and
current entry materials. It uses the production `RunAgent`, `Context`, projection,
prompt reader and tool catalog. Its game fixture permits recorded native reads;
it cannot submit actions, present routes or control a game session.

### Commands

Keep cases and results outside the public repository. A case file or a directory
of case JSON files can be supplied. Each output directory must be new.

```sh
python -m sts2_harness_eval.decision_replay recorded \
  --cases /private/cases --output /private/results/recorded

python -m sts2_harness_eval.decision_replay replay \
  --cases /private/cases --output /private/results/current \
  --env-file /private/config.env --samples 3 --variant run
```

`recorded` scores the source decision without model requests. `replay` uses the
configured builtin model and defaults to three independent samples per case. It
does not add evaluation retries; adapter retries are retained as separate
attempts. Results are saved after every completed sample.

A private entry point can supply its current knowledge materials:

```python
from sts2_harness_eval.decision_replay import main
from private_package.entry import entry_materials

raise SystemExit(main(materials=entry_materials()))
```

`--variant deck` shares the live card-reward role's input assembly. Its prompt
loads `deck-tasks.md`, `run-report-reader.md`, `state-format.md`, `deck.md`, the
current act's threshold, and `deck-choice.md` in that order. It only exposes
`run_deck_choice`. It sees the visible card reward groups to plan jointly (or the
focused single offer for legacy single-offer cases), current
player/deck/relics/potions, notes, statistics including the average fixed energy
cost of playable circulating cards, definitions/mechanisms, and the latest battle
report/review. It projects the remaining recorded route when available and omits the
map and earlier reward interactions. Guidance is selected for the Deck audience from
owned objects and the visible card offers, rather than the Run audience. Non-card
selection cases are `not_applicable` for this variant.

### Case extraction

Run extraction in a separate process importing the **exact frozen public source
and private entry-material provider from the source run**. Add only this
evaluator's modules to that package's search path when they were not present in
the source version. Do not substitute current source while extracting historical
inputs. Preserve the source archive and record source revisions and file hashes
alongside the private dataset index.

```python
import asyncio
from pathlib import Path

from sts2_harness.entry_materials import MaterialContext
from sts2_harness_eval.decision_cases import DecisionLabels, DecisionSource
from sts2_harness_eval.decision_extract import ExtractionAudit, extract_case
from private_package.entry import entry_materials

audit = ExtractionAudit()
case = asyncio.run(
    extract_case(
        DecisionSource(
            round="source-round",
            seed="source-seed",
            floor=1,
            model_sequence=123,
            archive=Path("/private/source.sqlite"),
        ),
        DecisionLabels(good=("CARD.EXAMPLE",), bad=(), comment="User judgment"),
        source_documents=entry_materials().documents(MaterialContext(10)),
        audit=audit,
    )
)
Path("/private/cases/example.json").write_text(case.model_dump_json(indent=2), encoding="utf-8")
```

The extractor replays the journal to the requested model anchor without model
or game calls. It checks the original inputs, tools and encoded request digest.
Cases retain raw native replies and harness-owned semantic state/history,
including original assistant native replies needed to reconstruct earlier
interactions within the role context. They do not retain rendered prompts, tools,
projections or knowledge bodies. A Deck case can retain the active Run plan as
native edges reconciled at the reward position. Labels never enter the model
input. Cases can contain real model text, so store them as private data.

The optional source document provider repairs missing historical `auto_roles`
metadata only: document identity/resource and subjects must match the archived
document. Archived document bodies are never replaced during extraction.

One narrow source-replay normalization accepts a permutation of uniquely keyed,
otherwise identical definition entries in an **unused** compaction baseline.
Compaction must be unchanged, the delivered input unchanged, and every other
preparation field identical. Changed definition bodies or a used baseline are
rejected. `audit.unused_baseline_permutations` records sequence and original,
reconstructed and canonical hashes; `audit.validated_model_sequences` lists
successfully checked original requests. Keep this audit with extraction evidence.
This normalization does not relax actual request/input/tool/digest checks.
The recorded action must satisfy the captured source tool schema and have source
completion evidence. Matching tool feedback is authoritative; absent feedback
requires an exact completed native selection before the next model request, on
the same route, plus a raw deck-count increase for a taken card. The audit records
the evidence sequence. Rejected proposals are not recorded choices.

### Results and acceptance

Native instance references are resolved to exposed native definition IDs or
`skip`. A single-choice label can list several acceptable alternatives. A
multi-selection passes only when it contains every `good` ID and no `bad` ID.
Other known bad choices fail; an unclassified single choice is `unlabeled`.
Another game action, another offer, or an illegal native selection is `deviated`.
The Run variant can update notes before the decision; the Deck variant only reads them.

Technical incompatibility, such as an unmappable historical notes shape or a new
native read missing from the case, is `unsupported`, not a model failure or
deviation. Preserve the raw case and re-extract from its source when a faithful
state mapping is unavailable. Recorded `run_plan` calls with a top-level `edges`
field or unparseable JSON are `unsupported`, even when rejected or aborted.
Parseable invalid current-format arguments can retain their recorded rejection.
An incompatible role is `not_applicable`. Both
categories are excluded from the reported eligible-sample denominator.
The CLI keeps a recorded-only typed header for incompatible native case formats:
`recorded` can still score their original choices, while `replay` writes one
`unsupported` row and continues with compatible cases. `load_case` exposes this
same boundary; a `RecordedDecision` without a full `DecisionCase` requires
re-extraction before replay.

`results.json` retains complete prepared request bodies, native model replies,
native tool calls, reasons, usage/cache counts, timings, per-attempt evidence and
request size breakdowns (system, tools, knowledge, volatile text, encoded bytes,
estimated tokens and digest). It contains no model credentials. `report.md`
summarizes choices, verdicts and usage. Treat both as private artifacts.
Usage summaries retain the known/total attempt coverage for each metric. Missing
usage is `unknown`; a partially known total reports only its known subtotal and
is marked `partial`. Zero-request scoring is identified separately.

For byte-equality acceptance, rebuild a source case under its exact public and
private versions with `rebuild_case(case, client, materials=...)`, then compare
`rebuilt.prepared.measurement.digest` with the source model attempt's
`protocol_encoding_digest`. Preparation alone makes no model request. Keep the
full actual prepared body for independent inspection. After changing prompts or
projection code, rebuilding the same unchanged case must use those new owners.

The asynchronous APIs are `extract_case`, `rebuild_case` and `sample_case`.
`sample_case(..., variant=DecisionVariant.RUN)` evaluates one sample;
`recorded(case)` scores the original choice; `write_results(path, samples)` writes
the same JSON and Markdown artifacts as the CLI.
