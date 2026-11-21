# Using the core as a library

The Python APIs behind the CLI: model clients, recordings, single-run sessions, roles, and offline replay.

## Model client

`sts2_harness.llm.client.Llm` takes a `ModelConfiguration` and offers `complete(input, tools)`; use it as an async context manager. It speaks Responses, Chat Completions, and Messages. The caller owns the immutable `ModelInput` message tuple; the client keeps no conversation. Tool definitions use Pydantic argument models. Provider schemas are sent with `strict=False` so optional fields keep their meaning, and tools decode arguments strictly on their own side.

### Replies

A completed `ModelReply` gives:

- read-only text and tool-call views;
- the native continuation;
- usage;
- the requested and returned model names.

To continue, append the returned assistant message and the matching tool results to your input. Native output is sent back once, without `previous_response_id`. The client does not run tools or return partial streamed arguments.

How each protocol keeps its native output:

- **Responses.** `ResponsesReply.to_json()` / `from_json()` keep the complete ordered output, including encrypted reasoning and assistant phases.
- **Chat Completions.** `ChatCompletionsReply` keeps the full completion and continues with its native assistant message.
- **Messages.** `MessagesReply` keeps the full message and continues with its ordered content blocks, including thinking, signatures, redacted thinking, and opaque fields. Thinking is never in the text view. Tool results go in the next user message, before any extra text.

A history from one protocol cannot be continued with another.

### Completion and retries

A reply counts as complete only on these signals:

- **Responses:** `response.completed`.
- **Chat Completions:** a `stop` or `tool_calls` choice, after the whole stream (including a final usage chunk) has been read.
- **Messages:** closed, ordered content blocks and `message_stop` after `end_turn` or `tool_use`. Server-tool blocks, `pause_turn`, `refusal`, and unconfigured `stop_sequence` are not supported.

Early EOF, connection failures, timeouts, rate limits, and overload are retried. Each attempt has its own timeout, from request start to final decoding. SDK retries are off; the default 25 extra attempts wait 2, 4, 8, 16, then 32 seconds (702 seconds of backoff in total). Truncated output, authentication, permission, quota, model or configuration errors, and malformed responses raise `ModelUnavailable` and are not retried. Cancellation propagates.

### Messages specifics

Messages uses the official Anthropic SDK with an internal `max_tokens=16000`, which is a project policy rather than a model limit.

- Thinking is left at the server default.
- Effort accepts `low`, `medium`, `high`, `xhigh`, or `max`, and service tier accepts `auto` or `standard_only`. Unsupported options fail instead of falling back.
- Automatic top-level caching uses the default five-minute TTL, with no warmup requests or extra breakpoints.
- `/v1/messages` is appended to the base URL, keeping any path prefix.

### Usage and testing

Missing usage counters stay `None`. Total input counts uncached input plus cache creation and cache reads. The cache hit ratio is cache-read tokens divided by all input tokens, including cache writes. `Usage.summarize_cache()` sums comparable token counts and reports coverage instead of averaging per-request percentages.

To test without a network, inject an HTTP transport and a sleep function into `Llm`. Fake-HTTP tests check protocol handling; a real endpoint still needs its own check.

## Recordings

Game, model, material, and control records share one SQLite archive per run. Wrap the live game in `RecordedGame` and give the same `Journal` to `LiveGame` as its audit receiver:

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

These classes live in `sts2_harness.game`, `sts2_harness.execution`, and `sts2_harness.journal`. Built-in driving waits for Start; external driving uses `wait_for_start=False` and no recovery budget. Use one live game and one Journal per recording. Reopening an archive gives a read-only view, not a way to continue the session.

### What is recorded

**Operations.**

- Each logical operation gets one sequence number. Native polls are child attempts, with replies compressed and deduplicated as they arrive.
- A catalog query and the state read after it are separate operations.
- Only confirmed completed receipts advance the game-operation cursor. Unfinished operations keep their request and attempts, marked incomplete.

**Model requests.**

- Each logical request gets an anchor before its first attempt, and retries share it. Complete outputs are saved before the stream closes.
- The archive keeps a digest of the protocol request encoding, separate from the semantic input digest. It does not store provider bodies, headers, or SSE fragments.
- Failure kinds, usage, and model metadata are kept.
- The first input in a stream is stored in full; later inputs store a common prefix plus the new suffix. Materials, documents, and native outputs share compressed content blocks.
- A recording gap never becomes the base of a later input, and editing source files never changes history.

**Storage failures.** If the archive cannot be written, gameplay continues and the gap is reported. Unwritten records stay in memory until storage recovers. `journal.availability` tells an unsaved initial manifest apart from a usable archive. Existing archives and invalid paths are rejected rather than overwritten.

**Sealing.** If sealing fails, the archive and its WAL are left in place with a diagnostic. Keep both files; there is no command to seal them later. Evaluation refuses unsealed archives.

While a Journal is recording, inspect the archive only through `sts2 journal` or a read-only SQLite connection, and close it before recording ends.

### Diagnostics

Diagnostics go to the archive directory by default:

- **Log files.** `diagnostics.jsonl` and two backups are each capped at 10 MiB. The Journal is never rotated.
- **Status.** `status.json` is updated on every stage change.
- **INFO.** Covers tool, batch, automatic-progression, and role/act/battle results. Batch summaries separate planned, confirmed, and uncertain steps. Ordinary rejections and Pause are INFO.
- **ERROR.** Covers unknown effects and exhausted failures.
- **Retries.** Retry warnings for the same request are combined for up to 60 seconds, with totals on recovery or exhaustion.

`Diagnostics(..., level=logging.DEBUG)` adds model encoding digests; prompts and wire bodies are never written to diagnostics.

### Inspecting an archive

Inspection needs no configuration or network:

```sh
uv run sts2 journal --archive /path/to/archive.sqlite
uv run sts2 journal --archive /path/to/archive.sqlite --kind settle --around 12 --window 2
uv run sts2 journal --archive /path/to/archive.sqlite --role combat --node NODE_ID --errors-only
uv run sts2 journal --archive /path/to/archive.sqlite --view operation --sequence 12 --expand
uv run sts2 journal --archive /path/to/archive.sqlite --view request --sequence 15 --expand
uv run sts2 journal --archive /path/to/archive.sqlite --around 12 --expand --export evidence.json
```

The default timeline shows the latest 20 matching records in sequence order.

- **Filters.** `--kind`, `--role run|combat`, `--node NODE_ID`, and `--errors-only` can be combined. With `--around`, filters apply inside that window.
- **Unknown links.** A role or node that is not known stays empty; it is never guessed from a neighboring record.
- **Errors.** `--errors-only` shows failed, unknown, and incomplete records. A complete model reply that was never delivered is incomplete, not a model failure. Ordinary rejections, Pause, and orderly stops are not errors.
- **Order, not cause.** Sequence numbers and the game cursor show order. They do not show that a particular model request caused an adjacent action.

In Python:

- `Journal.operations()` selects game and control operations.
- `requests()` selects model anchors, and `request(sequence)` expands one.
- `materials()` selects material reads.

Summaries come from a small index and do not expand stored blocks. Expanded views include inputs, replies, attempts, and observations, and exports include the manifest and gaps.

### Recording a session

To record a session yourself:

- **Model.** Wrap the live `Llm` in `sts2_harness.model_journal.RecordedLlm`, giving it the same Journal, the role, and a distinct stream name for each new context.
- **Documents.** Wrap `FileDocuments` in `RecordedDocuments`.
- **Prompts.** Pass `RecordedPrompts(PackagedPrompts(), journal)` as `prompts`.
- **Initial notes.** Set `Manifest.initial_notes` from the real starting value: `""` means known empty and `None` means unknown. Combat's opening notes never fill it in.
- **Tools.** Tool definitions are loaded before the session starts, and every model anchor records the actual schemas and descriptions.

### Archive versions

`Manifest.schema_version` gives the archive format. `Journal.open()` checks it and does not migrate. Read an older archive with the core revision that recorded it. To continue a recorded headless run in a live game, see verified cases and takeover in [gameplay-retrospectives.md](gameplay-retrospectives.md#4-select-the-appropriate-evaluation).

## Single-run sessions

`sts2_harness.live_session.live_session` puts a `ModClient`, an `Llm`, a `Manifest`, and an archive path together into one run. The manifest supplies the game and run identities and the initial notes; `initial_notes` must be explicit, `""` for known empty.

```python
from sts2_harness.live_session import live_session

async with live_session(client, manifest, archive_path, model=model) as session:
    await session.start()
    ended = await session.run()
```

`start()` records a real Pause and waits for Start; `run()` then plays until the run ends. The live session closes the client, the model, and the Journal when it exits. `RunSession(manifest, dependencies)` borrows its game, prompt, document, and model-factory dependencies without closing them. It owns one `Notes`, one recovery budget, and the flow. A borrowed `Diagnostics` stays open across sessions. A session never launches the game or picks a character.

`DefaultFlow` handles Run, Deck, and Combat, as described in [architecture](architecture.md#how-a-run-flows):

- It finishes pending battle handoffs before creating the next Combat.
- It replaces the Run role when the native act changes. An unknown act number is not a transition, and Boss encounters or rewards do not end the Run role.
- A terminal state ends the run with no review or further model call.
- Strategy notes last the whole run and are copied to each Combat at its start.

Automatic entry guidance is enabled separately from tool documents.

## Role contexts

The Run agent and its route plan last one act. After a confirmed node change, the next Run model decision starts a fresh context, once the map is complete and action feedback has settled. The context contains:

- the current observation, map, and route plan;
- applicable guidance and the latest battle evidence;
- in ordinary mode, the current notes.

Within a node, queries and actions stay in one conversation, so opening a reward or continuing a shop visit keeps the local exchange, even if the decision owner changes. Combat's context lasts one battle. Capacity compaction handles long local exchanges. Failed or uncertain actions keep the feedback needed to recover. Earlier nodes leave the model input but stay in the archive. Archives record this as `run_context_policy=node_local_v1` and `state_format=sections_v2`.

While a role is active, action receipts and query data arrive in tool results, and the next `act` task delivers fresh state once. Results keep their observation when control leaves the role. Messages are appended between compactions.

## Roles and strategy notes

`RunAgent`, `DeckAgent`, and `CombatAgent` share the `Agent` loop and `Execution.run_unit`. `DefaultFlow` sends card-adding selections and card offers on the reward screen to a `DeckAgent`; every other Run decision, including shop purchases, stays with `RunAgent`. A Deck role is created for the first undecided card offer in a node and kept while that offer remains. It ends when the choice resolves or the node changes. An offer declined in a node is not handed to Deck again in that node.

`DeckAgent` extends `RunAgent` to reuse its notes, state enrichment, and route-plan view. It has its own initial context, reward view, and tool dispatch. It only accepts a catalog built with `ToolCatalog(..., focused_deck=True)`, which offers `run_deck_choice` alone; an ordinary Run catalog excludes that tool. Each `ToolCatalog` owns its tool set. Tool descriptions and role prompts are the source of truth for model-visible rules.

### Notes

Create one `Notes(NotesState(...))` per run and pass it to the Run `ToolCatalog`; all agents read the same owner. `NotesState` defines the fields and limits:

- `core`, up to five distinct entries;
- `plan`, up to 300 characters;
- `other`.

`run_notes(mode="read")` returns the full notes. `run_notes(mode="write", ...)` replaces only the fields supplied, and an empty string clears a field. A successful write returns `{"updated":true}`. Notes live in memory. Combat gets a copy at the start of each battle. Minimal mode has no notes tools or injected notes.

The Mod tells rewards that open a selector apart from immediate card grants. A declined immediate grant stays unclaimed until the reward screen is left.

### State delivery

State is delivered as complete named sections, and stable-ID definitions are sent once per context. Run's deck rows group identical native instances with their actual card text. `deck_stats` counts the current deck and reports native base draw and energy. Reward takes and skips are counted per act; shop purchases do not change those counts. `use.observed_plays` covers the last five sampled battle-history prefixes and leaves out unmeasured draw, damage, block, and energy statistics.

### Entering an act

`await role.enter_act(ActStart(observation))` loads guidance and packaged instructions for that act, then installs the context. Finish pending action feedback before calling it. If required material fails to load, it raises `DecisionInterrupted` to request Pause, and a retry keeps the same notes. The first real act or handoff task in the new context carries the current notes, even when they are empty. Session orchestration creates a new Run role for each confirmed act change and assembles the recording streams.

## Offline software replay

`sts2_harness_eval` exports `reach`, `ModelAnchor`, `Reached`, `Rejected`, `RejectReason`, and `ReplayResult`. Pick a recording identity and model sequence from the Journal:

```python
from pathlib import Path
from sts2_harness_eval import ModelAnchor, Reached, Rejected, reach

async with reach(Path("/path/to/archive.sqlite"), ModelAnchor("recording-id", 15)) as result:
    if isinstance(result, Reached):
        print(result.confirmed_operation_cursor, result.semantic_digest)
    elif isinstance(result, Rejected):
        print(result.reason, result.sequence, result.detail)
```

Replay rebuilds the default Run/Combat flow from recorded inputs and checks dependency order, without contacting a model, game, or material source. Automatic entry guidance is off, and explicit document reads use recorded materials. `Reached` holds the original `RunSession` coroutine paused inside the target model call while the context is open; leaving the context cancels it. It consumes no reply and restores no game. Missing facts and mismatches come back as typed `Rejected` results.
