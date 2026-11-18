using System.IO;
using Godot;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Ftue;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.CharacterSelect;
using MegaCrit.Sts2.Core.Nodes.Screens.MainMenu;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Timeline.Epochs;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Observation;

namespace Sts2Mod.Developer;

/// <summary>
/// Drives one-shot headless run startup configured via <c>STS2AGENT_START_RUN_FILE</c>
/// through native menu controls and writes the verified <c>.result.json</c> receipt.
/// </summary>
internal sealed class HeadlessRunStarter
{
    [Flags]
    private enum Stage
    {
        None = 0,
        EarlyAccessDismissed = 1 << 0,
        SingleplayerOpened = 1 << 1,
        StandardOpened = 1 << 2,
        CharacterSelected = 1 << 3,
        AscensionApplied = 1 << 4,
        SeedApplied = 1 << 5,
        TutorialsDismissed = 1 << 6,
        AscensionFtueDismissed = 1 << 7,
        ConfirmReleased = 1 << 8,
    }

    private string? _startFilePath;
    private HeadlessRunConfiguration? _config;
    private bool _loaded;
    private bool _completed;
    private bool _runStartedObserved;
    private DateTimeOffset _deadline;
    private Stage _stages;

    public bool IsActive => _config is not null && !_completed;

    public void OnRunStarted()
    {
        if (IsActive)
            _runStartedObserved = true;
    }

    public void Advance()
    {
        if (_completed)
            return;

        if (!_loaded)
        {
            _loaded = true;
            _startFilePath = System.Environment.GetEnvironmentVariable("STS2AGENT_START_RUN_FILE");
            if (string.IsNullOrWhiteSpace(_startFilePath))
            {
                _completed = true;
                return;
            }

            _config = HeadlessRunConfiguration.Parse(File.ReadAllText(_startFilePath));
            if (RunManager.Instance.IsInProgress)
                throw new InvalidOperationException("Headless automatic run start requires a fresh game instance.");

            _deadline = DateTimeOffset.UtcNow.AddSeconds(45);
        }

        if (_config is not { } config || _startFilePath is null)
            return;

        if (DateTimeOffset.UtcNow >= _deadline)
            throw new TimeoutException("Headless automatic run start timed out before reaching a ready run.");

        if (_stages.HasFlag(Stage.ConfirmReleased))
        {
            DismissVisibleFtues();
            if (!_runStartedObserved || !RunManager.Instance.IsInProgress)
                return;

            var run = RunManager.Instance.DebugOnlyGetState();
            var seed = run?.Rng.StringSeed;
            if (string.IsNullOrWhiteSpace(seed)
                || ObservationHub.Current.Reply is not CompletedStateRead completed
                || completed.Observation.State is not RunStateView)
            {
                return;
            }

            var receiptJson = JsonContract.Serialize(HeadlessStartReceipt.Completed(seed));
            File.WriteAllText(_startFilePath + ".result.json", receiptJson);
            _completed = true;
            GD.Print($"[sts2agent] Headless run started: character={config.Character}, ascension={config.Ascension}, seed={seed}.");
            return;
        }

        var saveManager = SaveManager.Instance;
        if (!saveManager.IsProfileInitialized || Engine.GetMainLoop() is not SceneTree tree)
            return;

        if (saveManager.HasRunSave)
            throw new InvalidOperationException("Headless automatic run start found an unexpected existing run save.");

        ValidateAscensionUnlocked(saveManager, config);

        if (NModalContainer.Instance?.OpenModal is NEarlyAccessDisclaimer disclaimer)
        {
            if (!_stages.HasFlag(Stage.EarlyAccessDismissed))
            {
                _stages |= Stage.EarlyAccessDismissed;
                TaskHelper.RunSafely(disclaimer.CloseScreen());
            }
            return;
        }

        if (DismissVisibleFtues())
            return;

        var screen = FindVisibleNode<NCharacterSelectScreen>(tree.Root);
        if (screen is null)
        {
            var submenu = FindVisibleNode<NSingleplayerSubmenu>(tree.Root);
            if (submenu is null)
            {
                var mainMenu = FindVisibleNode<NMainMenu>(tree.Root);
                var singleplayerButton = mainMenu is null
                    ? null
                    : FindNamedNode<NMainMenuTextButton>(mainMenu, "SingleplayerButton");
                if (singleplayerButton is { Visible: true, IsEnabled: true } && !_stages.HasFlag(Stage.SingleplayerOpened))
                {
                    _stages |= Stage.SingleplayerOpened;
                    singleplayerButton.EmitSignal(NClickableControl.SignalName.Released, singleplayerButton);
                }
                return;
            }

            var standardButton = FindNamedNode<NButton>(submenu, "StandardButton");
            if (standardButton is { Visible: true, IsEnabled: true } && !_stages.HasFlag(Stage.StandardOpened))
            {
                _stages |= Stage.StandardOpened;
                standardButton.EmitSignal(NClickableControl.SignalName.Released, standardButton);
            }
            return;
        }

        var expectedEntry = CharacterEntry(config.Character);
        var charButton = NodeHierarchy.Descendants(screen)
            .OfType<NCharacterSelectButton>()
            .FirstOrDefault(button => !button.IsRandom && button.Character?.Id.Entry == expectedEntry);
        if (charButton is null)
            return;
        if (charButton.IsLocked)
            throw new InvalidOperationException($"Requested character '{config.Character}' is locked in the active profile.");

        if (!_stages.HasFlag(Stage.CharacterSelected))
        {
            _stages |= Stage.CharacterSelected;
            charButton.Select();
            return;
        }

        var panel = screen.GetNodeOrNull<NAscensionPanel>("%AscensionPanel");
        if (panel is null)
            return;

        if (!_stages.HasFlag(Stage.AscensionApplied))
        {
            _stages |= Stage.AscensionApplied;
            panel.SetAscensionLevel(config.Ascension);
        }

        if (!_stages.HasFlag(Stage.SeedApplied))
        {
            _stages |= Stage.SeedApplied;
            NGame.Instance!.DebugSeedOverride = string.IsNullOrWhiteSpace(config.Seed)
                ? null
                : SeedHelper.CanonicalizeSeed(config.Seed);
        }

        if (DismissVisibleFtues())
            return;

        var confirm = screen.GetNodeOrNull<NConfirmButton>("ConfirmButton");
        if (confirm is { Visible: true, IsEnabled: true })
        {
            _stages |= Stage.ConfirmReleased;
            confirm.EmitSignal(NClickableControl.SignalName.Released, confirm);
        }
    }

    private bool DismissVisibleFtues()
    {
        if (Engine.GetMainLoop() is not SceneTree tree)
            return false;

        var tutorials = FindVisibleNode<NAcceptTutorialsFtue>(tree.Root);
        if (tutorials is not null)
        {
            if (!_stages.HasFlag(Stage.TutorialsDismissed))
            {
                var noButton = tutorials.GetNodeOrNull<NVerticalPopup>("VerticalPopup")?.NoButton;
                if (noButton is { Visible: true, IsEnabled: true })
                {
                    _stages |= Stage.TutorialsDismissed;
                    noButton.EmitSignal(NClickableControl.SignalName.Released, noButton);
                }
            }
            return true;
        }

        if (FindVisibleNode<NModalContainer>(tree.Root)?.OpenModal is Node ascensionFtue
            && ascensionFtue.GetType().Name.Contains("Ascension", StringComparison.Ordinal))
        {
            if (!_stages.HasFlag(Stage.AscensionFtueDismissed))
            {
                var confirm = NodeHierarchy.Descendants(ascensionFtue).OfType<NConfirmButton>().FirstOrDefault(b => b.Visible && b.IsEnabled);
                if (confirm is not null)
                {
                    _stages |= Stage.AscensionFtueDismissed;
                    confirm.EmitSignal(NClickableControl.SignalName.Released, confirm);
                }
            }
            return true;
        }

        return false;
    }

    private static string CharacterEntry(StandardCharacter character) => character switch
    {
        StandardCharacter.Ironclad => "IRONCLAD",
        StandardCharacter.Silent => "SILENT",
        StandardCharacter.Regent => "REGENT",
        StandardCharacter.Necrobinder => "NECROBINDER",
        StandardCharacter.Defect => "DEFECT",
        _ => throw new ArgumentOutOfRangeException(nameof(character)),
    };

    private static void ValidateAscensionUnlocked(SaveManager saveManager, HeadlessRunConfiguration config)
    {
        if (config.Ascension == 0)
            return;

        var (characterId, epochRevealed) = config.Character switch
        {
            StandardCharacter.Ironclad => (ModelDb.GetId<Ironclad>(), saveManager.IsEpochRevealed<Ironclad4Epoch>()),
            StandardCharacter.Silent => (ModelDb.GetId<Silent>(), saveManager.IsEpochRevealed<Silent4Epoch>()),
            StandardCharacter.Regent => (ModelDb.GetId<Regent>(), saveManager.IsEpochRevealed<Regent4Epoch>()),
            StandardCharacter.Necrobinder => (ModelDb.GetId<Necrobinder>(), saveManager.IsEpochRevealed<Necrobinder4Epoch>()),
            StandardCharacter.Defect => (ModelDb.GetId<Defect>(), saveManager.IsEpochRevealed<Defect4Epoch>()),
            _ => (ModelId.none, false),
        };

        var maxAscension = saveManager.Progress.GetStatsForCharacter(characterId)?.MaxAscension ?? 0;
        if (!epochRevealed || config.Ascension > maxAscension)
        {
            throw new InvalidOperationException(
                $"Requested ascension {config.Ascension} for {config.Character} is not unlocked in the active profile.");
        }
    }

    private static T? FindVisibleNode<T>(Node root) where T : CanvasItem =>
        NodeHierarchy.Descendants(root).OfType<T>().FirstOrDefault(node => node.IsVisibleInTree());

    private static T? FindNamedNode<T>(Node root, string name) where T : Node =>
        NodeHierarchy.Descendants(root).OfType<T>().FirstOrDefault(node => node.Name == name);
}
