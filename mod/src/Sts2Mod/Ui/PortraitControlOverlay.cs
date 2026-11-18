using Godot;
using HarmonyLib;
using MegaCrit.sts2.Core.Nodes.TopBar;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using Sts2Mod.Bootstrap;
using Sts2Mod.Core.Control;

namespace Sts2Mod.Ui;

[HarmonyPatch(typeof(NRun), nameof(NRun._Ready))]
internal static class NRunPortraitControlPatch
{
    private static void Postfix(NRun __instance) =>
        PortraitControlOverlay.Install(__instance);
}

/// <summary>
/// Attaches the in-game start/pause control directly to the top-bar character portrait,
/// with an English fallback button if the portrait node is unavailable.
/// </summary>
internal static class PortraitControlOverlay
{
    private const string FallbackButtonName = "Sts2AgentControlButton";
    private const string PortraitButtonName = "Sts2AgentPortraitControl";
    private const string PortraitStatusName = "Sts2AgentPortraitStatus";

    private static Button? _portraitButton;
    private static Label? _portraitStatus;
    private static AgentControlMode? _renderedMode;

    public static void Install(NRun run)
    {
        if (!GodotObject.IsInstanceValid(run)
            || run.GlobalUi is not NGlobalUi globalUi
            || !GodotObject.IsInstanceValid(globalUi)
            || globalUi.AboveTopBarVfxContainer is not Control container
            || !GodotObject.IsInstanceValid(container))
        {
            return;
        }

        var fallback = container.GetNodeOrNull<Button>(FallbackButtonName);
        if (fallback is null)
        {
            fallback = new Button
            {
                Name = FallbackButtonName,
                CustomMinimumSize = new Vector2(160f, 48f),
                Position = new Vector2(24f, 24f),
                ZIndex = 100,
                MouseFilter = Control.MouseFilterEnum.Stop,
            };
            fallback.Pressed += OnTogglePressed;
            container.AddChild(fallback);
        }

        var portraitInstalled = InstallOnPortrait(globalUi);
        fallback.Visible = !portraitInstalled;
        UpdateFallbackText(fallback);
    }

    public static void Refresh()
    {
        var run = NRun.Instance;
        if (run is null
            || !GodotObject.IsInstanceValid(run)
            || run.GlobalUi is not { } globalUi
            || !GodotObject.IsInstanceValid(globalUi))
        {
            return;
        }

        var portraitActive = RefreshPortrait(globalUi);
        if (globalUi.AboveTopBarVfxContainer is { } container && GodotObject.IsInstanceValid(container))
        {
            var fallback = container.GetNodeOrNull<Button>(FallbackButtonName);
            if (fallback is not null && GodotObject.IsInstanceValid(fallback))
            {
                fallback.Visible = !portraitActive;
                UpdateFallbackText(fallback);
            }
        }
    }

    private static bool InstallOnPortrait(NGlobalUi globalUi)
    {
        if (globalUi.TopBar is null
            || !GodotObject.IsInstanceValid(globalUi.TopBar)
            || globalUi.TopBar.Portrait is not NTopBarPortrait portrait
            || !GodotObject.IsInstanceValid(portrait))
        {
            return false;
        }

        EnsurePortraitNodes(portrait);
        return _portraitButton is not null;
    }

    private static bool RefreshPortrait(NGlobalUi globalUi)
    {
        if (!InstallOnPortrait(globalUi) || _portraitButton is null || _portraitStatus is null)
            return false;

        var portrait = globalUi.TopBar.Portrait;
        _portraitButton.Size = portrait.Size;
        _portraitStatus.Position = new Vector2(Math.Max(0f, portrait.Size.X - 18f), 2f);
        _portraitStatus.Size = new Vector2(18f, 18f);

        var mode = ModEntry.ControlState.Mode;
        var phase = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000d;
        var pulse = mode == AgentControlMode.Running
            ? 0.86f + 0.14f * (float)((Math.Sin(phase * 2.0) + 1.0) / 2.0)
            : 0.55f;
        var color = mode == AgentControlMode.Running
            ? new Color(0.45f, 1f, 0.45f)
            : new Color(0.62f, 0.62f, 0.68f);

        if (_renderedMode != mode)
        {
            _renderedMode = mode;
            var style = new StyleBoxFlat
            {
                BgColor = Colors.Transparent,
                BorderColor = color,
                BorderWidthLeft = 3,
                BorderWidthTop = 3,
                BorderWidthRight = 3,
                BorderWidthBottom = 3,
                CornerRadiusTopLeft = 6,
                CornerRadiusTopRight = 6,
                CornerRadiusBottomLeft = 6,
                CornerRadiusBottomRight = 6,
            };
            _portraitButton.AddThemeStyleboxOverride("normal", style);
            _portraitButton.AddThemeStyleboxOverride("hover", style);
            _portraitButton.AddThemeStyleboxOverride("pressed", style);
            _portraitButton.TooltipText = mode == AgentControlMode.Running
                ? "AI: Running (click portrait to pause)"
                : "AI: Paused (click portrait to start)";
        }

        _portraitButton.Modulate = new Color(1f, 1f, 1f, pulse);
        _portraitStatus.Text = "●";
        _portraitStatus.Modulate = new Color(color, pulse);
        return true;
    }

    private static void EnsurePortraitNodes(NTopBarPortrait portrait)
    {
        if (_portraitButton is null
            || !GodotObject.IsInstanceValid(_portraitButton)
            || _portraitButton.GetParent() != portrait)
        {
            _portraitButton = portrait.GetNodeOrNull<Button>(PortraitButtonName) ?? new Button
            {
                Name = PortraitButtonName,
                MouseFilter = Control.MouseFilterEnum.Stop,
                FocusMode = Control.FocusModeEnum.None,
                Flat = true,
                ZIndex = 20,
            };
            if (_portraitButton.GetParent() is null)
            {
                _portraitButton.SetAnchorsAndOffsetsPreset(Control.LayoutPreset.FullRect);
                _portraitButton.Pressed += OnTogglePressed;
                portrait.AddChild(_portraitButton);
            }
            _renderedMode = null;
        }

        if (_portraitStatus is null
            || !GodotObject.IsInstanceValid(_portraitStatus)
            || _portraitStatus.GetParent() != portrait)
        {
            _portraitStatus = portrait.GetNodeOrNull<Label>(PortraitStatusName) ?? new Label
            {
                Name = PortraitStatusName,
                MouseFilter = Control.MouseFilterEnum.Ignore,
                ZIndex = 21,
                Text = "●",
            };
            _portraitStatus.AddThemeFontSizeOverride("font_size", 18);
            _portraitStatus.AddThemeColorOverride("font_outline_color", Colors.Black);
            _portraitStatus.AddThemeConstantOverride("outline_size", 4);
            if (_portraitStatus.GetParent() is null)
                portrait.AddChild(_portraitStatus);
        }
    }

    private static void OnTogglePressed()
    {
        if (NRun.Instance is null || !GodotObject.IsInstanceValid(NRun.Instance))
            return;
        ModEntry.ControlState.Toggle();
        Refresh();
    }

    private static void UpdateFallbackText(Button button)
    {
        button.Text = ModEntry.ControlState.Mode == AgentControlMode.Running
            ? "Pause AI"
            : "Start AI";
    }
}
