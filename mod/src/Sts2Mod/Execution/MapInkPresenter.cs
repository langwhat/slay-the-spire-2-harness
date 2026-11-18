using System.Collections.Concurrent;
using System.Reflection;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Observation;

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
/// </summary>
internal sealed class MapInkPresenter
{
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
        public MapOpeningBarrier? OpeningBarrier;
        public bool Drawing;
        public bool Returning;
        public bool Closed;
        public bool IdentityLost;
        public PresentationDrawing Outcome = PresentationDrawing.Shown;
    }

    private readonly StepGateway _gateway;
    private readonly ConcurrentQueue<Action> _requests = new();
    private ActiveDisplay? _display;
    private SurfaceCapture? _capture;
    private bool _restorationUncertain;

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
            _restorationUncertain = true;
            display.OpeningBarrier = null;
            display.Outcome = PresentationDrawing.Failed;
            display.Returning = true;
            StopDrawingSafely(display);
            try
            {
                Finish(display);
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
                _restorationUncertain = false;
                var display = _display = new(request, captured!);
                _gateway.TrackPresentationWorker(request.OperationId, display.Worker.Task);
                response.SetResult(new HttpReply(200, JsonContract.Serialize(new PendingStep(request.OperationId))));

                if (captured!.WasOpen)
                {
                    if (captured.Animations.Busy() || captured.Pan.Position != captured.ScrollTarget.Current)
                        display.OpeningBarrier = new(true);
                    return;
                }

                try
                {
                    captured.Map.Open();
                    captured.Animations.Busy();
                    display.OpeningBarrier = new(true);
                }
                catch (Exception exception)
                {
                    GD.PrintErr($"[sts2agent] Map presentation open failed: {exception}");
                    _restorationUncertain = true;
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
                    if (!_restorationUncertain && IsRestored(_capture))
                        result = RestorationOutcome(_capture!);
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
            pan.Position,
            scrollTarget,
            animations,
            points,
            JsonContract.Serialize(current.Observation.State),
            lineCount,
            PointCount: pointCount,
            current.Observation.InputReadiness,
            map.GetViewport().GuiGetFocusOwner());
        return null;
    }

    private void AdvanceDisplay(ActiveDisplay display)
    {
        if (display.IdentityLost)
            return;

        if (!_gateway.IsPresentationIdentityCurrent(display.Request.OperationId))
        {
            _restorationUncertain = true;
            display.IdentityLost = true;
            return;
        }

        if (display.OpeningBarrier is { } barrier)
        {
            var settling = AnimationsBusy(display)
                           || (display.Capture.Map.IsOpen
                               && display.Capture.ScrollTarget.IsBound
                               && display.Capture.Pan.Position != display.Capture.ScrollTarget.Current);
            if (!barrier.Advance(settling))
                return;

            display.OpeningBarrier = null;
            if (display.Capture.WasOpen)
            {
                var error = CaptureSurface(display.Request, requireSettledScroll: true, out var settled);
                if (error is not null
                    || settled is null
                    || !ReferenceEquals(settled.Map, display.Capture.Map)
                    || !settled.WasOpen
                    || settled.SerializedState != display.Capture.SerializedState)
                {
                    GD.PrintErr($"[sts2agent] Map opening recapture failed: {error?.Message ?? "The captured map surface changed."}");
                    _restorationUncertain = true;
                    display.Worker.TrySetResult();
                    _gateway.SettlePresentation(
                        display.Request.OperationId,
                        PresentationDrawing.Failed,
                        PresentationRestoration.Unknown);
                    _display = null;
                    return;
                }
                display.Capture = settled;
                _capture = settled;
            }
            if (barrier.TimedOut)
            {
                display.Outcome = PresentationDrawing.Failed;
                display.Returning = true;
            }
        }

        var capture = display.Capture;
        if (!capture.ScrollTarget.IsBound)
        {
            _restorationUncertain = true;
            display.IdentityLost = true;
            return;
        }
        if (AnimationsBusy(display))
            return;
        if (display.Returning)
        {
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
            var (lines, points) = InkSize(capture.Map);
            if (lines < capture.LineCount + display.Request.Edges.Count
                || points < capture.PointCount + 2 * display.Request.Edges.Count)
            {
                display.Outcome = PresentationDrawing.Failed;
            }
            display.Returning = true;
            Finish(display);
            return;
        }

        var edge = display.Request.Edges[display.Edge];
        var from = capture.Points[edge.From];
        var to = capture.Points[edge.To];

        if (!display.Drawing && !capture.Map.IsNodeOnScreen(from))
        {
            Follow(capture, ScreenCenter(from));
            if (++display.FollowFrame > 120)
            {
                display.Outcome = PresentationDrawing.Failed;
                display.Returning = true;
            }
            return;
        }

        var start = ScreenCenter(from);
        var end = ScreenCenter(to);
        var position = start.Lerp(end, display.Progress);
        if (!display.Drawing)
        {
            capture.Map.Drawings.BeginLineLocal(position, DrawingMode.Drawing);
            display.Drawing = true;
        }
        else
        {
            capture.Map.Drawings.UpdateCurrentLinePositionLocal(position);
        }

        if (!capture.Map.IsNodeOnScreen(to))
            Follow(capture, position);

        if (display.Progress < 1f)
        {
            display.Progress = Math.Min(1f, display.Progress + 12f / Math.Max(1f, start.DistanceTo(end)));
            return;
        }

        if (!capture.Map.IsNodeOnScreen(to))
        {
            if (++display.TargetFollowFrame <= 120)
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
    }

    private void Finish(ActiveDisplay display)
    {
        var capture = display.Capture;
        if (!capture.ScrollTarget.IsBound)
        {
            _restorationUncertain = true;
            display.IdentityLost = true;
            return;
        }
        if (AnimationsBusy(display))
            return;

        if (display.Drawing)
        {
            display.Drawing = false;
            capture.Map.Drawings.StopLineLocal();
        }

        if (((capture.Scroll is { } movingScroll
              && (movingScroll.ScrollHorizontal != capture.ScrollX || movingScroll.ScrollVertical != capture.ScrollY))
             || capture.Pan.Position != capture.PanPosition)
            && ++display.RestoreFrame > 120)
        {
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
            var next = capture.Pan.Position.MoveToward(capture.PanPosition, Math.Max(12f, distance / 8f));
            capture.ScrollTarget.Set(next);
            capture.Pan.Position = next;
            return;
        }

        if (!capture.WasOpen && !display.Closed)
        {
            if (capture.Map.IsOpen)
                capture.Map.Close(animateOut: false);
            capture.ScrollTarget.Set(capture.ScrollTarget.Original);
            display.Closed = true;
            return;
        }

        if (capture.WasOpen)
            capture.ScrollTarget.Set(capture.ScrollTarget.Original);

        display.Worker.TrySetResult();
        var restoration = !_restorationUncertain && IsRestored(capture)
            ? RestorationOutcome(capture)
            : PresentationRestoration.Unknown;
        _gateway.SettlePresentation(display.Request.OperationId, display.Outcome, restoration);
        _display = null;
    }

    private static bool IsRestored(SurfaceCapture? capture) =>
        capture is not null
        && GodotObject.IsInstanceValid(capture.Map)
        && capture.Map.IsInsideTree()
        && !capture.Animations.Busy()
        && !capture.Map.Drawings.IsLocalDrawing()
        && capture.Map.IsOpen == capture.WasOpen
        && ReferenceEquals(NOverlayStack.Instance?.Peek(), capture.Overlay)
        && (capture.WasOpen || (!capture.Map.Visible && capture.Map.ProcessMode == Node.ProcessModeEnum.Disabled))
        && ReferenceEquals(CardSelectionInterceptor.GetPending(), capture.Choice)
        && ReferenceEquals(NMerchantRoom.Instance, capture.Merchant)
        && capture.Merchant?.Inventory.IsOpen == capture.ShopOpen
        && (capture.Scroll is null
            || (GodotObject.IsInstanceValid(capture.Scroll)
                && capture.Scroll.ScrollHorizontal == capture.ScrollX
                && capture.Scroll.ScrollVertical == capture.ScrollY))
        && GodotObject.IsInstanceValid(capture.Pan)
        && capture.Pan.Position == capture.PanPosition
        && capture.ScrollTarget.IsBound
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

    private static PresentationRestoration RestorationOutcome(SurfaceCapture capture) =>
        capture.WasOpen ? PresentationRestoration.NotNeeded : PresentationRestoration.Restored;

    private bool AnimationsBusy(ActiveDisplay display)
    {
        try
        {
            return display.Capture.Animations.Busy();
        }
        catch (Exception exception)
        {
            GD.PrintErr($"[sts2agent] Map animation state unavailable: {exception}");
            AbortUncertain(display);
            return true;
        }
    }

    private void AbortUncertain(ActiveDisplay display)
    {
        _restorationUncertain = true;
        display.OpeningBarrier = null;
        StopDrawingSafely(display);
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

    private static Vector2 ScreenCenter(Control control) =>
        control.GetGlobalTransformWithCanvas() * (control.Size / 2);

    private static void Follow(SurfaceCapture capture, Vector2 target)
    {
        if (capture.Scroll is { } scroll)
        {
            var offset = target - scroll.GetGlobalRect().GetCenter();
            scroll.ScrollHorizontal += (int)(offset.X * 0.15f);
            scroll.ScrollVertical += (int)(offset.Y * 0.15f);
            return;
        }

        var pan = capture.Pan;
        var parent = (CanvasItem)pan.GetParent();
        var offsetToCenter = capture.Map.GetViewport().GetVisibleRect().GetCenter() - target;
        var next = pan.Position + parent.GetGlobalTransformWithCanvas().AffineInverse().BasisXform(offsetToCenter * 0.15f);
        capture.ScrollTarget.Set(next);
        pan.Position = next;
    }
}
