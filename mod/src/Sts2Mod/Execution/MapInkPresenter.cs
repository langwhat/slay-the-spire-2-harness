using System.Collections.Concurrent;
using System.Reflection;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Assets;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Observation;
using Sts2Mod.Ui;

namespace Sts2Mod.Execution;

internal sealed class NativeMapAnimations
{
    private static readonly FieldInfo? OpeningField = AccessTools.Field(typeof(NMapScreen), "_actAnimTween");
    private static readonly FieldInfo? TransitionField = AccessTools.Field(typeof(NMapScreen), "_tween");
    private static readonly FieldInfo? DraggingField = AccessTools.Field(typeof(NMapScreen), "_isDragging");

    private readonly object _map;
    private readonly FieldInfo _openingField;
    private readonly FieldInfo _transitionField;
    private readonly FieldInfo _draggingField;
    private readonly List<Tween> _observed = [];

    private NativeMapAnimations(object map, FieldInfo opening, FieldInfo transition, FieldInfo dragging)
    {
        _map = map;
        _openingField = opening;
        _transitionField = transition;
        _draggingField = dragging;
    }

    public bool IsDragging => (bool)_draggingField.GetValue(_map)!;

    public bool CanAwaitOpening => !IsDragging && Busy();

    public static NativeMapAnimations? Capture(NMapScreen map) =>
        Capture(map, OpeningField, TransitionField, DraggingField);

    internal static NativeMapAnimations? Capture(
        object map,
        FieldInfo? opening,
        FieldInfo? transition,
        FieldInfo? dragging)
    {
        if (opening is null || opening.IsStatic || opening.FieldType != typeof(Tween)
            || transition is null || transition.IsStatic || transition.FieldType != typeof(Tween)
            || dragging is null || dragging.IsStatic || dragging.FieldType != typeof(bool))
        {
            return null;
        }
        return new(map, opening, transition, dragging);
    }

    public bool Busy()
    {
        var opening = (Tween?)_openingField.GetValue(_map);
        var transition = (Tween?)_transitionField.GetValue(_map);
        Observe(opening);
        Observe(transition);
        return opening is not null
               || IsDragging
               || _observed.Any(tween => GodotObject.IsInstanceValid(tween) && tween.IsValid() && tween.IsRunning());
    }

    private void Observe(Tween? tween)
    {
        if (tween is not null && !_observed.Any(previous => ReferenceEquals(previous, tween)))
            _observed.Add(tween);
    }
}

internal sealed class NativeMapScrollTarget
{
    private static readonly FieldInfo? ContainerField = AccessTools.Field(typeof(NMapScreen), "_mapContainer");
    private static readonly FieldInfo? TargetField = AccessTools.Field(typeof(NMapScreen), "_targetDragPos");

    private readonly object _map;
    private readonly Control _pan;
    private readonly FieldInfo _containerField;
    private readonly FieldInfo _targetField;

    private NativeMapScrollTarget(
        object map,
        Control pan,
        FieldInfo containerField,
        FieldInfo targetField,
        Vector2 original)
    {
        _map = map;
        _pan = pan;
        _containerField = containerField;
        _targetField = targetField;
        Original = original;
    }

    public Vector2 Original { get; }
    public bool IsBound => ReferenceEquals(_containerField.GetValue(_map), _pan);
    public Vector2 Current => (Vector2)_targetField.GetValue(_map)!;

    public void Set(Vector2 value)
    {
        if (!IsBound)
            throw new InvalidOperationException("The native map container changed during presentation.");
        _targetField.SetValue(_map, value);
    }

    public static NativeMapScrollTarget? Capture(NMapScreen map, Control pan) =>
        Capture(map, pan, ContainerField, TargetField);

    internal static NativeMapScrollTarget? Capture(
        object map,
        Control pan,
        FieldInfo? containerField,
        FieldInfo? targetField)
    {
        if (containerField is null
            || containerField.IsStatic
            || containerField.FieldType != typeof(Control)
            || targetField is null
            || targetField.IsStatic
            || targetField.IsInitOnly
            || targetField.IsLiteral
            || targetField.FieldType != typeof(Vector2)
            || !ReferenceEquals(containerField.GetValue(map), pan)
            || targetField.GetValue(map) is not Vector2 original)
        {
            return null;
        }
        return new(map, pan, containerField, targetField, original);
    }
}

/// <summary>
/// Executes <c>/map/present</c> ink drawing and <c>/map/restore/verify</c> viewport restoration on the game thread.
/// Centers the active stroke vertically in GUI mode with an in-canvas quill tip while keeping headless mode instant.
/// </summary>
internal sealed class MapInkPresenter
{
    private const float NativeScrollLimitBottom = -600f;
    private const float NativeScrollLimitTop = 1800f;
    private const ulong FinishedRouteHoldMs = 350;
    private const string QuillNodeName = "Sts2AgentMapQuill";

    private sealed record SurfaceCapture(
        NMapScreen Map,
        bool WasOpen,
        object? Overlay,
        object? Choice,
        NMerchantRoom? Merchant,
        bool? ShopOpen,
        NClickableControl[] Controls,
        ScrollContainer? Scroll,
        int ScrollX,
        int ScrollY,
        Control Pan,
        Vector2 PanPosition,
        NativeMapScrollTarget ScrollTarget,
        NativeMapAnimations Animations,
        Dictionary<string, NMapPoint> Points,
        string SerializedState,
        Control.MouseFilterEnum MouseFilter,
        int LineCount,
        int PointCount,
        NativeInputReadiness Readiness,
        Control? Focus);

    private sealed class ActiveDisplay(PresentationRequest request, SurfaceCapture capture)
    {
        public readonly PresentationRequest Request = request;
        public SurfaceCapture Capture = capture;
        public readonly TaskCompletionSource Worker = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int Edge;
        public float Progress;
        public int FollowFrame;
        public int TargetFollowFrame;
        public int RestoreFrame;
        public ulong HoldUntilMsec;
        public MapOpeningBarrier? OpeningBarrier;
        public TextureRect? Quill;
        public bool Drawing;
        public bool Returning;
        public bool Closed;
        public bool OpenedMap;
        public bool AbandonRestore;
        public PresentationDrawing Outcome = PresentationDrawing.Shown;
    }

    private static ImageTexture? _cachedQuillTexture;
    private static bool _quillLoadAttempted;

    private readonly StepGateway _gateway;
    private readonly ConcurrentQueue<Action> _requests = new();
    private ActiveDisplay? _display;
    private SurfaceCapture? _capture;
    private bool _returnNeeded;

    public MapInkPresenter(StepGateway gateway)
    {
        _gateway = gateway;
        Transport = new(SubmitAsync, VerifyAsync);
    }

    public MapPresentationTransport Transport { get; }

    public void Advance()
    {
        while (_requests.TryDequeue(out var request))
            request();

        if (_display is not { } display)
            return;

        try
        {
            AdvanceDisplay(display);
        }
        catch (Exception exception)
        {
            GD.PrintErr($"[sts2agent] Map presentation failed: {exception}");
            display.OpeningBarrier = null;
            display.Outcome = PresentationDrawing.Failed;
            display.Returning = true;
            StopDrawingSafely(display);
            try
            {
                if (!display.AbandonRestore && _gateway.IsPresentationIdentityCurrent(display.Request.OperationId))
                    Finish(display);
                else
                    AbortUncertain(display);
            }
            catch (Exception cleanupError)
            {
                GD.PrintErr($"[sts2agent] Map presentation cleanup failed: {cleanupError}");
                AbortUncertain(display);
            }
        }
    }

    private Task<HttpReply> SubmitAsync(PresentationRequest request)
    {
        var response = new TaskCompletionSource<HttpReply>(TaskCreationOptions.RunContinuationsAsynchronously);
        _requests.Enqueue(() =>
        {
            try
            {
                SurfaceCapture? captured = null;
                var error = _gateway.BeginPresentation(
                    request.OperationId,
                    request.Route,
                    () => CaptureSurface(request, requireSettledScroll: false, out captured));
                if (error is not null)
                {
                    response.SetResult(new HttpReply(200, JsonContract.Serialize(new RejectedStep(request.OperationId, error))));
                    return;
                }

                _capture = captured!;
                _returnNeeded = false;
                var display = _display = new(request, captured!);
                _gateway.TrackPresentationWorker(request.OperationId, display.Worker.Task);
                captured!.Map.MouseFilter = Control.MouseFilterEnum.Ignore;
                response.SetResult(new HttpReply(200, JsonContract.Serialize(new PendingStep(request.OperationId))));

                if (!ViewerPacing.IsInteractiveGui && !captured.WasOpen)
                    return;

                if (captured.WasOpen)
                {
                    if (captured.Animations.Busy() || captured.Pan.Position != captured.ScrollTarget.Current)
                        display.OpeningBarrier = new(true);
                    return;
                }

                try
                {
                    _returnNeeded = display.OpenedMap = true;
                    captured.Map.Open(isOpenedFromTopBar: true);
                    captured.Animations.Busy();
                    display.OpeningBarrier = new(true);
                }
                catch (Exception exception)
                {
                    GD.PrintErr($"[sts2agent] Map presentation open failed: {exception}");
                    display.OpeningBarrier = null;
                    display.Outcome = PresentationDrawing.Failed;
                    display.Returning = true;
                }
            }
            catch (Exception exception)
            {
                response.TrySetException(exception);
            }
        });
        return response.Task;
    }

    private Task<HttpReply> VerifyAsync(PresentationVerificationRequest request)
    {
        var response = new TaskCompletionSource<HttpReply>(TaskCreationOptions.RunContinuationsAsynchronously);
        _requests.Enqueue(() =>
        {
            try
            {
                var error = _gateway.BeginPresentationVerification(request.OperationId, request.Route);
                if (error is not null)
                {
                    response.SetResult(new HttpReply(200, JsonContract.Serialize(new RejectedStep(request.OperationId, error))));
                    return;
                }

                var done = Task.CompletedTask;
                _gateway.TrackPresentationVerifier(request.OperationId, done);
                var result = PresentationRestoration.Unknown;
                try
                {
                    if (IsRestored(_capture))
                        result = RestorationOutcome();
                }
                catch (Exception exception)
                {
                    GD.PrintErr($"[sts2agent] Map restoration verification failed: {exception}");
                }

                _gateway.FinishPresentationVerification(request.OperationId, result);
                var status = _gateway.Presentation!;
                response.SetResult(new HttpReply(200, JsonContract.Serialize(new
                {
                    operation_id = request.OperationId,
                    status = StepOutcome.Completed,
                    restoration = status.Restoration,
                    gate = status.Gate,
                })));
            }
            catch (Exception exception)
            {
                response.TrySetException(exception);
            }
        });
        return response.Task;
    }

    private static StepCause? CaptureSurface(
        PresentationRequest request,
        bool requireSettledScroll,
        out SurfaceCapture? capture)
    {
        capture = null;
        var map = NMapScreen.Instance;
        if (map is null || !GodotObject.IsInstanceValid(map) || !map.IsInsideTree())
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: the native map is unavailable.");
        if (map.Drawings.IsLocalDrawing())
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: another local drawing is active.");
        if (!RunManager.Instance.IsInProgress)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: no run is in progress.");
        if (!RunManager.Instance.ActionQueueSet.IsEmpty)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: native game actions are queued.");
        if (RunManager.Instance.ActionExecutor.CurrentlyRunningAction is not null)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: a native game action is running.");
        if (map.GetTree().Paused)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: the game tree is paused.");
        if (ObservationHub.Current.Reply is not CompletedStateRead current)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: the current public state is unavailable.");

        var points = NodeHierarchy.Descendants(map).OfType<NMapPoint>().ToDictionary(
            point => MapObserver.NodePosition(point.Point.coord),
            StringComparer.Ordinal);
        foreach (var edge in request.Edges)
        {
            if (!points.ContainsKey(edge.From))
                return new(StepErrorCode.ActionUnavailable, $"Map capture failed: source point {edge.From} is unavailable.");
            if (!points.ContainsKey(edge.To))
                return new(StepErrorCode.ActionUnavailable, $"Map capture failed: destination point {edge.To} is unavailable.");
        }

        var overlay = NOverlayStack.Instance?.Peek();
        var controls = (overlay is Node node ? NodeHierarchy.Descendants(node) : NodeHierarchy.Descendants(map.GetTree().Root))
            .OfType<NClickableControl>()
            .Where(control => control.IsVisibleInTree() && control.IsEnabled)
            .ToArray();

        var firstPoint = points[request.Edges[0].From];
        var scroll = NodeHierarchy.Ancestors(firstPoint).OfType<ScrollContainer>().FirstOrDefault();
        var pan = NodeHierarchy.Ancestors(firstPoint).OfType<Control>().FirstOrDefault(control => control.Name == "TheMap");
        if (pan?.GetParent() is not CanvasItem)
            pan = null;
        if (pan is null)
            return new(StepErrorCode.ActionUnavailable, $"Map capture failed: source point {request.Edges[0].From} has no supported pan container.");

        var animations = NativeMapAnimations.Capture(map);
        if (animations is null)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: native animation state could not be bound.");

        if (map.IsOpen && animations.IsDragging)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: native map dragging is active.");
        if (!map.IsOpen && (map.Visible || map.ProcessMode != Node.ProcessModeEnum.Disabled))
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: the closed map is still visible or processing.");

        var scrollTarget = NativeMapScrollTarget.Capture(map, pan);
        if (scrollTarget is null)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: the native scroll target could not be bound.");
        if (requireSettledScroll && map.IsOpen && pan.Position != scrollTarget.Original)
            return new(StepErrorCode.ActionUnavailable, "Map capture failed: the map viewport has not reached its native scroll target.");

        var merchant = NMerchantRoom.Instance;
        var (lineCount, pointCount) = InkSize(map);
        capture = new(
            map,
            map.IsOpen,
            overlay,
            CardSelectionInterceptor.GetPending(),
            merchant,
            merchant?.Inventory.IsOpen,
            controls,
            scroll,
            scroll?.ScrollHorizontal ?? 0,
            scroll?.ScrollVertical ?? 0,
            pan,
            map.IsOpen ? scrollTarget.Original : pan.Position,
            scrollTarget,
            animations,
            points,
            JsonContract.Serialize(current.Observation.State),
            map.MouseFilter,
            lineCount,
            PointCount: pointCount,
            current.Observation.InputReadiness,
            map.GetViewport().GuiGetFocusOwner());
        return null;
    }

    private void AdvanceDisplay(ActiveDisplay display)
    {
        if (display.AbandonRestore
            || !_gateway.IsPresentationIdentityCurrent(display.Request.OperationId)
            || !HasMapSurface(display.Capture))
        {
            AbortUncertain(display);
            return;
        }

        var capture = display.Capture;
        if (display.OpeningBarrier is { } barrier)
        {
            var settling = capture.Animations.Busy()
                           || (capture.Map.IsOpen && capture.Pan.Position != capture.ScrollTarget.Current);
            if (!barrier.Advance(settling))
                return;

            display.OpeningBarrier = null;
            if (barrier.TimedOut)
            {
                AbortUncertain(display);
                return;
            }
            if (capture.WasOpen)
            {
                var error = CaptureSurface(display.Request, requireSettledScroll: true, out var settled);
                if (error is not null
                    || settled is null
                    || !ReferenceEquals(settled.Map, capture.Map)
                    || !settled.WasOpen
                    || settled.SerializedState != capture.SerializedState)
                {
                    GD.PrintErr($"[sts2agent] Map opening recapture failed: {error?.Message ?? "The captured map surface changed."}");
                    AbortUncertain(display);
                    return;
                }
                display.Capture = capture = settled with { MouseFilter = capture.MouseFilter, Focus = capture.Focus };
                _capture = capture;
            }
        }
        if (capture.Animations.Busy())
            return;
        if (display.Returning)
        {
            Finish(display);
            return;
        }
        if (!ViewerPacing.IsInteractiveGui)
        {
            DrawAllEdgesInstantly(display, capture);
            Finish(display);
            return;
        }

        if (!capture.Map.IsOpen)
        {
            display.Outcome = PresentationDrawing.Failed;
            display.Returning = true;
            Finish(display);
            return;
        }

        if (display.Edge >= display.Request.Edges.Count)
        {
            RemoveQuillVisual(display);
            if (display.HoldUntilMsec != 0 && Time.GetTicksMsec() < display.HoldUntilMsec)
                return;

            VerifyInkCompleteness(display, capture);
            display.Returning = true;
            Finish(display);
            return;
        }

        var edge = display.Request.Edges[display.Edge];
        var from = capture.Points[edge.From];
        var to = capture.Points[edge.To];

        if (!display.Drawing)
        {
            var startScreen = ScreenEndpoint(from);
            var onScreen = capture.Map.IsNodeOnScreen(from);
            var centeredEnough = MathF.Abs(startScreen.Y - VisibleCenterY(capture)) <= 56f;
            if (!onScreen || (display.Edge == 0 && !centeredEnough && display.FollowFrame < 20))
            {
                var moved = FollowVertical(capture, startScreen);
                _returnNeeded |= moved;
                display.FollowFrame++;
                if (!onScreen)
                {
                    if (display.FollowFrame > 120)
                    {
                        display.Outcome = PresentationDrawing.Failed;
                        display.Returning = true;
                    }
                    return;
                }
                if (moved)
                    return;
            }
        }

        var start = ScreenEndpoint(from);
        var end = ScreenEndpoint(to);
        var target = start.Lerp(end, display.Progress);
        _returnNeeded |= FollowVertical(capture, target);

        // Recompute canvas coordinates after vertical map pan so the stroke and quill align with the nodes.
        start = ScreenEndpoint(from);
        end = ScreenEndpoint(to);
        target = start.Lerp(end, display.Progress);
        UpdateQuillVisual(display, target);

        var ink = ToDrawingsLocal(capture.Map, target);
        if (!display.Drawing)
        {
            capture.Map.Drawings.BeginLineLocal(ink, DrawingMode.Drawing);
            display.Drawing = true;
        }
        else
        {
            capture.Map.Drawings.UpdateCurrentLinePositionLocal(ink);
        }

        if (display.Progress < 1f)
        {
            var delta = (float)Math.Clamp(capture.Map.GetProcessDeltaTime(), 1d / 240d, 0.05d);
            var step = Math.Clamp(920f * delta / Math.Max(1f, start.DistanceTo(end)), 0.02f, 0.25f);
            display.Progress = Math.Min(1f, display.Progress + step);
            return;
        }

        if (!capture.Map.IsNodeOnScreen(to))
        {
            if (++display.TargetFollowFrame <= 90)
                return;
            display.Outcome = PresentationDrawing.Failed;
            display.Returning = true;
        }

        display.Drawing = false;
        capture.Map.Drawings.StopLineLocal();
        display.Edge++;
        display.Progress = 0;
        display.FollowFrame = 0;
        display.TargetFollowFrame = 0;
        if (display.Edge >= display.Request.Edges.Count)
        {
            RemoveQuillVisual(display);
            display.HoldUntilMsec = Time.GetTicksMsec() + FinishedRouteHoldMs;
        }
    }

    private static void DrawAllEdgesInstantly(ActiveDisplay display, SurfaceCapture capture)
    {
        while (display.Edge < display.Request.Edges.Count)
        {
            var edge = display.Request.Edges[display.Edge];
            var from = capture.Points[edge.From];
            var to = capture.Points[edge.To];
            var fromInk = ToDrawingsLocal(capture.Map, ScreenEndpoint(from));
            var toInk = ToDrawingsLocal(capture.Map, ScreenEndpoint(to));
            capture.Map.Drawings.BeginLineLocal(fromInk, DrawingMode.Drawing);
            capture.Map.Drawings.UpdateCurrentLinePositionLocal(toInk);
            capture.Map.Drawings.StopLineLocal();
            display.Edge++;
        }

        VerifyInkCompleteness(display, capture);
        display.Returning = true;
    }

    private static void VerifyInkCompleteness(ActiveDisplay display, SurfaceCapture capture)
    {
        var (lines, points) = InkSize(capture.Map);
        if (lines < capture.LineCount + display.Request.Edges.Count
            || points < capture.PointCount + 2 * display.Request.Edges.Count)
        {
            display.Outcome = PresentationDrawing.Failed;
        }
    }

    private void Finish(ActiveDisplay display)
    {
        RemoveQuillVisual(display);
        var capture = display.Capture;
        if (!HasMapSurface(capture))
        {
            AbortUncertain(display);
            return;
        }
        if (capture.Animations.Busy())
            return;

        if (display.Drawing)
        {
            display.Drawing = false;
            capture.Map.Drawings.StopLineLocal();
        }

        if (!ViewerPacing.IsInteractiveGui)
        {
            if (capture.Scroll is { } instantScroll)
            {
                instantScroll.ScrollHorizontal = capture.ScrollX;
                instantScroll.ScrollVertical = capture.ScrollY;
            }
            if (capture.Pan.Position != capture.PanPosition)
            {
                capture.ScrollTarget.Set(capture.PanPosition);
                capture.Pan.Position = capture.PanPosition;
            }
        }
        else
        {
            if (((capture.Scroll is { } movingScroll
                  && (movingScroll.ScrollHorizontal != capture.ScrollX || movingScroll.ScrollVertical != capture.ScrollY))
                 || capture.Pan.Position != capture.PanPosition)
                && ++display.RestoreFrame > 120)
            {
                RestoreInputAndFocus(capture);
                display.Worker.TrySetResult();
                _gateway.SettlePresentation(display.Request.OperationId, display.Outcome, PresentationRestoration.Unknown);
                _display = null;
                return;
            }

            if (capture.Scroll is { } scroll
                && (scroll.ScrollHorizontal != capture.ScrollX || scroll.ScrollVertical != capture.ScrollY))
            {
                scroll.ScrollHorizontal = Approach(scroll.ScrollHorizontal, capture.ScrollX);
                scroll.ScrollVertical = Approach(scroll.ScrollVertical, capture.ScrollY);
                return;
            }

            if (capture.Pan.Position != capture.PanPosition)
            {
                var distance = capture.Pan.Position.DistanceTo(capture.PanPosition);
                var delta = (float)Math.Clamp(capture.Map.GetProcessDeltaTime(), 1d / 240d, 0.05d);
                var step = distance <= 2f
                    ? distance
                    : Math.Max(18f, distance * Math.Clamp(14f * delta, 0.12f, 0.45f));
                var next = capture.Pan.Position.MoveToward(capture.PanPosition, step);
                capture.ScrollTarget.Set(next);
                capture.Pan.Position = next;
                return;
            }
        }

        if (!capture.WasOpen && !display.Closed)
        {
            if (capture.Map.IsOpen)
                capture.Map.Close(animateOut: false);
            capture.ScrollTarget.Set(capture.ScrollTarget.Original);
            display.Closed = true;
            if (ViewerPacing.IsInteractiveGui)
                return;
        }

        if (capture.WasOpen)
            capture.ScrollTarget.Set(capture.ScrollTarget.Original);

        RestoreInputAndFocus(capture);
        var restoration = IsRestored(capture)
            ? RestorationOutcome()
            : PresentationRestoration.Unknown;
        display.Worker.TrySetResult();
        _gateway.SettlePresentation(display.Request.OperationId, display.Outcome, restoration);
        _display = null;
    }

    private static void RestoreInputAndFocus(SurfaceCapture capture)
    {
        if (!GodotObject.IsInstanceValid(capture.Map) || !capture.Map.IsInsideTree())
            return;

        capture.Map.MouseFilter = capture.MouseFilter;
        var currentFocus = capture.Map.GetViewport().GuiGetFocusOwner();
        if (ReferenceEquals(currentFocus, capture.Focus))
            return;

        if (capture.Focus is { } focus
            && GodotObject.IsInstanceValid(focus)
            && focus.IsInsideTree()
            && focus.FocusMode != Control.FocusModeEnum.None)
        {
            focus.GrabFocus();
        }
        else if (capture.Focus is null)
        {
            capture.Map.GetViewport().GuiReleaseFocus();
        }
    }

    private static void UpdateQuillVisual(ActiveDisplay display, Vector2 screenTarget)
    {
        var map = display.Capture.Map;
        var quill = display.Quill;
        if (quill is null || !GodotObject.IsInstanceValid(quill) || quill.GetParent() != map)
        {
            var texture = LoadQuillTexture();
            if (texture is null)
                return;

            if (map.GetNodeOrNull<Node>(QuillNodeName) is { } existing)
            {
                map.RemoveChild(existing);
                existing.QueueFree();
            }
            quill = new TextureRect
            {
                Name = QuillNodeName,
                Texture = texture,
                MouseFilter = Control.MouseFilterEnum.Ignore,
                ZIndex = 60,
            };
            map.AddChild(quill);
            display.Quill = quill;
        }

        var localTarget = map.GetGlobalTransformWithCanvas().AffineInverse() * screenTarget;
        quill.Position = localTarget - NMapDrawings.drawingCursorHotspot;
        quill.Visible = true;
    }

    private static void RemoveQuillVisual(ActiveDisplay display)
    {
        if (display.Quill is { } quill)
        {
            display.Quill = null;
            if (GodotObject.IsInstanceValid(quill))
            {
                quill.GetParent()?.RemoveChild(quill);
                quill.QueueFree();
            }
        }

        if (GodotObject.IsInstanceValid(display.Capture.Map)
            && display.Capture.Map.GetNodeOrNull<Control>(QuillNodeName) is { } leftover)
        {
            leftover.GetParent()?.RemoveChild(leftover);
            leftover.QueueFree();
        }
    }

    private static ImageTexture? LoadQuillTexture()
    {
        if (_quillLoadAttempted)
            return _cachedQuillTexture;

        _quillLoadAttempted = true;
        try
        {
            var image = PreloadManager.Cache.GetAsset<Image>(NMapDrawings.drawingCursorTiltedPath);
            if (image is not null)
                _cachedQuillTexture = ImageTexture.CreateFromImage(image);
        }
        catch
        {
            _cachedQuillTexture = null;
        }
        return _cachedQuillTexture;
    }

    private static bool IsRestored(SurfaceCapture? capture) =>
        capture is not null
        && HasMapSurface(capture)
        && !capture.Animations.Busy()
        && !capture.Map.Drawings.IsLocalDrawing()
        && capture.Map.IsOpen == capture.WasOpen
        && capture.Map.MouseFilter == capture.MouseFilter
        && ReferenceEquals(NOverlayStack.Instance?.Peek(), capture.Overlay)
        && (capture.WasOpen || (!capture.Map.Visible && capture.Map.ProcessMode == Node.ProcessModeEnum.Disabled))
        && ReferenceEquals(CardSelectionInterceptor.GetPending(), capture.Choice)
        && ReferenceEquals(NMerchantRoom.Instance, capture.Merchant)
        && capture.Merchant?.Inventory.IsOpen == capture.ShopOpen
        && (capture.Scroll is null
            || (GodotObject.IsInstanceValid(capture.Scroll)
                && capture.Scroll.ScrollHorizontal == capture.ScrollX
                && capture.Scroll.ScrollVertical == capture.ScrollY))
        && capture.Pan.Position == capture.PanPosition
        && capture.ScrollTarget.Current == capture.ScrollTarget.Original
        && capture.Controls.All(control =>
            GodotObject.IsInstanceValid(control)
            && control.IsInsideTree()
            && control.IsVisibleInTree()
            && control.IsEnabled)
        && ObservationHub.Current.Reply is CompletedStateRead current
        && JsonContract.Serialize(current.Observation.State) == capture.SerializedState
        && current.Observation.InputReadiness == capture.Readiness
        && ReferenceEquals(capture.Map.GetViewport().GuiGetFocusOwner(), capture.Focus)
        && RunManager.Instance.IsInProgress
        && RunManager.Instance.ActionQueueSet.IsEmpty
        && RunManager.Instance.ActionExecutor.CurrentlyRunningAction is null
        && !capture.Map.GetTree().Paused;

    private static bool HasMapSurface(SurfaceCapture capture) =>
        GodotObject.IsInstanceValid(capture.Map)
        && capture.Map.IsInsideTree()
        && ReferenceEquals(NMapScreen.Instance, capture.Map)
        && GodotObject.IsInstanceValid(capture.Pan)
        && capture.Pan.IsInsideTree()
        && capture.ScrollTarget.IsBound;

    private PresentationRestoration RestorationOutcome() =>
        _returnNeeded ? PresentationRestoration.Restored : PresentationRestoration.NotNeeded;

    private void AbortUncertain(ActiveDisplay display)
    {
        var capture = display.Capture;
        if (!display.AbandonRestore)
        {
            display.AbandonRestore = true;
            display.OpeningBarrier = null;
            StopDrawingSafely(display);
            try
            {
                RemoveQuillVisual(display);
            }
            finally
            {
                if (GodotObject.IsInstanceValid(capture.Map) && capture.Map.IsInsideTree())
                    capture.Map.MouseFilter = capture.MouseFilter;
            }
        }
        // A native animation started by this display still owns its work until it stops.
        if (display.OpenedMap && GodotObject.IsInstanceValid(capture.Map) && capture.Map.IsInsideTree()
            && capture.Animations.Busy())
        {
            return;
        }

        display.Worker.TrySetResult();
        _gateway.SettlePresentation(
            display.Request.OperationId,
            PresentationDrawing.Failed,
            PresentationRestoration.Unknown);
        _display = null;
    }

    private static void StopDrawingSafely(ActiveDisplay display)
    {
        try
        {
            if (display.Drawing)
                display.Capture.Map.Drawings.StopLineLocal();
        }
        catch (Exception stopError)
        {
            GD.PrintErr($"[sts2agent] Map pen stop failed: {stopError}");
        }
        display.Drawing = false;
    }

    private static int Approach(int value, int target) =>
        Math.Abs(value - target) <= 3
            ? target
            : value + (int)Math.CopySign(Math.Max(1, Math.Abs(value - target) / 4), target - value);

    private static (int Lines, int Points) InkSize(NMapScreen map)
    {
        var lines = map.Drawings.GetSerializableMapDrawings().drawings.SelectMany(player => player.lines).ToArray();
        return (lines.Length, lines.Sum(line => line.mapPoints.Count));
    }

    private static Vector2 ScreenEndpoint(NMapPoint point)
    {
        var anchor = point.PivotOffset != Vector2.Zero ? point.PivotOffset : point.Size * 0.5f;
        return point.GetGlobalTransformWithCanvas() * anchor;
    }

    private static Vector2 ToDrawingsLocal(NMapScreen map, Vector2 screenPoint) =>
        map.Drawings.GetGlobalTransformWithCanvas().AffineInverse() * screenPoint;

    private static float VisibleCenterY(SurfaceCapture capture) =>
        capture.Map.GetViewport().GetVisibleRect().GetCenter().Y;

    private static bool FollowVertical(SurfaceCapture capture, Vector2 target)
    {
        var delta = (float)Math.Clamp(capture.Map.GetProcessDeltaTime(), 1d / 240d, 0.05d);
        var factor = 1f - MathF.Exp(-14f * delta);

        if (capture.Scroll is { } scroll)
        {
            var previousScroll = scroll.ScrollVertical;
            var offsetY = target.Y - scroll.GetGlobalRect().GetCenter().Y;
            scroll.ScrollVertical += (int)MathF.Round(offsetY * factor);
            return scroll.ScrollVertical != previousScroll;
        }

        var pan = capture.Pan;
        var previousPos = pan.Position;
        var parent = (CanvasItem)pan.GetParent();
        var verticalOffset = new Vector2(0f, VisibleCenterY(capture) - target.Y);
        var next = pan.Position + parent.GetGlobalTransformWithCanvas().AffineInverse().BasisXform(verticalOffset * factor);
        next.Y = Math.Clamp(next.Y, NativeScrollLimitBottom, NativeScrollLimitTop);
        capture.ScrollTarget.Set(next);
        pan.Position = next;
        return next != previousPos;
    }
}
