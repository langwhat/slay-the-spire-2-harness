# STS2 Harness

The no-save/load gameplay core provides a programmatic single-run gameplay loop through
`RunSession` and `DefaultFlow`, with Responses and Chat Completions model clients and
recorded game IO.
The CLI can attach a resident backend to your running Mod-enabled game, validate
configuration offline, expose external gameplay tools, launch isolated developer headless
runs on macOS, inspect archives, and replay built-in software history offline.

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync
cp .env.example .env
# Edit .env once with your model and credentials.
uv run sts2 check-config
uv run sts2 check-config --set STS2_DRIVER=external
./scripts/check
```

The `input_characters_v1` estimator runs locally without tokenizer packages, downloaded
tables, provider counting calls, or historical-usage calibration. It counts Unicode
characters in decoded string values plus a canonical JSON structural skeleton whose
string values are empty. Keys, non-string scalar values, and structure count once.
The estimate is `ceil(characters / 3) + 3 * items`, where items are top-level entries
in the protocol's input-bearing arrays and tool list: Responses `input`/`tools`, Chat
Completions `messages`/`tools`, and Messages `system`/`messages`/`tools`.
Opaque continuation strings count at ordinary character length. HTTP escaping and
transport-only settings do not inflate the estimate. Full canonical request digests
and byte counts cover the unchanged transport body. The algorithm identity is recorded
in the Journal alongside the capacity policy and each attempt's estimate and provider usage.
These are approximate counts, not provider counts or a guaranteed upper bound, especially
for different languages, content distributions, or providers. Use `uv run --offline`
when uv itself must not access the network; dependencies must already be cached.

Configuration is read once into an immutable snapshot shared by Run and Combat. Precedence
is `--set NAME=VALUE`, process environment, local `.env`, then code defaults.
`--env-file PATH` selects another defaults file; a missing file means no local defaults.
Overrides never modify the environment or defaults file. Dotenv interpolation is disabled.
Keep `.env` private; `.env.example` contains placeholders only. Unknown `LLM_`/`STS2_`
names in the local file fail validation; unrelated process environment keys are ignored.

| Variable | Default / meaning |
| --- | --- |
| `STS2_DRIVER` | `builtin`; `external` needs no model configuration |
| `STS2_USER_INPUT` | `false`; `true` enables [live user input](#live-user-input) |
| `LLM_PROTOCOL` | `responses`; also `chat-completions`, `messages` |
| `LLM_MODEL` | Required for builtin; configure once locally |
| `LLM_BASE_URL` | `https://api.openai.com/v1`; Messages uses `https://api.anthropic.com` |
| `LLM_AUTH` | `bearer` (`api-key` for Messages); also `none`, `api-key` (named header), `basic` |
| `LLM_API_KEY` | Bearer token or named-header credential |
| `LLM_AUTH_HEADER` | `x-api-key` for `api-key` |
| `LLM_USERNAME`, `LLM_PASSWORD` | Basic authentication credentials |
| `LLM_HEADERS` | Custom headers, one `Name: value` per line; last case-insensitive duplicate wins |
| `LLM_REASONING_EFFORT`, `LLM_SERVICE_TIER` | Optional provider-specific settings |
| `LLM_REASONING_EFFORT_RUN`, `LLM_REASONING_EFFORT_DECK`, `LLM_REASONING_EFFORT_COMBAT` | Optional per-purpose effort; empty inherits `LLM_REASONING_EFFORT` |
| `LLM_REQUEST_TIMEOUT_S` | `3600`, positive finite per-attempt timeout |
| `LLM_EXTRA_RETRIES` | `25`, nonnegative additional attempts |
| `LLM_CONTEXT_WINDOW` | `300000`, positive input accounting window |
| `LLM_CAPACITY_VERIFIED_ENDPOINT` | Empty by default; an exact match with `LLM_BASE_URL` enables the verified Responses HTTP 400 `context_too_large` rejection signal |

Set `LLM_CAPACITY_VERIFIED_ENDPOINT` only after verifying that exact endpoint's structured
Responses rejection. Changing `LLM_BASE_URL` disables the signal until the selector matches
again. Other protocols, HTTP statuses, and streamed failures do not establish this rejection.
Recordings retain the verified category, protocol, and signal; they do not store the endpoint URL.

Base URLs must omit userinfo, query strings, and fragments. Endpoint path prefixes are
retained when the selected client appends `/responses` or `/chat/completions`.
Authentication applies SDK
defaults, configured authentication, then case-insensitive custom headers. SDK environment
credentials and custom headers do not supply project authentication.
`none` discards configured credentials and must not inherit SDK environment credentials.
External mode fixes the driver and skips unused model configuration validation. Custom
headers use LF-separated lines and ASCII field values with optional tabs; other control
characters are rejected. Bearer and named-header credentials must fit these HTTP header
constraints without leading/trailing whitespace. Basic passwords may contain Unicode.

The check emits only bounded configuration metadata. Free-form model IDs, endpoint URLs,
headers, effort, tier, and credentials are hidden, including on validation failures.
Success establishes offline validity only; provider-specific model/effort/tier support and
actual authentication require real endpoint checks.

Run Ruff, strict Pyright, and pytest with `./scripts/check` (requires `uv` on PATH).
Tests use temporary defaults, fake Mod/provider HTTP, finite local loopback servers, and
owned child processes; no real game or model provider is required.

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

## Ordinary gameplay startup

Start your Mod-enabled game and select a character, then run:

```console
uv run sts2 start
```

The backend reads the configured model defaults once. If the game is not ready, it waits
without calling a model. On connection to a run it sets Pause, then waits for the in-game
Start control; it does not inherit a previous running state. Start/Pause controls AI
admission, not the game's scene pause. Manual Pause retains the current session and notes.
After a terminal result the backend stays resident, waiting for a new user-started run.
Every new run has fresh notes, contexts, recovery state, and an archive directory.

Add `--minimal` to `start` or `headless` for minimal gameplay, or set
`STS2_MINIMAL=true` in `.env` or the process environment. As with other settings, `--set`
overrides the process environment, which overrides `.env`; explicit `--minimal` takes
precedence over all three. Use `--set STS2_MINIMAL=false` to disable an environment default.
`check-config` reports the resolved mode. Both modes share gameplay and state-reading rules.
Ordinary mode adds deck construction and battle review instructions, notes, and query tools.
Minimal gameplay exposes only actions and route planning, skips model battle reviews and
standalone report-processing calls, and supplies no notes or candidate event links. Automatic
knowledge injection, measured deck statistics, and automatic progression remain available.
Minimal gameplay targets lower total usage cost; fewer input tokens alone do not establish
savings when cached input is discounted. The archive records the mode so offline replay
uses the same behavior. Ordinary gameplay is the default.

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

The default Mod URL is `http://127.0.0.1:17877`. Use `--mod-url URL` and `--output DIRECTORY`
to override the connection and output directory; the default output directory is `runs`.
`status.json` publishes the control endpoint, current lifecycle, identities, archive path,
and in-flight operation or model request. `control_intent` describes the native Start/Pause
intent separately from in-flight work. Diagnostics share a bounded log in the output
directory, while each run keeps its own `archive.sqlite`.

Ctrl-C or SIGTERM requests shutdown after the admitted decision, its action batch,
settlement, and feedback finish. Waiting has no fixed deadline. The backend closes its own
resources and never launches or terminates your game. Model protocols not yet implemented
by the model adapter fail explicitly before connection.

## Live user input

`STS2_USER_INPUT=true` lets `start` and `headless` accept live viewer input on
`127.0.0.1:17879` and adds the viewer rule to the Run and Combat system prompts; `--input-port PORT`
selects another port. It requires builtin driving and is off by default, which leaves the prompts
unchanged.

`POST /inputs` with `{ "inputs": [{ "id": "...", "text": "..." }] }` appends to a memory-only FIFO,
ignoring ids it has already seen. Input enters the current role's Context as `[viewer] text` lines
immediately before a new logical model request; HTTP retries reuse that frozen request, and a
battle's closing review does not take input. One delivery keeps its first 50,000 characters and
drops the rest.

`GET /inputs/deliveries?after=N` returns `{ "instance", "last", "deliveries": [{ "sequence", "id" }]
}`. A delivery means the input left the queue for a Context, not that all its text fit or that a
model request succeeded. `instance`
changes when the backend restarts, so clients can resubmit undelivered input. Runs that received
input cannot be replayed offline without it, and recordings made with the setting on carry the
viewer rule.

## External gameplay tools

Start the local tool backend without model configuration:

```console
uv run sts2 start --set STS2_DRIVER=external
```

It listens only on `127.0.0.1:17878`; `--tool-port PORT` selects another tool port.
`--mod-url` still selects the Mod connection, defaulting to port 17877. The resident
backend establishes Pause when the game connects; use the in-game Start control before
writes. It creates fresh notes and execution ownership for each run and stays resident
after a terminal result. It runs no built-in model, role history, or automatic progression.
External runs publish process diagnostics but do not create built-in model Journals.

`GET /tools` returns `{ "roles": { "run": [...], "combat": [...] } }` with each tool's
name, description, and self-contained input schema. `POST /tools/{name}` accepts its
bare JSON argument object and returns the final semantic outcome, complete current state,
and full relevant directories, plus query data when applicable. Technical routes and
observation stamps are internal. A missing trustworthy observation is `state: null`.

Send one call at a time. Concurrent POSTs, including reads, receive `busy` with null state;
static discovery remains available. While paused, reads work and new actions or notes
writes are rejected. An accepted action batch finishes its valid steps even if Pause is
pressed during it. A lost connection does not cancel an accepted call. Reconnect and
inspect once no longer busy, then make a new decision instead of replaying the lost call.
A first write or changed decision route is rejected with fresh state before a new write
can be accepted. Tool names retain their role permissions; notes writes can also occur
while the current game decision belongs to Combat.

The external agent entry point is [play-sts2](skills/play-sts2/SKILL.md), with separate
Run and Combat references. Make that skill directory available to the external agent's
skill loader. The backend serves tools directly and does not install or run an agent.

## Isolated developer headless runs

Install the production Mod once and supply an already verified, fully unlocked clean
native template generated with its NativeTemplate tools. This command owns one game
process and one run; it never attaches another running game or changes the installation:

```console
uv run sts2 headless --game /absolute/Game.app/Contents/MacOS/Game \
  --template /absolute/template.manifest.json --character SILENT --ascension 1 --seed EXAMPLE
```

The game executable must be inside its installed macOS application bundle. The resource
pack, release metadata, architecture-specific game assembly, and `mods/sts2agent` are
read from that installation. Other platforms are currently rejected. The template's
game assembly hash must match. Each run copies the three read-only native template files
into a fresh writable profile; it never reads ordinary player saves or reuses a previous
run's progress. Template content is retained once under `runs/templates/<manifest-digest>`.

By default the launcher atomically creates `runs/headless-*` and chooses a candidate port.
Use `--instance NEW_DIRECTORY`, `--output DIRECTORY`, and `--port PORT` for explicit paths
and port. Existing instance directories and occupied explicit ports fail. A candidate
port is not a reservation: readiness requires the owned child to be its actual listener,
a verified isolated native user-data path, a successful native start receipt, and stable
paused public state for the requested character. Automatic port collisions have at most
three launch attempts, each with a fresh profile. Startup has a 90-second budget per
attempt; gameplay, programmatic Pause, and complete admitted units have no such deadline.

After verified startup, the host sends Start once and enters one `RunSession` directly.
Later programmatic Pause requires an explicit Start through the `control_endpoint` in
that instance's `status.json` (POST `{"mode":"running"}`); there is no automatic failure
restart. The status also publishes actual directories, the owned game PID and port, and
safe effective model configuration. Per-instance `--set` overrides use the same immutable
configuration loading as ordinary startup; external headless driving is not implemented.
The game child receives an isolated environment without model credentials.

The manifest records the actual canonical seed, template digest, release version,
requested character/ascension, verified native start receipt and first control reply,
settings and installation hashes. Requested difficulty is verified by the native start
path, not independently reported by the public state. Bootstrap evidence preserves the
paused initial observation separately from the later first Journal observation after
Start. No checkpoint sequence or save restoration is collected or promised.

Terminal completion exits the host. Ctrl-C or SIGTERM requests stopping at the existing
complete-unit boundary. An unexpected child exit also requests that safe boundary and
reports failure. Cleanup waits for and terminates only the owned child, removing only its
owned user-data alias; logs, the archive, and native autosaves remain in the instance
directory. Shared installation files and the source template remain intact.

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

## Run decisions and strategy notes

`RunAgent` uses the shared `Agent` loop and `Execution.run_unit` for explicitly supplied
`ActTask` and `HandoffTask` values. Its tools are `run_act`, `run_deck_choice`, `run_plan`,
`run_next`, `run_inspect`, `run_knowledge`, `run_skill`, and `run_notes`. Construct one
`Notes(NotesState(...))` per run and pass it to the Run `ToolCatalog`; the agent reads the
same owner. An empty initial string creates empty structured notes; nonempty legacy text
is rejected. Structured initial notes contain `plan` (up to 800 characters) and `other`.
`run_notes(mode="write", ...)` replaces only supplied fields, including clearing a field
with an empty string. Combat reads the plan at battle entry. Both modes use the same
`run_deck_choice` schema: a take supplies offer, card, and reason; a skip supplies offer
and reason. Shop card purchases use the same tool; `run_act` cannot bypass it. Minimal
mode has no notes tools or injected notes. The Mod distinguishes rewards that open a selector from immediate
card grants; declined immediate grants stay unclaimed until the reward screen is left.

State delivery replaces complete named sections and sends stable-ID definitions once per
context. Run deck rows aggregate identical native instances with their actual card text.
`deck_stats` describes the current deck using native base draw and energy; its baseline
cycle, hand-cost, and unplayed-card estimates exclude conditional combat and relic effects.
Reward takes and skips are counted per act; shop purchases do not change those counters.
`use.observed_plays` describes the last five sampled battle-history prefixes;
they omit unmeasured draw, damage, block, and energy statistics. Optional private candidate
links describe potential event connections whose conditions still require the native rules.

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
and zero for `CHARACTER.IRONCLAD`, `CHARACTER.SILENT`, `CHARACTER.REGENT`, and
`CHARACTER.NECROBINDER`. The source file is unchanged, and the derived field and basis
appear in the trial and report. Other characters with a missing value remain unsupported.

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
      "candidate_links": "snapshots/q1506-links.json",
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
Roles are `run`, `deck`, and `combat`. Omit `documents` or `candidate_links` only when
the case did not enable that source. A catalog file uses the `FrozenCatalog` schema:
`catalog` contains the native catalog metadata, `definitions` contains native lookup
entries, and `complete` asserts the definition set covers the entire native catalog.
Only a verified complete snapshot can answer arbitrary searches. Partial snapshots can
answer lookups for included IDs, but missing IDs and searches are unknown. A verified
complete snapshot reports missing lookup IDs as absent from the native catalog. Document
snapshots use `FrozenDocuments`: declare all covered subjects (or `complete_subjects`)
and freeze the documents, requested resources, and role skill lists. Candidate-link
snapshots use `FrozenLinks`, with the exact candidate/owned inputs and result for each
covered assembly. Inspect queries are answered only by matching native reads in the case.
The snapshot schemas are in `sts2_harness_eval.frozen_catalog` and `frozen_documents`.
Use `FrozenDocuments.snapshot_json()` to save document snapshots: it preserves material
IDs, content hashes, and bodies rather than the model-visible text-only serialization.

By default, each configuration snapshots the packaged prompts before the run. To compare
another version, set `prompt_directory` to a directory containing the corresponding
named Markdown files. The version label and the digest of the actual prompt set are both
recorded. The case, catalog, document, and link files are read into memory before calls;
their hashes are recorded per trial. Prepare full snapshots from trustworthy sources:
an exact-query cache alone is not a complete catalog or document snapshot.

Before launch, check active batches, system memory pressure, representative worker
memory, and headroom for the OS and interactive applications. Choose `--concurrency`
from the available capacity; record scheduling changes separately. Configure the existing
builtin model and run:

```sh
uv run python -m sts2_harness_eval.stable_suite \
  --plan /private/evaluation/plan.json --output /private/evaluation/result \
  --env-file .env --concurrency 4
```

The output directory must be new. This entry point currently accepts the Responses
protocol only and rejects other protocols before any request; the game's other protocols
are unaffected. No transport retries or replacement trials are made. Before sending,
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
