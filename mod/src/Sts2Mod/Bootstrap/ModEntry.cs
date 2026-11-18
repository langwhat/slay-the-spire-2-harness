using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Catalog;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Transport;
using Sts2Mod.Developer;
using Sts2Mod.Execution;
using Sts2Mod.Observation;
using Sts2Mod.Ui;

namespace Sts2Mod.Bootstrap;

/// <summary>
/// Entry point for the <c>sts2agent</c> Mod. Owns the single-process frame loop and loopback HTTP bridge.
/// </summary>
[ModInitializer(nameof(Initialize))]
public static class ModEntry
{
    private const int DefaultPort = 17877;

    internal static AgentControlState ControlState { get; } = new();

    private static readonly GameCatalogProvider Catalog = new();
    private static readonly HeadlessRunStarter HeadlessStarter = new();
    private static readonly HeadlessTerminationHandler HeadlessExit = new();
    private static GameStepDispatcher? _steps;
    private static CheckpointService? _checkpoints;
    private static LaboratoryService? _laboratory;
    private static LoopbackHttpServer? _http;

    public static void Initialize()
    {
        GD.Print("[sts2agent] Initializing Slay the Spire 2 agent bridge.");
        ObservationHub.Initialize();
        SubscribeLifecycle();
        ReplayTimeScalePatch.Configure();
        ReplayPresentationPatch.Configure();
        new Harmony("sts2-harness.sts2agent").PatchAll(typeof(ModEntry).Assembly);
        StartHttpServer();

        if (Engine.GetMainLoop() is SceneTree tree)
        {
            HeadlessExit.RegisterIfRequested(tree);
            tree.ProcessFrame += OnProcessFrame;
        }
        else
        {
            GD.PrintErr("[sts2agent] SceneTree unavailable during Initialize; frame loop inactive.");
        }
    }

    internal static void CapturePreTerminalFrame()
    {
        if (!ObservationHub.IsTerminalLatched && RunManager.Instance.IsInProgress)
            ObservationHub.PublishCurrentFrame();
    }

    internal static void OnRunEnded(bool isVictory)
    {
        ObservationHub.PublishRunEnded(isVictory);
        if (_steps is not null)
            _steps.Advance(ObservationHub.CurrentRunInstanceId, MainMenuProbe.Observe());
        ControlState.SetMode(AgentControlMode.Paused);
        CardSelectionInterceptor.ResetForRunBoundary();
    }

    internal static void OnRunCleanUp()
    {
        if (_checkpoints?.OwnsInput != true)
            ControlState.SetMode(AgentControlMode.Paused);
        CardSelectionInterceptor.ResetForRunBoundary();
        ObservationHub.OnRunCleanUp();
    }

    private static void SubscribeLifecycle()
    {
        RunManager.Instance.RunStarted += _ =>
        {
            HeadlessStarter.OnRunStarted();
            var preserved = ControlState.ApplyRunStartedBoundary();
            CardSelectionInterceptor.ResetForRunBoundary();
            ObservationHub.OnRunStarted();
            _steps?.ClearOnRunReset();
            GD.Print($"[sts2agent] RunStarted: preserved_control={preserved.ToString().ToLowerInvariant()}, mode={ControlState.Mode.ToString().ToLowerInvariant()}.");
        };

        var combat = CombatManager.Instance;
        combat.CombatSetUp += _ => CardSelectionInterceptor.Install();
        combat.CombatEnded += _ => CardSelectionInterceptor.Uninstall();
        combat.CombatWon += _ => CardSelectionInterceptor.Uninstall();
    }

    private static void StartHttpServer()
    {
        if (_http is not null)
            return;

        var rawPort = System.Environment.GetEnvironmentVariable("STS2AGENT_PORT");
        var port = int.TryParse(rawPort, out var parsed) && parsed is > 0 and <= 65535
            ? parsed
            : DefaultPort;

        LoopbackHttpServer? server = null;
        try
        {
            var developerEnabled = DisplayServer.GetName() == "headless"
                                   && System.Environment.GetEnvironmentVariable("STS2_ALLOW_CHECKPOINTS") == "true";
            ObservationHub.CheckpointIdentityEnabled = developerEnabled;

            var steps = new GameStepDispatcher(
                ControlState,
                externalInputBusy: () => _checkpoints?.OwnsInput == true
                                         || _laboratory?.OwnsInput == true
                                         || HeadlessStarter.IsActive);
            ObservationHub.ProjectForRead = steps.Project;
            _steps = steps;
            _checkpoints = new CheckpointService(developerEnabled, ControlState, steps.CurrentStatus);
            _laboratory = new LaboratoryService(developerEnabled, ControlState, Catalog, steps.CurrentStatus);

            var handlers = new ModHttpRouter(steps, _checkpoints, _laboratory);
            server = new LoopbackHttpServer(port, handlers);
            server.Start();
            _http = server;
            server = null;
            GD.Print($"[sts2agent] Listening on {_http.Prefix}");
        }
        catch (Exception error)
        {
            server?.Dispose();
            _http = null;
            GD.PrintErr($"[sts2agent] Failed to start loopback HTTP server: {error.Message}");
        }
    }

    private static void OnProcessFrame()
    {
        if (HeadlessExit.TryHandleExitRequest(() =>
            {
                ReplayPresentationPatch.RecordSummary();
                ControlState.SetMode(AgentControlMode.Paused);
            }))
        {
            return;
        }

        Catalog.Advance();

        try
        {
            PortraitControlOverlay.Refresh();
            var menuReadiness = MainMenuProbe.Observe();
            ReplayPresentationPatch.ActivateBeforeInput(RunManager.Instance.IsInProgress);

            ObservationHub.PublishCurrentFrame();
            _steps?.Advance(ObservationHub.CurrentRunInstanceId, menuReadiness);
            HeadlessStarter.Advance();
            _checkpoints?.Advance();
            _laboratory?.Advance();
        }
        catch (Exception error)
        {
            ObservationHub.Invalidate();
            GD.PrintErr($"[sts2agent] Frame update error: {error}");
        }
    }

    private sealed class ModHttpRouter(
        GameStepDispatcher steps,
        CheckpointService checkpoints,
        LaboratoryService laboratory) : IModHttpHandlers
    {
        public (int StatusCode, string Json) GetStatus() => steps.StatusJson();

        public (int StatusCode, string Json) GetState() => (200, ObservationHub.ReadJson());

        public Task<(int StatusCode, string Json)> Inspect(string body) =>
            Task.FromResult(ObservationHub.InspectJson(body));

        public Task<(int StatusCode, string Json)> Catalog(string body) =>
            ModEntry.Catalog.ReadAsync(body);

        public (int StatusCode, string Json) SubmitAction(string body) =>
            steps.Submit(body);

        public (int StatusCode, string Json) GetControl() =>
            ControlState.ReadJson();

        public (int StatusCode, string Json) SetControl(string body) =>
            ControlState.WriteJson(body);

        public Task<(int StatusCode, string Json)> PresentMap(string body) =>
            steps.MapPresentation.Present(body);

        public Task<(int StatusCode, string Json)> VerifyMapRestoration(string body) =>
            steps.MapPresentation.Verify(body);

        public (int StatusCode, string Json) GetCheckpointIdentity() =>
            checkpoints.ReadIdentity();

        public (int StatusCode, string Json) SubmitCheckpoint(string body) =>
            checkpoints.Submit(body, otherDeveloperWorkBusy: laboratory.OwnsInput || HeadlessStarter.IsActive);

        public Task<(int StatusCode, string Json)> PrepareLaboratoryScenario(string body) =>
            laboratory.PrepareScenarioAsync(body, checkpointBusy: checkpoints.OwnsInput || HeadlessStarter.IsActive);
    }
}
