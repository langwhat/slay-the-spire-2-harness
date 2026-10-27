# STS2 Harness

The no-save/load gameplay core provides a programmatic single-run gameplay loop through
`RunSession` and `DefaultFlow`, with Responses and Chat Completions model clients and
recorded game IO.
The CLI can attach a resident backend to your running Mod-enabled game, validate
configuration offline, expose external gameplay tools, and inspect archives. Software replay
is not implemented yet.

Install Python 3.12+ and [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync
cp .env.example .env
# Edit .env once with your model and credentials.
uv run sts2 check-config
uv run sts2 check-config --set STS2_DRIVER=external
./scripts/check
```

Configuration is read once into an immutable snapshot shared by Run and Combat. Precedence
is `--set NAME=VALUE`, process environment, local `.env`, then code defaults.
`--env-file PATH` selects another defaults file; a missing file means no local defaults.
Overrides never modify the environment or defaults file. Dotenv interpolation is disabled.
Keep `.env` private; `.env.example` contains placeholders only. Unknown `LLM_`/`STS2_`
names in the local file fail validation; unrelated process environment keys are ignored.

| Variable | Default / meaning |
| --- | --- |
| `STS2_DRIVER` | `builtin`; `external` needs no model configuration |
| `LLM_PROTOCOL` | `responses`; also `chat-completions`, `messages` |
| `LLM_MODEL` | Required for builtin; configure once locally |
| `LLM_BASE_URL` | `https://api.openai.com/v1`; Messages uses `https://api.anthropic.com` |
| `LLM_AUTH` | `bearer` (`api-key` for Messages); also `none`, `api-key` (named header), `basic` |
| `LLM_API_KEY` | Bearer token or named-header credential |
| `LLM_AUTH_HEADER` | `x-api-key` for `api-key` |
| `LLM_USERNAME`, `LLM_PASSWORD` | Basic authentication credentials |
| `LLM_HEADERS` | Custom headers, one `Name: value` per line; last case-insensitive duplicate wins |
| `LLM_REASONING_EFFORT`, `LLM_SERVICE_TIER` | Optional provider-specific settings |
| `LLM_REQUEST_TIMEOUT_S` | `3600`, positive finite per-attempt timeout |
| `LLM_EXTRA_RETRIES` | `25`, nonnegative additional attempts |

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
Tests use temporary defaults, fake Mod/provider HTTP, and finite local loopback servers;
no game or real provider runs.

The Python `sts2_harness.llm.client.Llm` client accepts a `ModelConfiguration` snapshot
and implements `complete(input, tools)` as an async context manager. It currently supports
Responses and Chat Completions. Callers own the immutable `ModelInput` message tuple;
the client retains
no conversation. Tool definitions use concrete Pydantic argument models. Provider
schemas use `strict=False` to preserve optional-field semantics; tools remain responsible
for strict local argument decoding.

A completed `ModelReply` exposes read-only text/tool-call views, native continuation,
usage, and separate requested/response model metadata. `ResponsesReply.to_json()` and
`from_json()` preserve the complete ordered native output, including encrypted reasoning
and assistant phases in one immutable encoded snapshot. Decoded SDK items are independent
copies. `ChatCompletionsReply` retains a complete native completion and continues with its
native assistant message, including opaque continuation fields. Histories from another
protocol are rejected; protocol switching does not migrate existing context. Append the
returned assistant message and associated tool-result
messages to the caller's input; native output is sent once, without `previous_response_id`.
The adapter does not execute tools or return intermediate streamed argument fragments.

Responses requires `response.completed`; Chat Completions requires a complete `stop` or
`tool_calls` choice after consuming the stream, including any final usage chunk.
Premature clean EOF,
connection failures, timeouts, rate limits, and overload are retried. Each attempt gets its
own configured timeout covering request start through final decoding. SDK retries are
disabled; the default 25 additional attempts wait 2, 4, 8, 16, then 32 seconds, totaling
702 seconds of backoff. Explicit output truncation, authentication, permission, quota,
model/configuration errors, and malformed terminal responses raise a typed
`ModelUnavailable` without automatic regeneration. Cancellation propagates to the caller.

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
Diagnostics default to the archive directory, with `diagnostics.jsonl`, two rotated
backups of at most 10 MiB each, and a `status.json` updated at operation transitions.
The Journal is never rotated with diagnostics. Keep archives outside the repository.

Inspect recorded facts without startup configuration or network access:

```sh
uv run sts2 journal --archive /path/to/journal.sqlite
uv run sts2 journal --archive /path/to/journal.sqlite --kind settle --around 12 --window 2
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
order; `--kind model` and `--kind material` filter dependencies. `Journal.operations()`
selects game/control operations, `requests()` selects model anchors, and `materials()`
selects material reads. `request(sequence)` expands one anchor. Expanded CLI views include
historical inputs, replies, attempts, and observations; selective exports include the
manifest and gaps. Use `--around` with the timeline to inspect neighboring actions,
receipts, queries, and material reads. Sequence and confirmed game cursor express ordering;
they do not imply that an adjacent action was caused by a particular model request.

The current archive schema is version 5 and records each native reply's protocol explicitly.
Reopening provides offline inspection only. Schema versions are validated without archive
migration. Read older archives with the revision that recorded them. Software replay,
native restoration, and evaluation remain unsupported;
the manifest does not claim exact replay.

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

## Single-run sessions

`sts2_harness.session.RunSession` assembles `ModClient`, `Llm`, `Manifest`, and an archive
path into one run. The manifest supplies the bound game/run identities and the unique
initial notes value; `initial_notes` must be explicit, including `""` for known empty notes.
Pass resources that the session may own and close:

```python
from sts2_harness.session import RunSession

async with RunSession(client, manifest, archive_path, model=model) as session:
    await session.start()
    ended = await session.run()
```

The session owns and releases the supplied client and optional model, creates and closes
its Journal, and shares one Notes owner and recovery budget. `DefaultFlow` waits for Start,
schedules Run and Combat, completes pending battle handoffs before creating the next
Combat, rebuilds Run context at act boundaries, and returns on termination or run change.
`start()` records a real Pause and waits for explicit Start before `run()` enters gameplay.
A supplied process Diagnostics instance is borrowed and remains open across run sessions.
It does not launch a game or select a character. Run chooses automatic progression or
strategy within the same execution path; shop entry actions remain separate units and
purchases and departure remain strategy decisions. Validation uses fake HTTP and local
archives; this does not establish complete real-game acceptance.

## Run decisions and strategy notes

`RunAgent` uses the shared `Agent` loop and `Execution.run_unit` for explicitly supplied
`ActTask` and `HandoffTask` values. Its fixed tools are `run_act`, `run_inspect`,
`run_knowledge`, `run_skill`, and `run_notes`. Construct one `Notes(initial_text)` per run
and pass it to the Run `ToolCatalog`; the agent reads the same owner. Notes are authoritative
in memory. Reads return the full text; replacement permits clearing; an edit requires one
exact unique match, including overlapping occurrences, and permits deletion. Failed edits
leave notes unchanged, while successful writes return only `{"updated":true}`.

`await role.enter_act(ActStart(observation))` fully loads guidance and packaged instructions
before installing a fresh Context and delivery ledger. A required-material failure requests
Pause through `DecisionInterrupted`; explicit retry preserves the same notes owner. Finish
pending action feedback before entering an act. The first actual act or handoff task in the
new Context carries current notes, including an empty string; construction alone does not
consume them. Session orchestration owns act scheduling and recording stream assembly.
