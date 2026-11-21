# Isolated Native Template and Standard Starts

These developer-only tools create a minimal, fully unlocked native template profile for isolated headless runs. They do not participate in gameplay, expose HTTP endpoints, or read an ordinary player profile. Keep generated profiles, manifests, and observations outside Git.

## Generate Once

1. Build `mod/tools/NativeTemplate/Settings/Settings.csproj` and `mod/tools/NativeTemplate/Mod/NativeTemplate.csproj` in `Release` with .NET 9. `mod/Directory.Build.props` resolves `$(GameDir)` automatically from `STS2_GAME_DIR` or standard Steam installation paths, or pass `-p:GameDir=<installed managed assembly directory>`:
   ```sh
   dotnet build mod/tools/NativeTemplate/Settings/Settings.csproj -c Release
   dotnet build mod/tools/NativeTemplate/Mod/NativeTemplate.csproj -c Release
   ```
2. Create an isolated user-data directory containing only native `SettingsSave` with Mod loading enabled:
   ```sh
   dotnet mod/tools/NativeTemplate/Settings/bin/Release/net9.0/Settings.dll \
     "<game-assembly-directory>" "<new-user-data-directory>"
   ```
   Both paths must be absolute, and `<new-user-data-directory>` must not exist. This constructs `SettingsSave` and `ModSettings` directly through the native serializer without copying player files.
3. Temporarily install `sts2agent_template.dll` and its `mod_manifest.json` in a separate `mods/sts2agent_template` directory inside the game installation. Own and restore any installation changes. Launch with fresh isolated `HOME`/platform user-data paths and verify the resolved Godot `user://` directory independently. Never launch against ordinary player saves.
4. In only that headless child's environment, set `STS2AGENT_TEMPLATE_USER_DIR` to the exact absolute `OS.GetUserDataDir()` path and `STS2AGENT_TEMPLATE_OUTPUT` to a new absolute template directory outside the user-data directory. Do not set `STS2AGENT_START_RUN_FILE`. Require process exit code `0` and `<template-directory>.manifest.json`.
5. Remove the temporary `mods/sts2agent_template` directory. Verify a standard headless start against a fresh writable copy of the template before admitting it for runs.

`TemplateGenerator` requires a clean profile, executes native `unlock all`, disables FTUEs, marks the tutorial and singleplayer ascension FTUEs complete, and saves through `SaveManager`. Its manifest records unlock counts, character IDs, aggregate ascension progress, zero run/history checks, and SHA-256 digests of `sts2.dll`, `sts2agent_template.dll`, and the three read-only native files (`default/1/settings.save`, `default/1/modded/profile1/saves/progress.save`, and `default/1/modded/profile1/saves/prefs.save`).

## Start Each Isolated Run

`sts2_harness.native_template.NativeTemplate` verifies the manifest and read-only files, retains an immutable copy by manifest SHA-256, and populates a fresh writable user-data directory per instance.

The production `sts2agent` Mod reads `STS2AGENT_START_RUN_FILE` pointing to a one-shot UTF-8 JSON recipe:

```json
{"character":"SILENT","ascension":0,"seed":"STANDARD26"}
```

Supported characters are `IRONCLAD`, `SILENT`, `REGENT`, `NECROBINDER`, `DEFECT`, and `FOOL`; `ascension` is an integer from `0` through `10`. Unknown or duplicate properties, unsupported characters, and out-of-range ascension values are rejected. Omitting `seed` (or setting `null`) requests a native random seed; fixed recipes supply and verify the canonical seed. `HeadlessRunStarter` drives the native character-select screen, verifies the started run identity, and writes `<start-file>.result.json` with `status="completed"` and `effect_verified=true` while keeping the control boundary paused until the host sends an explicit `running` command.
