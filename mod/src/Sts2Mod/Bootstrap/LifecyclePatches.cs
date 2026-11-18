using System.Runtime.InteropServices;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Assets;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Developer;

namespace Sts2Mod.Bootstrap;

internal static class MainMenuProbe
{
    public static MainMenuReadinessObservation Observe()
    {
        var game = NGame.Instance;
        var mainMenu = game?.MainMenu;
        var mainMenuValid = mainMenu is not null && GodotObject.IsInstanceValid(mainMenu);
        var submenuStack = mainMenuValid ? mainMenu!.SubmenuStack : null;
        var submenuStackPresent = submenuStack is not null && GodotObject.IsInstanceValid(submenuStack);

        return new MainMenuReadinessObservation(
            GamePresent: game is not null,
            MainMenuPresent: mainMenu is not null,
            MainMenuValid: mainMenuValid,
            MainMenuInsideTree: mainMenuValid && mainMenu!.IsInsideTree(),
            MainMenuVisibleInTree: mainMenuValid && mainMenu!.IsVisibleInTree(),
            SubmenuStackPresent: submenuStackPresent,
            SubmenusOpen: submenuStackPresent && submenuStack!.SubmenusOpen,
            TransitionInProgress: game?.Transition.InTransition == true);
    }
}

internal sealed class HeadlessTerminationHandler
{
    private PosixSignalRegistration? _registration;
    private int _exitRequested;

    public bool Quitting { get; private set; }

    public void RegisterIfRequested(SceneTree tree)
    {
        if (!OperatingSystem.IsMacOS()
            || System.Environment.GetEnvironmentVariable("STS2AGENT_GRACEFUL_HEADLESS_EXIT") != "1")
        {
            return;
        }

        _registration = PosixSignalRegistration.Create(
            PosixSignal.SIGTERM,
            context =>
            {
                context.Cancel = true;
                Interlocked.Exchange(ref _exitRequested, 1);
            });
        tree.Root.TreeExiting += () => _registration?.Dispose();
        GD.Print("[sts2agent] Graceful headless SIGTERM handling enabled.");
    }

    public bool TryHandleExitRequest(Action beforeQuit)
    {
        if (Quitting)
            return true;
        if (Volatile.Read(ref _exitRequested) == 0)
            return false;
        if (NGame.Instance is not { } game)
            return true;

        Quitting = true;
        beforeQuit();
        GD.Print("[sts2agent] Headless termination invoking NGame.Quit.");
        try
        {
            game.Quit();
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Headless NGame.Quit failed: {error}");
        }
        return true;
    }
}

[HarmonyPatch(typeof(NGame), "LaunchMainMenu")]
internal static class StartupLogoPatch
{
    private static void Prefix(ref bool skipLogo) => skipLogo = true;
}

[HarmonyPatch(typeof(PreloadManager), nameof(PreloadManager.LoadMainMenuEssentials))]
internal static class StartupAssetPreloadPatch
{
    private static bool Prefix(ref Task __result)
    {
        foreach (var path in AssetSets.MainMenuEssentials)
        {
            var resource = ResourceLoader.Load<Resource>(path, typeHint: null, ResourceLoader.CacheMode.Reuse)
                ?? throw new InvalidOperationException($"Failed to synchronously load startup asset: {path}");
            PreloadManager.Cache.SetAsset(path, resource);
        }

        __result = Task.CompletedTask;
        return false;
    }
}

[HarmonyPatch(typeof(PreloadManager), nameof(PreloadManager.LoadCommonAndMainMenuAssets))]
internal static class CommonMenuPreloadPatch
{
    private static bool Prefix(ref Task __result)
    {
        PreloadManager.Cache.UnloadMissedCacheAssets();
        var requiredAssets = new HashSet<string>(AssetSets.CommonAssets);
        requiredAssets.UnionWith(AssetSets.MainMenuSet);
        PreloadManager.Cache.UnloadAssets(
            PreloadManager.Cache.GetLoadedCacheAssets().Except(requiredAssets));
        __result = Task.CompletedTask;
        return false;
    }
}

[HarmonyPatch(typeof(RunManager), nameof(RunManager.OnEnded))]
internal static class RunManagerOnEndedPatch
{
    private static void Prefix() => ModEntry.CapturePreTerminalFrame();

    private static void Postfix(bool isVictory) => ModEntry.OnRunEnded(isVictory);
}

[HarmonyPatch(typeof(RunManager), nameof(RunManager.CleanUp))]
internal static class RunManagerCleanUpPatch
{
    private static void Postfix() => ModEntry.OnRunCleanUp();
}
