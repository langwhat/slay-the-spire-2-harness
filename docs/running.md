# Running

Model and mode settings are described in [configuration](configuration.md).

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

## Live user input

`STS2_USER_INPUT=true` lets `start` and `headless` accept live viewer input on
`127.0.0.1:17879` and adds the viewer rule to the Run, Deck, and Combat system prompts; `--input-port PORT`
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
native template generated with [mod/tools/NativeTemplate](mod/tools/NativeTemplate/README.md). This command owns one game
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
