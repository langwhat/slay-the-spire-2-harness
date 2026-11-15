# Decision replay

Decision replay rebuilds a labeled Run decision with the installed public code and
current entry materials. It uses the production `RunAgent`, `Context`, projection,
prompt reader and tool catalog. Its game fixture permits recorded native reads;
it cannot submit actions, present routes or control a game session.

## Commands

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

`--variant deck` shares the live card-reward role's input assembly. It uses
`deck-tasks.md`, `deck.md`, relevant state-format instructions, and only
`run_deck_choice`. It sees one current card offer, current player/deck/relics/potions,
notes, statistics, definitions/mechanisms, and the latest battle report/review.
It omits map fields and earlier reward interactions. Guidance is selected for
the Deck audience from owned objects and the focused offer, rather than the Run
audience. Non-card selection cases are `not_applicable` for this variant.

## Case extraction

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
Path("/private/cases/example.json").write_text(case.model_dump_json(indent=2))
```

The extractor replays the journal to the requested model anchor without model
or game calls. It checks the original inputs, tools and encoded request digest.
Cases retain raw native replies and harness-owned semantic state/history,
including original assistant native replies needed to reconstruct earlier
interactions within the node. They do not retain rendered prompts, tools,
projections, knowledge bodies or candidate links. Labels never enter the model
input. Because history may include actual model text, store cases according to
the private repository's artifact policy.

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

## Results and acceptance

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
