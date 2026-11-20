using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.Combat;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Vfx;
using MegaCrit.Sts2.Core.Nodes.Vfx.Utilities;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;
using Sts2Mod.Bootstrap;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Developer;

namespace Sts2Mod.Developer;

/// <summary>
/// Optional presentation fast-forward patches active only in explicit headless replay launches.
/// </summary>
internal static class ReplayPresentationPatch
{
    private static ReplayPresentationLifecycle _lifecycle = new(false);
    private static readonly ConditionalWeakTable<Tween, object> CardTweens = new();
    private static int _cardTweenCompletions;
    private static int _cardFlights;
    private static int _hitStops;

    internal static bool Enabled => _lifecycle.Enabled;
    internal static bool Active => _lifecycle.Active;
    internal static FastModeType OriginalFastMode { get; private set; }

    internal static void Configure()
    {
        _lifecycle = new ReplayPresentationLifecycle(ReplayPresentation.Parse(
            System.Environment.GetEnvironmentVariable("STS2AGENT_REPLAY_PRESENTATION"),
            DisplayServer.GetName() == "headless",
            System.Environment.GetEnvironmentVariable("STS2AGENT_REPLAY_ACTIVE") == "1"));

        if (Enabled && Engine.TimeScale != 1)
            throw new InvalidOperationException("Instant replay presentation requires Engine.TimeScale=1.");
    }

    internal static void ActivateBeforeInput(bool runInProgress)
    {
        if (!Enabled
            || Active
            || !runInProgress
            || ModEntry.ControlState.Mode != AgentControlMode.Running)
        {
            return;
        }

        OriginalFastMode = SaveManager.Instance.PrefsSave.FastMode;
        var originalFps = Engine.MaxFps;
        _lifecycle.Activate(() =>
        {
            SaveManager.Instance.PrefsSave.FastMode = FastModeType.Instant;
            Engine.MaxFps = originalFps > 0 ? originalFps : 60;
        });

        GD.Print($"[sts2agent] Replay presentation active: original_fast_mode={OriginalFastMode}, fps={originalFps}->{Engine.MaxFps}.");
    }

    internal static void RecordSummary()
    {
        if (!Enabled)
            return;
        GD.Print(
            $"[sts2agent] Replay presentation summary: active={Active}, " +
            $"fast_mode={SaveManager.Instance.PrefsSave.FastMode}, fps={Engine.MaxFps}, " +
            $"card_tweens={_cardTweenCompletions}, card_flights={_cardFlights}, hit_stops={_hitStops}.");
    }

    [HarmonyPatch(typeof(NGame), nameof(NGame.DoHitStop))]
    private static class HitStopPatch
    {
        private static bool Prepare() => Enabled;

        private static bool Prefix()
        {
            if (!Active)
                return true;
            _hitStops++;
            return false;
        }
    }

    [HarmonyPatch(typeof(NCreatureStateDisplay), nameof(NCreatureStateDisplay.AnimateIn))]
    private static class KeepDisplayRandomCallPatch
    {
        private static bool Prepare() => Enabled;

        private static void Prefix(out FastModeType? __state)
        {
            __state = null;
            if (!Active)
                return;
            __state = SaveManager.Instance.PrefsSave.FastMode;
            SaveManager.Instance.PrefsSave.FastMode = OriginalFastMode;
        }

        private static void Finalizer(FastModeType? __state)
        {
            if (__state is { } mode)
                SaveManager.Instance.PrefsSave.FastMode = mode;
        }
    }

    [HarmonyPatch]
    private static class MarkCardTweenPatch
    {
        private static bool Prepare() => Enabled;

        private static MethodBase TargetMethod() =>
            typeof(CardPileCmd)
                .GetMethods(BindingFlags.NonPublic | BindingFlags.Static)
                .Single(method => method.Name == "GetTweenForCardsChangingPiles" && method.ReturnType == typeof(Tween));

        private static void Postfix(Tween? __result)
        {
            if (Active && __result is not null)
                CardTweens.GetValue(__result, _ => new object());
        }
    }

    [HarmonyPatch(
        typeof(TweenHelper),
        nameof(TweenHelper.AwaitFinished),
        [typeof(Tween), typeof(Node), typeof(CancellationToken)])]
    private static class CompleteCardTweenAfterSubscriptionPatch
    {
        private static bool Prepare() => Enabled;

        private static void Postfix(Tween tween, Node owner)
        {
            if (!Active
                || !CardTweens.TryGetValue(tween, out _)
                || !GodotObject.IsInstanceValid(owner)
                || !owner.IsInsideTree()
                || owner.GetTree().Paused
                || !tween.IsValid()
                || !tween.IsRunning())
            {
                return;
            }

            tween.FastForwardToCompletion();
            _cardTweenCompletions++;
        }
    }

    [HarmonyPatch(typeof(NCardFlyVfx), "PlayAnim")]
    private static class ShortenCardFlightPatch
    {
        private static bool Prepare() => Enabled;

        private static void Prefix(ref float ____duration)
        {
            if (!Active)
                return;
            ____duration = 0.000001f;
            _cardFlights++;
        }
    }

    [HarmonyPatch(typeof(NTransition), nameof(NTransition.RoomFadeIn))]
    private static class CompleteRoomFadePatch
    {
        private static bool Prepare() => Enabled;

        private static void Postfix(NTransition __instance, Tween? ____tween)
        {
            if (Active
                && !__instance.GetTree().Paused
                && ____tween is { } tween
                && tween.IsValid()
                && tween.IsRunning())
            {
                tween.FastForwardToCompletion();
            }
        }
    }
}

[HarmonyPatch(typeof(NHitStop), "SetTimeScale")]
internal static class ReplayTimeScalePatch
{
    private static ReplayTimeScale? _scale;
    private static string? _logPath;

    internal static void Configure()
    {
        _scale = ReplayTimeScale.Parse(
            System.Environment.GetEnvironmentVariable("STS2AGENT_REPLAY_TIME_SCALE"),
            DisplayServer.GetName() == "headless",
            System.Environment.GetEnvironmentVariable("STS2AGENT_REPLAY_ACTIVE") == "1");
        if (_scale is null)
            return;

        _logPath = System.Environment.GetEnvironmentVariable("STS2AGENT_REPLAY_TIME_SCALE_LOG");
        if (_logPath is not null && !Path.IsPathFullyQualified(_logPath))
            throw new InvalidOperationException("STS2AGENT_REPLAY_TIME_SCALE_LOG must be an absolute file path.");

        Log("configured", 1f, Engine.TimeScale);
    }

    private static bool Prepare() => _scale is not null;

    private static void Prefix(ref float timeScale, out (float Relative, double Before) __state)
    {
        __state = (timeScale, Engine.TimeScale);
        timeScale = _scale!.ApplyHitStop(timeScale);
    }

    private static void Postfix((float Relative, double Before) __state) =>
        Log("hit_stop", __state.Relative, __state.Before);

    private static void Log(string stage, float relative, double before)
    {
        if (_logPath is null)
            return;

        File.AppendAllText(_logPath, JsonSerializer.Serialize(new
        {
            stage,
            utc = DateTimeOffset.UtcNow,
            ticks_us = Time.GetTicksUsec(),
            base_scale = _scale!.BaseScale,
            relative_scale = relative,
            before,
            actual = Engine.TimeScale,
            tree_paused = (Engine.GetMainLoop() as SceneTree)?.Paused,
            input_control = ModEntry.ControlState.Mode.ToString(),
        }) + "\n");
    }
}
