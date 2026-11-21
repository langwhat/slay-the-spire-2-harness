using System.Security.Cryptography;
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.DevConsole;
using MegaCrit.Sts2.Core.DevConsole.ConsoleCommands;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Nodes.Ftue;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Timeline;
using Environment = System.Environment;

namespace Sts2Mod.Tools.NativeTemplate;

/// <summary>
/// Developer-only Mod entry point that initializes an unlocked veteran profile in an isolated headless user directory
/// and exports the immutable three-file native template plus its verification manifest.
/// </summary>
[ModInitializer(nameof(Initialize))]
public static class TemplateGenerator
{
    private static readonly string[] TemplateFiles =
    [
        "default/1/settings.save",
        "default/1/modded/profile1/saves/progress.save",
        "default/1/modded/profile1/saves/prefs.save",
    ];

    private static readonly JsonSerializerOptions ManifestJsonOptions = new()
    {
        WriteIndented = true,
    };

    private static string? _output;
    private static bool _started;

    public static void Initialize()
    {
        _output = Environment.GetEnvironmentVariable("STS2AGENT_TEMPLATE_OUTPUT");
        if (string.IsNullOrWhiteSpace(_output))
            return;

        if (Engine.GetMainLoop() is SceneTree tree)
            tree.ProcessFrame += Generate;
    }

    private static async void Generate()
    {
        if (_started || !SaveManager.Instance.IsProfileInitialized)
            return;

        _started = true;
        if (Engine.GetMainLoop() is not SceneTree tree)
            return;
        tree.ProcessFrame -= Generate;

        try
        {
            var expectedUserDirectory = Environment.GetEnvironmentVariable("STS2AGENT_TEMPLATE_USER_DIR");
            var userDirectory = OS.GetUserDataDir();
            if (DisplayServer.GetName() != "headless"
                || string.IsNullOrWhiteSpace(expectedUserDirectory)
                || !Path.IsPathFullyQualified(expectedUserDirectory)
                || Path.GetFullPath(userDirectory) != Path.GetFullPath(expectedUserDirectory)
                || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("STS2AGENT_START_RUN_FILE")))
            {
                throw new InvalidOperationException(
                    "Template generation requires an explicitly isolated headless user-data directory and no run-start file.");
            }

            if (string.IsNullOrWhiteSpace(_output) || !Path.IsPathFullyQualified(_output))
                throw new InvalidOperationException("Template output must be an absolute path.");

            var output = Path.GetFullPath(_output);
            var fullUserDirectory = Path.GetFullPath(userDirectory);
            if (output == fullUserDirectory
                || output.StartsWith(fullUserDirectory + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("Template output must be outside the user-data directory.");
            }

            if (Directory.Exists(output) || File.Exists(output))
                throw new InvalidOperationException("Template output must not exist.");

            var saves = SaveManager.Instance;
            RequireClean(saves);

            var command = new UnlockConsoleCmd();
            var unlock = new DevConsole(shouldAllowDebugCommands: true).ProcessCommand(command.CmdName + " all");
            if (!unlock.success)
                throw new InvalidOperationException(unlock.msg);
            if (unlock.task is not null)
                await unlock.task;

            var bosses = ModelDb.Acts
                .SelectMany(act => act.AllBossEncounters.Concat(act.BossDiscoveryOrder))
                .DistinctBy(boss => boss.Id)
                .ToArray();
            foreach (var boss in bosses)
                saves.Progress.GetOrCreateEncounterStats(boss.Id);

            // An Ironclad loss increments the profile's run count without granting ascension progress.
            saves.Progress.GetOrCreateCharacterStats(ModelDb.GetId<Ironclad>()).TotalLosses = 1;

            saves.SetFtuesEnabled(false);
            saves.MarkFtueAsComplete(NAcceptTutorialsFtue.id);
            saves.MarkFtueAsComplete(NAscensionSingleplayerFtue.id);
            saves.SavePrefsFile();
            saves.SaveSettings();
            saves.SaveProgressFile();
            saves.SaveProfile();

            var progressRead = saves.InitProgressData();
            if (progressRead.Status != ReadSaveStatus.Success || saves.Progress.NumberOfRuns != 1)
                throw new InvalidOperationException("Saved native progress must load with exactly one recorded run.");

            var unlockState = saves.GenerateUnlockStateFromProgress();
            if (bosses.Length == 0 || bosses.Any(boss => !unlockState.HasSeenEncounter(boss)))
                throw new InvalidOperationException("Saved native progress must have seen every boss encounter.");

            RequireNoRunFiles(saves);

            var epochs = EpochModel.AllEpochIds.ToArray();
            if (saves.GetUnlocksRemaining() != 0
                || epochs.Any(id => !saves.IsEpochRevealed(id))
                || saves.GetTotalUnlockedCards() != SaveManager.GetUnlockableCardCount()
                || saves.GetTotalUnlockedRelics() != SaveManager.GetUnlockableRelicCount()
                || saves.GetTotalUnlockedPotions() != SaveManager.GetUnlockablePotionCount())
            {
                throw new InvalidOperationException("Native unlock validation failed.");
            }

            Directory.CreateDirectory(output);
            var files = TemplateFiles.Select(relative =>
            {
                var destination = Path.Combine(output, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                File.Copy(Path.Combine(userDirectory, relative), destination);
                File.SetAttributes(destination, File.GetAttributes(destination) | FileAttributes.ReadOnly);
                return new { path = relative, sha256 = Digest(destination) };
            }).ToArray();

            var manifest = new
            {
                files,
                game_assembly_sha256 = Digest(typeof(SaveManager).Assembly.Location),
                generator_sha256 = Digest(typeof(TemplateGenerator).Assembly.Location),
                epochs = epochs.Length,
                cards = saves.GetTotalUnlockedCards(),
                relics = saves.GetTotalUnlockedRelics(),
                potions = saves.GetTotalUnlockedPotions(),
                characters = unlockState.Characters
                    .Select(character => character.Id.ToString())
                    .ToArray(),
                ascension_total = saves.GetAggregateAscensionProgress(),
                runs = saves.Progress.NumberOfRuns,
                bosses_seen = bosses.Select(boss => boss.Id.ToString()).Order(StringComparer.Ordinal).ToArray(),
                history = saves.GetRunHistoryCount(),
                has_run = saves.HasRunSave,
                has_multiplayer_run = saves.HasMultiplayerRunSave,
                tutorials_seen = saves.SeenFtue(NAcceptTutorialsFtue.id),
                ascension_ftue_seen = saves.SeenFtue(NAscensionSingleplayerFtue.id),
            };

            File.WriteAllText(output + ".manifest.json", JsonSerializer.Serialize(manifest, ManifestJsonOptions));
            tree.Quit();
        }
        catch (Exception exception)
        {
            GD.PrintErr($"[sts2agent_template] Native template generation failed: {exception}");
            tree.Quit(1);
        }
    }

    private static void RequireClean(SaveManager saves)
    {
        RequireNoRunFiles(saves);
        if (saves.Progress.NumberOfRuns != 0 || saves.Progress.EncounterStats.Count != 0)
            throw new InvalidOperationException("Template generation requires a clean profile without run or encounter stats.");
    }

    private static void RequireNoRunFiles(SaveManager saves)
    {
        if (saves.HasRunSave || saves.HasMultiplayerRunSave || saves.GetRunHistoryCount() != 0)
            throw new InvalidOperationException("The template profile must have no run saves or run history.");
    }

    private static string Digest(string path) =>
        Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(path)));
}
