# Using the core as a library

These sections describe the Python APIs behind the CLI: model clients, recordings, single-run sessions, roles, and offline replay.

## Model client

The Python `sts2_harness.llm.client.Llm` client accepts a `ModelConfiguration` snapshot
and implements `complete(input, tools)` as an async context manager. It currently supports
Responses, Chat Completions, and Messages. Callers own the immutable `ModelInput` message tuple;
the client retains
no conversation. Tool definitions use concrete Pydantic argument models. Provider
schemas use `strict=False` to preserve optional-field semantics; tools remain responsible
for strict local argument decoding.

A completed `ModelReply` exposes read-only text/tool-call views, native continuation,
usage, and separate requested/response model metadata. `ResponsesReply.to_json()` and
`from_json()` preserve the complete ordered native output, including encrypted reasoning
and assistant phases in one immutable encoded snapshot. Decoded SDK items are independent
copies. `ChatCompletionsReply` retains a complete native completion and continues with its
native assistant message, including opaque continuation fields. `MessagesReply` retains the
complete native Message and continues with its ordered content blocks, including thinking,
signatures, redacted thinking, explicit nulls, and opaque fields. Thinking is not exposed
in the normalized text view. Messages groups tool results in the immediately following
user message before auxiliary text. Histories from another
protocol are rejected; protocol switching does not migrate existing context. Append the
returned assistant message and associated tool-result
messages to the caller's input; native output is sent once, without `previous_response_id`.
The adapter does not execute tools or return intermediate streamed argument fragments.

Responses requires `response.completed`; Chat Completions requires a complete `stop` or
`tool_calls` choice after consuming the stream, including any final usage chunk.
Messages requires closed, ordered content blocks and an explicit `message_stop` after
a supported `end_turn` or `tool_use` outcome. Server-tool blocks, `pause_turn`, `refusal`,
and unconfigured `stop_sequence` outcomes are unsupported.
Premature clean EOF,
connection failures, timeouts, rate limits, and overload are retried. Each attempt gets its
own configured timeout covering request start through final decoding. SDK retries are
disabled; the default 25 additional attempts wait 2, 4, 8, 16, then 32 seconds, totaling
702 seconds of backoff. Explicit output truncation, authentication, permission, quota,
model/configuration errors, and malformed terminal responses raise a typed
`ModelUnavailable` without automatic regeneration. Cancellation propagates to the caller.

Messages uses the official Anthropic SDK with an internal `max_tokens=16000` policy;
this is not a universal model maximum. Thinking is omitted to preserve the server default.
Nonempty effort accepts `low`, `medium`, `high`, `xhigh`, or `max`; service tier accepts
`auto` or `standard_only`. Unsupported target options fail explicitly without fallback.
Top-level ephemeral automatic caching uses the default five-minute TTL, without warmup
requests or additional block breakpoints. Total input includes uncached input plus actual
cache creation and read counts; missing counts remain unknown. Messages appends
`/v1/messages` to the configured base URL, preserving its path prefix. Each custom target
requires its own finite protocol acceptance check; passing fake HTTP tests does not
establish compatibility with a live provider.

Missing usage counters remain `None`. Cache hit ratio uses cache-read tokens divided by
all input tokens, including cache writes. `Usage.summarize_cache()` sums comparable token
counts and reports coverage instead of averaging request percentages. Cache configuration
belongs to the protocol adapter; no separate cache settings or warmup calls are introduced.

Protocol validation uses the official SDK with fake HTTP and controlled waits. It does
not establish real endpoint compatibility or game behavior. Inject an HTTP transport and
sleep callable into `Llm` to exercise these public boundaries without network calls.

## Recordings

Game, model, material, and control recordings share one local SQLite archive. Compose `RecordedGame` around the
logical `LiveGame` dependency; pass the same Journal as the live native audit receiver.
The caller supplies the recording identity and genuinely known initial conditions:

```python
journal = Journal.create(archive_path, manifest)
live = LiveGame(
    client,
    namespace=manifest.recording_id,
    audit=journal,
    game_instance_id=manifest.game_instance_id,
    run_id=manifest.run_id,
    wait_for_start=True,
)
execution = Execution(
    RecordedGame(live, journal),
    game_instance_id=manifest.game_instance_id,
    run_id=manifest.run_id,
    recovery=ToolRecoveryBudget(),
)
# Use Execution's run_unit, observe, inspect, catalog, and pause_until_resumed operations.
journal.close()  # After the last operation has finished.
```

These classes live in `sts2_harness.game`, `sts2_harness.execution`, and
`sts2_harness.journal`. Built-in admission waits for Start; external admission uses
`wait_for_start=False` and omits the recovery budget. Each dependency is sequential.
Use one live dependency and one Journal owner for a recording; reopening an archive
provides a read-only evidence view, not session continuation.

Each logical operation has one sequence. Native polls are child audit attempts whose
complete typed replies are compressed and deduplicated as they arrive. Catalog queries
and their subsequent current-state reads remain separate logical operations. A submitted
operation ID does not establish completion: only confirmed completed receipts advance
the game-operation cursor. Unfinished operations retain their request and arrived
attempts with an explicit incomplete result. Ordinary archive failures preserve gameplay
and report recording gaps; unwritable gaps remain in memory until storage recovers.
`journal.availability` distinguishes an unpersisted initial manifest from a usable archive;
initial disk failures also preserve gameplay and retry the same owned archive when writable.
Existing archives and invalid recording paths are rejected rather than overwritten.
Diagnostics default to the archive directory. `diagnostics.jsonl` and its two backups
are each capped at 10 MiB of UTF-8 bytes, including the active segment. `status.json`
updates on stage changes. INFO includes actual tool, batch, automatic-progression,
and role/act/battle results; batch summaries distinguish planned, confirmed, and
uncertain steps. Ordinary rejection and Pause are INFO; unknown effects and exhausted
failures are ERROR. Same-request retry warnings are combined for up to 60 seconds,
with cumulative failure totals on recovery or exhaustion. Journal attempts remain complete.
Embedded callers can temporarily construct `Diagnostics(..., level=logging.DEBUG)`
to include model encoding digests under the same output cap; prompts and wire bodies
are not diagnostic metadata. Failed destinations retry independently after one second.
The Journal is never rotated with diagnostics. Keep archives outside the repository.
Inspect an active archive through `sts2 journal` or a SQLite read-only connection; close
the connection before recording ends. External writers and checkpoint operations are
unsupported while its Journal is recording. Sealing failures leave the archive and WAL in
place and are diagnosed without interrupting ordinary gameplay or cancellation. Preserve
both files for inspection; no recovery command currently seals them afterward. Evaluation
binding and admission reject unsealed archives, as does pruning a scope containing one.
Evaluation artifacts require a successfully sealed standalone archive.

Inspect recorded facts without startup configuration or network access:

```sh
uv run sts2 journal --archive /path/to/journal.sqlite
uv run sts2 journal --archive /path/to/journal.sqlite --kind settle --around 12 --window 2
uv run sts2 journal --archive /path/to/journal.sqlite --role combat --node NODE_ID --errors-only
uv run sts2 journal --archive /path/to/journal.sqlite --view operation --sequence 12 --expand
uv run sts2 journal --archive /path/to/journal.sqlite --view request --sequence 15 --expand
uv run sts2 journal --archive /path/to/journal.sqlite --around 12 --expand --export evidence.json
```

Compose `sts2_harness.model_journal.RecordedLlm` around a live `Llm`, with the same
Journal, the actual role, and a distinct stream name for each new Context. Pass that
client to the Agent. Compose `RecordedDocuments` around `FileDocuments` for skill and
supplement lookups, and pass `RecordedPrompts(PackagedPrompts(), journal)` to either role's
`prompts` argument. The session creator supplies `Manifest.initial_notes` from its actual
initial value: `""` is known empty, while `None` is unknown. A Combat's opening notes never
backfill that manifest. Packaged tool definitions are code configuration loaded before
session creation; every model anchor records their actual schemas and named descriptions.

Each logical model request gets an anchor before its first attempt. Retry attempts share
that anchor; complete native outputs are durable before the provider stream exits.
Arrival and successful delivery are separate facts. The adapter records a canonical
protocol request encoding digest, distinct from the archival semantic input digest,
without saving duplicate provider bodies, headers, or SSE fragments. Safe failure kinds,
usage, and model metadata are retained. Model timeout and backoff still belong to `Llm`.

The first input in a stream is full; later inputs store a durable common prefix and new
suffix. Materials, documents, and native outputs use shared compressed content blocks.
Expansion restores named material IDs, versions, and bodies from the archive. A recording
gap cannot become the base of a later input. Source edits never repair or replace history.

The default timeline shows the latest 20 matching dependency records in global sequence
order; `--kind model` and `--kind material` filter dependencies.
`--role run|combat`, `--node NODE_ID`, and `--errors-only` combine with those filters.
With `--around`, all filters apply inside that fixed sequence window; otherwise the
latest 20 matching rows are selected. Unknown role or node associations remain null:
they are not inferred from a neighboring model request or a future observation.
The error filter includes failed, unknown, and incomplete dependencies. A complete
native model reply that has not been delivered is still incomplete, not a model failure.
Ordinary rejected actions, manual Pause, and orderly stopping are not errors.
Summaries use a small index in the same archive without expanding input or response
blocks; an inconsistent summary index is reported explicitly. `Journal.operations()`
selects game/control operations, `requests()` selects model anchors, and `materials()`
selects material reads. `request(sequence)` expands one anchor. Expanded CLI views include
historical inputs, replies, attempts, and observations; selective exports include the
manifest and gaps. Use `--around` with the timeline to inspect neighboring actions,
receipts, queries, and material reads. Sequence and confirmed game cursor express ordering;
they do not imply that an adjacent action was caused by a particular model request.

The current archive schema is version 16 and records each native reply's protocol explicitly.
`Journal.open()` provides read-only inspection. Schema versions are validated without archive
migration. Read older archives with the revision that recorded them. Native restoration
and live evaluation remain unsupported; the manifest does not claim exact native replay.

## Single-run sessions

`sts2_harness.live_session.live_session` assembles `ModClient`, `Llm`, `Manifest`, and an
archive path into one run. The manifest supplies the bound game/run identities and the unique
initial notes value; `initial_notes` must be explicit, including `""` for known empty notes.
Pass resources that the live context owns and closes:

```python
from sts2_harness.live_session import live_session

async with live_session(client, manifest, archive_path, model=model) as session:
    await session.start()
    ended = await session.run()
```

The live context releases the supplied client and optional model and closes its Journal.
`RunSession(manifest, dependencies)` borrows typed game, prompt, document, and model-factory
dependencies and owns one Notes instance, recovery budget, and role flow. It does not close
injected dependencies. Automatic entry guidance is enabled separately from tool documents.
`DocumentSource.auto_roles` limits automatic subject-matched guidance to the listed roles;
explicit document reads and knowledge lookups still return the complete document.
`DefaultFlow` waits for Start,
schedules Run and Combat, completes pending battle handoffs before creating the next
Combat, and replaces the Run role and its context when the native act changes. An unknown
act does not establish a transition. Boss encounters and rewards do not define the role
lifetime. A native terminal state ends the run without a battle review or further model call.
Strategy notes remain available across acts and are supplied to each Combat at its opening.
`start()` records a real Pause and waits for explicit Start before `run()` enters gameplay.
A supplied process Diagnostics instance is borrowed and remains open across run sessions.
It does not launch a game or select a character. Run chooses automatic progression or
strategy within the same execution path; shop entry actions remain separate units and
purchases and departure remain strategy decisions. Validation uses fake HTTP and local
archives; this does not establish complete real-game acceptance.

## Role contexts

In both modes, the next Run model decision after a confirmed native node change starts
a fresh context with the current observation, map and route plan, applicable guidance,
and latest battle evidence. Rebuilding waits for a complete native map and settled action
feedback. Ordinary gameplay also carries the current strategy notes. Queries and actions within the
same node stay in order, so opening a reward or continuing a shop purchase preserves the
local decision. A change of decision owner within one node does not reset this history.
The Run agent and route plan still last one act; Combat context lasts one battle.
Capacity-triggered compaction remains available for long local interactions. Failed or
uncertain actions retain the feedback needed for recovery. Full history stays in the
archive even when earlier nodes leave the model input. New archives record
`run_context_policy=node_local_v1` and `state_format=sections_v2` in schema 16; earlier schemas require their matching
core version for inspection and replay.

In both modes, an ongoing role receives action receipts and query data in tool results,
with fresh state delivered once in the next `act` task. Results retain their observation
when control leaves that role. Messages append within the active context between capacity
compactions.

## Roles and strategy notes

`RunAgent`, `DeckAgent`, and `CombatAgent` share the `Agent` loop and `Execution.run_unit`.
`DefaultFlow` hands card-adding selections and card offers on the reward screen to a
`DeckAgent`; every other Run decision, including shop purchases, stays with `RunAgent`.
A Deck role is created for the first undecided card offer in a node and kept while that
offer remains. It ends when the choice resolves or the node changes, and an offer
declined in a node is not handed to a new Deck role in that node.

`DeckAgent` subclasses `RunAgent` to reuse its notes, act-scoped context lifetime, state
enrichment, and route-plan view. It replaces the initial context, the reward view, and
tool dispatch, and it accepts only a catalog built with `ToolCatalog(..., focused_deck=True)`,
which advertises `run_deck_choice` alone. An ordinary Run catalog excludes that tool.
Each `ToolCatalog` owns its tool set; callers do not edit its definitions. Tool
descriptions and role prompts are the source of truth for model-visible rules.

Construct one `Notes(NotesState(...))` per run and pass it to the Run `ToolCatalog`; the
agents read the same owner. An empty initial string creates empty notes. Fields and their
limits are defined by `NotesState`. `run_notes(mode="write", ...)` replaces only supplied
fields, including clearing a field with an empty string. Combat receives the notes at
battle entry. Minimal mode has no notes tools or injected notes. The Mod distinguishes
rewards that open a selector from immediate card grants; declined immediate grants stay
unclaimed until the reward screen is left.

State delivery replaces complete named sections and sends stable-ID definitions once per
context. Run deck rows aggregate identical native instances with their actual card text.
`deck_stats` counts the current deck and reports native base draw and energy.
Reward takes and skips are counted per act; shop purchases do not change those counters.
`use.observed_plays` describes the last five sampled battle-history prefixes;
they omit unmeasured draw, damage, block, and energy statistics.

Notes are authoritative
in memory. Reads return the full text; replacement permits clearing; an edit requires one
exact unique match, including overlapping occurrences, and permits deletion. Failed edits
leave notes unchanged, while successful writes return only `{"updated":true}`.

`await role.enter_act(ActStart(observation))` loads guidance and packaged instructions for
that role's single act before installing its Context and delivery ledger. A required-material
failure requests Pause through `DecisionInterrupted`; explicit retry preserves the same notes
owner. Finish pending action feedback before entering an act. The first actual act or handoff
task in the new Context carries current notes, including an empty string; construction alone
does not consume them. Session orchestration creates a new Run role for each confirmed native
act transition and owns recording stream assembly.

## Offline software replay

`sts2_harness_eval` exports `reach`, `ModelAnchor`, `Reached`, `Rejected`, `RejectReason`,
and `ReplayResult`. Select a recording identity and model sequence from the Journal:

```python
from pathlib import Path
from sts2_harness_eval import ModelAnchor, Reached, Rejected, reach

async with reach(Path("/path/to/archive.sqlite"), ModelAnchor("recording-id", 15)) as result:
    if isinstance(result, Reached):
        print(result.confirmed_operation_cursor, result.semantic_digest)
    elif isinstance(result, Rejected):
        print(result.reason, result.sequence, result.detail)
```

Replay supports the default built-in Run/Combat flow without automatic entry guidance;
explicit document tools use recorded materials. It checks recorded inputs and dependency
order without contacting a model, game, or material source. `Reached` holds the original
RunSession coroutine inside the target model call only while the context is open; exit
cancels and awaits that coroutine. It consumes no target reply, restores no native game,
and provides no live takeover. Missing facts and mismatches return typed `Rejected` results.
