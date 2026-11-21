using Godot;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events.Custom;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.InspectScreens;
using Sts2Mod.Bootstrap;
using Sts2Mod.Core.Display;

namespace Sts2Mod.Ui;

/// <summary>
/// Renders recent decision reasons in a non-interactive left-margin text stack.
/// Wrapped rows stay in the upper-left margin and hide on full-screen card grids or submenus.
/// </summary>
internal static class ReasonFeedOverlay
{
    private const string FeedNodeName = "Sts2AgentReasonFeed";
    private const int MaxVisibleReasons = 3;
    private const float LeftFraction = 0.022f;
    private const float TopFraction = 0.185f;
    private const int ReasonSeparation = 6;

    private sealed class ReasonSlot
    {
        public required Label Label { get; init; }
        public long EntryId { get; set; } = -1;
    }

    private static VBoxContainer? _container;
    private static readonly List<ReasonSlot> Slots = [];
    private static long _renderedVersion = -1;
    private static long _renderedHalfSecond = -1;
    private static float _renderedWidth = -1f;

    public static void Install(NRun run)
    {
        if (ViewerPacing.IsHeadless)
            return;

        try
        {
            if (GodotObject.IsInstanceValid(run)
                && run.GlobalUi is { } globalUi
                && GodotObject.IsInstanceValid(globalUi))
            {
                EnsureContainer(globalUi);
            }
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Reason feed install skipped: {error.Message}");
        }
    }

    public static void Refresh()
    {
        if (ViewerPacing.IsHeadless)
            return;

        var run = NRun.Instance;
        if (run is null
            || !GodotObject.IsInstanceValid(run)
            || run.GlobalUi is not { } globalUi
            || !GodotObject.IsInstanceValid(globalUi))
        {
            return;
        }

        try
        {
            var container = EnsureContainer(globalUi);
            if (container is null)
                return;

            var now = DateTimeOffset.UtcNow;
            var halfSecond = now.ToUnixTimeMilliseconds() / 500;
            var feed = ModEntry.ReasonFeed;
            var viewportSize = globalUi.GetViewportRect().Size;
            var width = Math.Clamp(viewportSize.X * 0.205f, 320f, 396f);

            var position = new Vector2(viewportSize.X * LeftFraction, viewportSize.Y * TopFraction);
            if (container.Position != position)
                container.Position = position;

            if (ShouldHideForActiveScreen(globalUi))
            {
                container.Visible = false;
                _renderedHalfSecond = -1;
                return;
            }

            if (feed.Version == _renderedVersion
                && halfSecond == _renderedHalfSecond
                && MathF.Abs(width - _renderedWidth) < 0.5f)
            {
                return;
            }

            _renderedVersion = feed.Version;
            _renderedHalfSecond = halfSecond;
            _renderedWidth = width;

            var entries = feed.GetActive(now, MaxVisibleReasons);
            if (entries.IsEmpty)
            {
                container.Visible = false;
                for (var index = 0; index < Slots.Count; index++)
                    Slots[index].Label.Visible = false;
                return;
            }

            EnsureSlots(container, entries.Length);
            container.CustomMinimumSize = new Vector2(width, 0f);
            container.Size = new Vector2(width, 0f);

            for (var index = 0; index < Slots.Count; index++)
            {
                var slot = Slots[index];
                if (index >= entries.Length)
                {
                    slot.Label.Visible = false;
                    slot.EntryId = -1;
                    continue;
                }

                var entry = entries[index];
                ApplyReason(slot, entry, index, width, feed.EntryLifetime, now);
                slot.Label.Visible = true;
            }

            container.ResetSize();
            container.Visible = true;
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Reason feed refresh failed: {error.Message}");
            _container = null;
            Slots.Clear();
            _renderedVersion = -1;
        }
    }

    private static VBoxContainer? EnsureContainer(NGlobalUi globalUi)
    {
        if (globalUi.AboveTopBarVfxContainer is not { } host || !GodotObject.IsInstanceValid(host))
            return null;

        if (_container is not null
            && GodotObject.IsInstanceValid(_container)
            && _container.GetParent() == host)
        {
            return _container;
        }

        Slots.Clear();
        _renderedVersion = -1;
        _renderedHalfSecond = -1;
        _renderedWidth = -1f;

        if (host.GetNodeOrNull<Node>(FeedNodeName) is { } existing)
        {
            host.RemoveChild(existing);
            existing.QueueFree();
        }
        var container = new VBoxContainer
        {
            Name = FeedNodeName,
            MouseFilter = Control.MouseFilterEnum.Ignore,
            ZIndex = 90,
            ZAsRelative = false,
            Visible = false,
        };
        container.AddThemeConstantOverride("separation", ReasonSeparation);
        host.AddChild(container);
        _container = container;
        return container;
    }

    private static bool ShouldHideForActiveScreen(NGlobalUi globalUi)
    {
        if (globalUi.CapstoneContainer is { InUse: true } capstone && GodotObject.IsInstanceValid(capstone))
            return true;
        if (NModalContainer.Instance is { OpenModal: not null } modals && GodotObject.IsInstanceValid(modals))
            return true;
        if (NMerchantRoom.Instance?.Inventory is { IsOpen: true }
            || (NEventRoom.Instance?.CustomEventNode as NFakeMerchant)?.Inventory is { IsOpen: true })
        {
            return true;
        }
        if (globalUi.Overlays is { } overlays && GodotObject.IsInstanceValid(overlays))
        {
            var topOverlay = overlays.Peek();
            if (topOverlay is NCardGridSelectionScreen
                or NGameOverScreen
                or NInspectCardScreen
                or NInspectRelicScreen)
            {
                return true;
            }
        }
        return false;
    }

    private static void EnsureSlots(VBoxContainer container, int needed)
    {
        while (Slots.Count < needed)
        {
            var label = new Label
            {
                MouseFilter = Control.MouseFilterEnum.Ignore,
                SizeFlagsHorizontal = Control.SizeFlags.ExpandFill,
                // Leave trimming disabled so the wrapped label can report its full height.
                AutowrapMode = TextServer.AutowrapMode.WordSmart,
                MaxLinesVisible = 3,
                Visible = false,
            };
            label.AddThemeFontSizeOverride("font_size", 21);
            label.AddThemeColorOverride("font_color", new Color(0.96f, 0.95f, 0.91f));
            label.AddThemeConstantOverride("outline_size", 4);
            label.AddThemeColorOverride("font_outline_color", new Color(0.01f, 0.01f, 0.02f, 0.94f));
            label.AddThemeConstantOverride("line_spacing", 2);

            container.AddChild(label);
            Slots.Add(new ReasonSlot { Label = label });
        }
    }

    private static void ApplyReason(
        ReasonSlot slot,
        ReasonFeedEntry entry,
        int index,
        float width,
        TimeSpan entryLifetime,
        DateTimeOffset now)
    {
        slot.Label.CustomMinimumSize = new Vector2(width, 0f);
        if (!string.Equals(slot.Label.Text, entry.Text, StringComparison.Ordinal))
            slot.Label.Text = entry.Text;

        var ageSeconds = Math.Max(0d, (now - entry.RecordedAt).TotalSeconds);
        var remainingSeconds = entryLifetime.TotalSeconds - ageSeconds;
        var fadeFactor = remainingSeconds < 2.5d
            ? (float)Math.Clamp(remainingSeconds / 2.5d, 0.15d, 1d)
            : 1f;
        var rankAlpha = index switch
        {
            0 => 1f,
            1 => 0.82f,
            _ => 0.64f,
        };

        var targetAlpha = rankAlpha * fadeFactor;
        if (slot.EntryId != entry.Id && index == 0)
        {
            slot.Label.Modulate = new Color(1f, 1f, 1f, 0.25f);
            var tween = slot.Label.CreateTween();
            tween.TweenProperty(slot.Label, "modulate:a", targetAlpha, 0.14);
        }
        else
        {
            slot.Label.Modulate = new Color(1f, 1f, 1f, targetAlpha);
        }

        slot.EntryId = entry.Id;
    }
}
