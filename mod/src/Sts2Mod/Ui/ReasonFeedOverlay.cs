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
/// Renders recent decision reasons in a non-interactive left-margin card stack.
/// Uses Godot container layout (<see cref="VBoxContainer"/> + <see cref="PanelContainer"/>) so multi-line
/// wrapped text never overlaps adjacent rows, shifts below the relic bar automatically, and hides on
/// full-screen card grids or submenus.
/// </summary>
internal static class ReasonFeedOverlay
{
    private const string FeedNodeName = "Sts2AgentReasonFeed";
    private const int MaxVisibleCards = 3;
    private const float HorizontalMargin = 24f;
    private const float CardSeparation = 6f;

    private sealed class ReasonCardSlot
    {
        public required PanelContainer Panel { get; init; }
        public required StyleBoxFlat Style { get; init; }
        public required Label Label { get; init; }
        public long EntryId { get; set; } = -1;
    }

    private static VBoxContainer? _container;
    private static readonly List<ReasonCardSlot> Slots = [];
    private static long _renderedVersion = -1;
    private static long _renderedHalfSecond = -1;
    private static float _renderedCardWidth = -1f;

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
            var cardWidth = Math.Clamp(viewportSize.X * 0.205f, 320f, 396f);

            UpdatePosition(globalUi, container, viewportSize);

            if (ShouldHideForActiveScreen(globalUi))
            {
                container.Visible = false;
                _renderedHalfSecond = -1;
                return;
            }

            if (feed.Version == _renderedVersion
                && halfSecond == _renderedHalfSecond
                && MathF.Abs(cardWidth - _renderedCardWidth) < 0.5f)
            {
                return;
            }

            _renderedVersion = feed.Version;
            _renderedHalfSecond = halfSecond;
            _renderedCardWidth = cardWidth;

            var entries = feed.GetActive(now, MaxVisibleCards);
            if (entries.IsEmpty)
            {
                container.Visible = false;
                for (var index = 0; index < Slots.Count; index++)
                    Slots[index].Panel.Visible = false;
                return;
            }

            EnsureSlots(container, entries.Length);
            container.CustomMinimumSize = new Vector2(cardWidth, 0f);
            container.Size = new Vector2(cardWidth, 0f);

            for (var index = 0; index < Slots.Count; index++)
            {
                var slot = Slots[index];
                if (index >= entries.Length)
                {
                    slot.Panel.Visible = false;
                    slot.EntryId = -1;
                    continue;
                }

                var entry = entries[index];
                var isNewest = index == 0;
                ApplyCardAppearance(slot, entry, index, isNewest, cardWidth, feed.EntryLifetime, now);
                slot.Panel.Visible = true;
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
        _renderedCardWidth = -1f;

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
        container.AddThemeConstantOverride("separation", (int)CardSeparation);
        host.AddChild(container);
        _container = container;
        return container;
    }

    private static void UpdatePosition(NGlobalUi globalUi, VBoxContainer container, Vector2 viewportSize)
    {
        var relicBottom = 136f;
        if (globalUi.RelicInventory is { Visible: true } relics && GodotObject.IsInstanceValid(relics))
            relicBottom = Math.Max(relicBottom, relics.GlobalPosition.Y + relics.Size.Y);

        var maxTop = Math.Max(228f, viewportSize.Y * 0.45f);
        var topY = Math.Clamp(Math.Max(148f, relicBottom + 10f), 148f, maxTop);
        if (container.Position != new Vector2(HorizontalMargin, topY))
            container.Position = new Vector2(HorizontalMargin, topY);
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
            var style = new StyleBoxFlat
            {
                CornerRadiusTopLeft = 6,
                CornerRadiusTopRight = 6,
                CornerRadiusBottomLeft = 6,
                CornerRadiusBottomRight = 6,
                BorderWidthTop = 1,
                BorderWidthRight = 1,
                BorderWidthBottom = 1,
            };

            var panel = new PanelContainer
            {
                MouseFilter = Control.MouseFilterEnum.Ignore,
                SizeFlagsHorizontal = Control.SizeFlags.ExpandFill,
                Visible = false,
            };
            panel.AddThemeStyleboxOverride("panel", style);

            var label = new Label
            {
                MouseFilter = Control.MouseFilterEnum.Ignore,
                SizeFlagsHorizontal = Control.SizeFlags.ExpandFill,
                AutowrapMode = TextServer.AutowrapMode.WordSmart,
                VerticalAlignment = VerticalAlignment.Center,
                MaxLinesVisible = 3,
                TextOverrunBehavior = TextServer.OverrunBehavior.TrimEllipsis,
            };
            label.AddThemeFontSizeOverride("font_size", 15);
            label.AddThemeColorOverride("font_color", new Color(0.96f, 0.95f, 0.91f));
            label.AddThemeConstantOverride("outline_size", 4);
            label.AddThemeColorOverride("font_outline_color", new Color(0.01f, 0.01f, 0.02f, 0.94f));
            label.AddThemeConstantOverride("line_spacing", 2);

            panel.AddChild(label);
            container.AddChild(panel);
            Slots.Add(new ReasonCardSlot
            {
                Panel = panel,
                Style = style,
                Label = label,
            });
        }
    }

    private static void ApplyCardAppearance(
        ReasonCardSlot slot,
        ReasonFeedEntry entry,
        int index,
        bool isNewest,
        float cardWidth,
        TimeSpan entryLifetime,
        DateTimeOffset now)
    {
        var horizontalPadding = isNewest ? 12f : 11f;
        var verticalPadding = isNewest ? 7f : 6f;
        slot.Style.ContentMarginLeft = horizontalPadding;
        slot.Style.ContentMarginRight = horizontalPadding;
        slot.Style.ContentMarginTop = verticalPadding;
        slot.Style.ContentMarginBottom = verticalPadding;
        slot.Style.BorderWidthLeft = isNewest ? 3 : 2;
        slot.Style.BgColor = isNewest
            ? new Color(0.04f, 0.055f, 0.085f, 0.78f)
            : new Color(0.035f, 0.045f, 0.07f, 0.60f);
        slot.Style.BorderColor = isNewest
            ? new Color(0.92f, 0.76f, 0.34f, 0.90f)
            : new Color(0.58f, 0.56f, 0.48f, 0.42f);

        var textWidth = Math.Max(160f, cardWidth - horizontalPadding * 2f - 4f);
        slot.Label.CustomMinimumSize = new Vector2(textWidth, 0f);
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
        if (slot.EntryId != entry.Id && isNewest)
        {
            slot.Panel.Modulate = new Color(1f, 1f, 1f, 0.25f);
            var tween = slot.Panel.CreateTween();
            tween.TweenProperty(slot.Panel, "modulate:a", targetAlpha, 0.14);
        }
        else
        {
            slot.Panel.Modulate = new Color(1f, 1f, 1f, targetAlpha);
        }

        slot.EntryId = entry.Id;
    }
}
