# STS2 Harness

A model-driven player for Slay the Spire 2 that plays one run at a time without save/load.
A Python backend drives Run, Deck, and Combat roles through the `sts2agent` game Mod's
public observations and actions, records every game and model exchange, and can replay
recordings offline.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Slay the Spire 2 with the [`sts2agent` Mod](mod/README.md) installed. Building the Mod
  needs the .NET 9 SDK and local game assemblies.
- A model endpoint using the OpenAI Responses, Chat Completions, or Anthropic Messages
  protocol. External driving needs no model.
- Developer headless runs are macOS only.

## Setup

```sh
uv sync
cp .env.example .env
# Edit .env with your model and credentials; keep it private.
uv run sts2 check-config
```

Settings, their precedence, and minimal mode are described in
[docs/configuration.md](docs/configuration.md).

## Play

Start the Mod-enabled game, select a character, then run:

```console
uv run sts2 start
```

The backend connects in Pause; press the in-game Start control to hand the run to the
model. It stays resident and serves each new run you start. To drive the game with your
own agent instead, run `uv run sts2 start --set STS2_DRIVER=external` and use the local
tool API with the [play-sts2](skills/play-sts2/SKILL.md) skill.

Developers can launch isolated game instances, each with its own profile, port, and
archive:

```console
uv run sts2 headless --game /absolute/Game.app/Contents/MacOS/Game \
  --template /absolute/template.manifest.json --character IRONCLAD --ascension 0
```

See [docs/running.md](docs/running.md) for startup, live viewer input, external tools,
and headless details. `uv run sts2 --help` lists every command and option.

## Inspect recordings

Each run writes `archive.sqlite` in its own directory under `runs/`. Inspect it offline:

```sh
uv run sts2 journal --archive runs/<run-directory>/archive.sqlite
```

More filters and views are listed in [docs/library.md](docs/library.md#recordings).

## Development

```sh
./scripts/check      # Ruff, strict Pyright, pytest (no game or model needed)
./scripts/check-mod  # Mod offline tests and Release build
```

## Documentation

- [Configuration](docs/configuration.md): settings, authentication, minimal mode.
- [Running](docs/running.md): ordinary, external, and headless startup.
- [Using the core as a library](docs/library.md): model client, recordings, sessions, roles.
- [Evaluation](docs/evaluation.md): offline replay and frozen decision evaluation.
- [Gameplay retrospectives](docs/gameplay-retrospectives.md): reviewing finished runs.
- [Run-scoped documents](docs/materials.md): supplying extension documents.
- [Mod](mod/README.md): the C# game Mod and native template tools.
