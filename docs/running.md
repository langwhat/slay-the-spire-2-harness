# Running

Model and mode settings are in [configuration.md](configuration.md).

## Ordinary startup

Start the Mod-enabled game, pick a character, then run:

```console
uv run sts2 start
```

The backend reads its configuration once. If the game is not ready yet, it waits without calling the model. When it connects to a run it sets Pause and waits for you to press the in-game Start control. It never resumes a previous running state on its own.

Start and Pause control whether the model plays; they do not pause the game scene itself. Pausing keeps the current session and notes. When a run ends, the backend stays up and waits for the next run you start. Each new run gets fresh notes, contexts, recovery state, and its own archive directory.

Options:

- `--mod-url URL` sets the Mod address (default `http://127.0.0.1:17877`).
- `--output DIRECTORY` sets where runs are written (default `runs`).

The output directory holds:

- `status.json`, with the control endpoint, lifecycle stage, identities, archive path, and any in-flight operation or model request. Its `control_intent` field shows the Start/Pause intent separately from work in flight.
- a shared, size-capped diagnostics log;
- one `archive.sqlite` per run.

Ctrl-C or SIGTERM stops after the current decision, its action batch, settlement, and feedback finish, however long that takes. The backend closes its own resources and never starts or stops your game.

## Live user input

With `STS2_USER_INPUT=true`, `start` and `headless` accept live viewer messages on `127.0.0.1:17879` (change it with `--input-port PORT`). This also adds a viewer rule to the Run, Deck, and Combat system prompts. It needs builtin driving and is off by default; while off, the prompts are unchanged.

Send messages with:

```http
POST /inputs
{ "inputs": [{ "id": "...", "text": "..." }] }
```

Messages go into an in-memory queue, and ids already seen are ignored. Right before each new model request that accepts input, the current role's context receives as many whole `[viewer] text` lines as fit in 50,000 characters. Whitespace within each message is collapsed to single spaces. The limit counts the prefixes and the line breaks between messages. When the next message does not fit, it and all later messages wait in order for the next eligible request. `POST /inputs` returns HTTP 400 and queues none of the submission if any formatted line exceeds the limit. HTTP retries reuse the same request, and a battle's closing review takes no input.

`GET /inputs/deliveries?after=N` returns `{ "instance", "last", "deliveries": [{ "sequence", "id" }] }`. A delivery means the message left the queue for a context; it does not mean the model request succeeded. `instance` changes when the backend restarts, so a client can tell which messages to resend. Runs that received viewer input cannot be replayed offline without that input.

## External gameplay tools

To drive the game with your own agent, start the tool service. It needs no model configuration:

```console
uv run sts2 start --set STS2_DRIVER=external
```

It listens on `127.0.0.1:17878` (change it with `--tool-port PORT`), and `--mod-url` still sets the Mod address. Like ordinary startup, it sets Pause when the game connects, so press Start before sending actions. Each run gets fresh notes, and the service stays up between runs. It runs no built-in model, role history, or automatic progression, and writes diagnostics but no model Journal.

- `GET /tools` returns `{ "roles": { "run": [...], "combat": [...] } }`, with each tool's name, description, and input schema.
- `POST /tools/{name}` takes the bare JSON arguments. It returns the final outcome, the complete current state, the relevant object and description directories, and query data where applicable. If no trustworthy observation is available, `state` is `null`.

Rules:

- **One call at a time.** A concurrent POST, including a read, gets `busy` with `state: null`. `GET /tools` always works.
- **Pause.** While paused, reads work, but actions and notes writes are rejected. A batch that was already accepted finishes its valid steps.
- **Lost connections.** A dropped connection does not cancel an accepted call. Reconnect, inspect once the service is no longer busy, and decide again; do not resend the lost call.
- **New run or route.** The first write after a new run or a changed decision route is rejected with fresh state, so the agent sees the change before acting.
- **Notes.** Each tool keeps its role's permissions. Notes writes are allowed even while Combat owns the current decision.

The [play-sts2](../skills/play-sts2/SKILL.md) skill explains the tools to an outside agent, with separate Run and Combat references. Make that directory available to your agent's skill loader. The backend only serves tools; it does not install or run an agent.

## Developer headless runs

Headless runs are for development and evaluation. Each one owns its own game process, profile, port, and archive, and several can run side by side. Headless runs are macOS only.

You need:

- the production Mod installed in the game bundle;
- a fully unlocked veteran template with one recorded run, every boss marked as seen, and no run history. Generate it for the same game assembly with [mod/tools/NativeTemplate](../mod/tools/NativeTemplate/README.md).

```console
uv run sts2 headless --game /absolute/Game.app/Contents/MacOS/Game \
  --template /absolute/template.manifest.json --character SILENT --ascension 1 --seed EXAMPLE
```

The game executable must be inside its installed macOS app bundle. The resource pack, release metadata, game assembly, and `mods/sts2agent` are read from there, and the template's game assembly hash must match. Each run copies the template's three files into a fresh writable profile. It never reads your own saves or reuses an earlier run's progress. Template content is stored once under `runs/templates/<manifest-digest>`.

### Directories and ports

By default the launcher creates `runs/headless-*` and picks a free port. Use `--instance NEW_DIRECTORY`, `--output DIRECTORY`, and `--port PORT` to choose them yourself; an existing directory or an occupied explicit port is an error.

Startup is complete when all of these hold:

- the launched game is listening on the port;
- it uses an isolated user-data path;
- the native start succeeded;
- the requested character is shown in a stable paused state.

If an automatically chosen port is taken, the launcher tries up to three times, each with a fresh profile. Each attempt has 90 seconds to start. Gameplay itself has no deadline.

### Control and status

After startup, the launcher presses Start once and plays one run. If the run is paused later, resume it by POSTing `{"mode":"running"}` to the `control_endpoint` listed in the instance's `status.json`. Failed runs are not restarted automatically.

`status.json` also lists the actual directories, the game's PID and port, and safe model settings. `--set` overrides work the same way as for ordinary startup. External driving is not available in headless mode. The game process gets an isolated environment without model credentials.

### What gets recorded

The archive manifest records:

- the actual seed, template digest, and game version;
- the requested character and ascension;
- the native start receipt and first control reply;
- settings and installation hashes.

The paused observation before Start is kept separately from the first gameplay observation. No save checkpoints are collected.

### Stopping

The launcher exits when the run ends. Ctrl-C, SIGTERM, or an unexpected game exit stops at the next unit boundary; an unexpected exit is reported as a failure. Cleanup terminates only the game it launched and removes only that game's user-data alias. Logs, the archive, and native autosaves stay in the instance directory, and the shared installation and template are untouched.

## Troubleshooting

- **Mod fails to load or build.** Confirm Steam is switched to the `public-beta` branch and the installed game version is **v0.114.0** (`Properties -> Game Versions & Betas`). Verify both `sts2agent.dll` and `mod_manifest.json` sit inside `<SlayTheSpire2>/mods/sts2agent/` (see [mod/README.md](../mod/README.md) for default Windows, macOS, and Linux paths).
- **Backend cannot reach `http://127.0.0.1:17877` while the game is running.** First check the game log for `[sts2agent] Listening on http://127.0.0.1:17877/`. If you use a system HTTP/SOCKS proxy (Clash, Clash Verge, Surge, etc.), make sure `127.0.0.1` and `localhost` are in your proxy bypass list (`NO_PROXY=127.0.0.1,localhost`), otherwise local Mod traffic may be hijacked by the proxy.
- **Port `17877` is already in use.** Set `STS2AGENT_PORT=<port>` in the game's environment and pass `--mod-url http://127.0.0.1:<port>` to `uv run sts2 start`.
- **`check-config` rejects `.env` with `Unknown setting name`.** Any `LLM_*` or `STS2_*` key in `.env` must be a supported variable from [configuration.md](configuration.md); remove typos or retired keys. Also remember `LLM_AUTH` defaults to `api-key` for `messages` and `bearer` for `responses` / `chat-completions`.
- **`external` driver fails with `Minimal mode requires builtin driving.`** Make sure `STS2_MINIMAL=true` is not explicitly set in `.env` or the shell environment, and do not pass `--minimal` alongside `STS2_DRIVER=external`.
