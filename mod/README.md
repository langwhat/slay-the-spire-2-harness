# STS2 Agent Mod (`sts2agent`)

`mod/` contains the C# Slay the Spire 2 Mod that exposes the local loopback HTTP bridge (`127.0.0.1:17877` by default) and the in-game Start/Pause control for `sts2_harness`.

## Layout

- `mod/src/Sts2Mod.Core/`: Game- and Godot-independent protocol contracts, observation/inspection/catalog readers, single-step admission and settlement gateway, loopback HTTP server, and developer contracts.
- `mod/src/Sts2Mod/`: Godot/Harmony game adapter (`sts2agent.dll` + `mod_manifest.json`) targeting Slay the Spire 2 (`net9.0`).
- `mod/tests/Sts2Mod.Core.Tests/`: Offline xUnit test suite exercising `Sts2Mod.Core` without game or Godot assemblies.
- `mod/tools/NativeTemplate/`: Developer-only isolated profile initializer (`Settings` CLI + `sts2agent_template` Mod) paired with `sts2_harness.native_template`.

## Build and Verify

Run the offline unit tests and, when local game assemblies are available, compile all Release assemblies:

```sh
./scripts/check-mod
```

`mod/Directory.Build.props` resolves `$(GameDir)` from `STS2_GAME_DIR` or standard Steam installation directories on macOS, Linux, and Windows.

## Install for Local Gameplay

Build `mod/src/Sts2Mod/Sts2Mod.csproj` in `Release` and copy the two output artifacts into `<SlayTheSpire2>/mods/sts2agent/`:

- `mod/src/Sts2Mod/bin/Release/net9.0/sts2agent.dll`
- `mod/src/Sts2Mod/bin/Release/net9.0/mod_manifest.json`

By default the Mod listens on `http://127.0.0.1:17877` (override with `STS2AGENT_PORT`). Developer checkpoint and laboratory endpoints are enabled only when running under Godot headless display mode with `STS2_ALLOW_CHECKPOINTS=true`.
