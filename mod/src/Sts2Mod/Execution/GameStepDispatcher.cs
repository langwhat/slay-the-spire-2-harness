using System.Collections.Immutable;
using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.Events.Custom;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.RestSite;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Control;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Execution;
using Sts2Mod.Core.Identity;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Observation;
using Sts2Mod.Ui;

namespace Sts2Mod.Execution;

/// <summary>
/// Resolves registered public identities and dispatches single native gameplay steps on the game thread.
/// </summary>
internal sealed class GameStepDispatcher
{
    private static readonly BindingFlags PrivateInstance = BindingFlags.Instance | BindingFlags.NonPublic;

    private readonly AgentControlState _control;
    private readonly StepGateway _gateway;
    private readonly MapInkPresenter _mapPresenter;
    private Task? _visualPresentation;
    private object? _submittedChoice;
    private Node? _confirmationScreen;
    private ulong _confirmationNotBeforeMsec;
    private readonly HashSet<NConfirmButton> _pressedConfirmations = new(ReferenceEqualityComparer.Instance);
    private bool _touched;
    private StepRequest? _selectionOrigin;
    private AbstractModel? _selectionSource;
    private int? _selectionTurn;
    private CardModel[]? _autoSelectedCards;
    private bool _multipleAutoSelections;

    public GameStepDispatcher(AgentControlState control, Func<bool>? externalInputBusy = null)
    {
        _control = control;
        _gateway = new StepGateway(control, ObservationHub.GameInstanceId);
        if (externalInputBusy is not null)
            _gateway.SetExternalInputBusyCheck(externalInputBusy);
        _mapPresenter = new MapInkPresenter(_gateway);
    }

    public MapPresentationTransport MapPresentation => _mapPresenter.Transport;

    public NativeReadPublication Project(NativeReadPublication publication) => _gateway.Project(publication);

    public HttpReply Submit(string body) => _gateway.Submit(body);

    public NativeStepStatus CurrentStatus() => _gateway.Status();

    public HttpReply StatusJson() =>
        new(200, JsonContract.Serialize(_gateway.Status()));

    public void ClearOnRunReset() => _gateway.ClearReceiptOnRunReset();

    public void Advance(long runInstanceId, MainMenuReadinessObservation menuReadiness)
    {
        var (location, readiness, sequence) = CurrentBoundary(runInstanceId, menuReadiness);

        if (_selectionOrigin is { } origin
            && (location != new StepLocation(
                    origin.ExpectedRoute.GameInstanceId,
                    origin.ExpectedRoute.RunId,
                    origin.ExpectedRoute.NodeId,
                    origin.ExpectedRoute.Owner)
                || RunManager.Instance.DebugOnlyGetState()?.Players.Single().PlayerCombatState?.TurnNumber != _selectionTurn))
        {
            ClearAutoSelection();
        }

        if (_confirmationScreen is { } screen)
        {
            if (!ReferenceEquals(NOverlayStack.Instance?.Peek(), screen))
            {
                _confirmationScreen = null;
            }
            else
            {
                try
                {
                    if (AdvanceConfirmation(screen))
                        readiness = NativeInputReadiness.Busy;
                }
                catch (Exception error)
                {
                    GD.PrintErr($"[sts2agent] Selection confirmation continuation failed: {error}");
                    _confirmationScreen = null;
                    _submittedChoice = null;
                    _gateway.ReportUncertain();
                }
            }
        }

        if (_submittedChoice is not null && ReferenceEquals(_submittedChoice, CurrentChoice()))
            readiness = NativeInputReadiness.Busy;
        else
            _submittedChoice = null;

        if (_visualPresentation is { IsCompleted: false })
            readiness = NativeInputReadiness.Busy;

        _gateway.Advance(
            location,
            readiness,
            sequence,
            Dispatch,
            RunManager.Instance.DebugOnlyGetState()?.AscensionLevel);
        _mapPresenter.Advance();

        if (_selectionOrigin is { } selectionOrigin
            && _gateway.Status().Receipt is { } receipt
            && receipt.OperationId == selectionOrigin.OperationId
            && receipt is not PendingStep)
        {
            AutoResolvedCardSelectionPatch.Resolved = null;
            if (receipt is not CompletedStep)
                ClearAutoSelection();
        }
    }

    private (StepLocation Location, NativeInputReadiness Readiness, long Sequence) CurrentBoundary(
        long runInstanceId,
        MainMenuReadinessObservation menuReadiness)
    {
        if (ObservationHub.Current.Reply is CompletedStateRead completed)
        {
            var observation = completed.Observation;
            var route = observation.Location;
            return (
                new StepLocation(route.GameInstanceId, route.RunId, route.NodeId, route.Owner),
                observation.InputReadiness,
                observation.Stamp.ObservationSequence);
        }

        if (RunManager.Instance.IsInProgress && !RunManager.Instance.IsCleaningUp && ObservationHub.Identities?.Registry is { } registry)
        {
            var location = new StepLocation(
                ObservationHub.GameInstanceId,
                $"run-{runInstanceId}",
                registry.NodeId,
                CombatManager.Instance.IsInProgress ? ObservationOwner.Combat : ObservationOwner.Run);
            return (location, NativeInputReadiness.Unavailable, -1);
        }

        var outsideRunReadiness = MainMenuReadinessRules.IsMenuSettled(menuReadiness)
            ? NativeInputReadiness.Unavailable
            : NativeInputReadiness.Busy;
        return (_gateway.AbsentLocation, outsideRunReadiness, -1);
    }

    private static object? CurrentChoice() =>
        (object?)CardSelectionInterceptor.GetPending() ?? NOverlayStack.Instance?.Peek();

    private StepReceipt? Dispatch(StepRequest request)
    {
        if (request.Step is not SelectStep)
            ClearAutoSelection();

        var result = DispatchNative(request);
        if (request.Step is SelectStep || result is not null)
            ClearAutoSelection();

        if (result is not null)
        {
            _submittedChoice = null;
            _confirmationScreen = null;
        }
        return result;
    }

    private StepReceipt? DispatchNative(StepRequest request)
    {
        _touched = false;
        try
        {
            var registry = ObservationHub.Identities?.Registry ?? throw new IdentityUnregisteredException();
            var run = RunManager.Instance.DebugOnlyGetState();
            if (run is null)
                return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "No native run is available.");

            var player = run.Players.Single();
            var combat = CombatManager.Instance.IsInProgress ? CombatManager.Instance.DebugOnlyGetState() : null;
            var step = request.Step;
            var hasBlockingSelection = CardSelectionInterceptor.GetPending() is not null
                                       || (combat is null && ObservationHub.Current.Reply is CompletedStateRead currentState
                                           && currentState.Observation.State.Selection is not null);

            if (step.Kind is NativeStepKind.PlayCard or NativeStepKind.EndTurn
                && request.ExpectedRoute.Owner != ObservationOwner.Combat)
            {
                return StepGateway.Reject(request, StepErrorCode.WrongOwner, "This step requires Combat ownership.");
            }

            switch (step)
            {
                case PlayCardStep play:
                    if (!registry.TryResolve<CardModel>(play.Card, out var card))
                        return InvalidRefs(request, [play.Card]);
                    if (!CombatInputProbe.IsCombatInputReady(player, combat)
                        || player.PlayerCombatState is not { } pcs
                        || !pcs.Hand.Cards.Any(current => ReferenceEquals(current, card))
                        || !card!.CanPlay())
                    {
                        return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "This card cannot be played now.");
                    }

                    var cardTarget = BindTarget(
                        request,
                        play.Target,
                        registry,
                        card.TargetType,
                        combat,
                        card.IsValidTarget,
                        out var targetError);
                    if (targetError is not null)
                        return targetError;
                    if (!card.CanPlayTargeting(cardTarget))
                        return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The card cannot be played against this target now.");

                    _touched = true;
                    TrackAutoSelection(request, card, player.PlayerCombatState?.TurnNumber);
                    if (!card.TryManualPlay(cardTarget))
                        return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native card play was not accepted.");
                    return null;

                case UsePotionStep or DiscardPotionStep:
                    var potionRef = step is UsePotionStep use ? use.Potion : ((DiscardPotionStep)step).Potion;
                    if (!registry.TryResolve<PotionModel>(potionRef, out var potion))
                        return InvalidRefs(request, [potionRef]);

                    var slot = player.PotionSlots.ToList().FindIndex(current => ReferenceEquals(current, potion));
                    if (slot < 0)
                        return InvalidRefs(request, [potionRef]);

                    if (step is DiscardPotionStep)
                    {
                        if (!CombatInputProbe.IsPotionDiscardable(potion!, player, combat, hasBlockingSelection))
                            return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "This potion cannot be discarded now.");
                        _touched = true;
                        RunManager.Instance.ActionQueueSynchronizer.RequestEnqueue(
                            new DiscardPotionGameAction(player, (uint)slot, combat is not null));
                        return null;
                    }

                    if (!CombatInputProbe.IsPotionUsable(potion!, player, combat, hasBlockingSelection))
                        return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "This potion cannot be used now.");

                    var potionTarget = BindTarget(
                        request,
                        ((UsePotionStep)step).Target,
                        registry,
                        potion!.TargetType,
                        combat,
                        potion.IsValidTarget,
                        out var potionError);
                    if (potionError is not null)
                        return potionError;

                    _touched = true;
                    TrackAutoSelection(request, potion, player.PlayerCombatState?.TurnNumber);
                    potion.EnqueueManualUse(potionTarget);
                    return null;

                case EndTurnStep:
                    if (!CombatInputProbe.IsCombatInputReady(player, combat)
                        || player.PlayerCombatState is not { } turn
                        || CombatManager.Instance.IsPlayerReadyToEndTurn(player))
                    {
                        return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native turn cannot end now.");
                    }
                    _touched = true;
                    RunManager.Instance.ActionQueueSynchronizer.RequestEnqueue(
                        new EndPlayerTurnAction(player, turn.TurnNumber));
                    return null;

                case InteractStep interaction:
                    return DispatchInteract(request, interaction, registry);

                case SelectStep selection:
                    return DispatchSelect(request, selection, registry);

                default:
                    throw new InvalidOperationException("Unsupported step kind.");
            }
        }
        catch (IdentityUnregisteredException)
        {
            return _touched
                ? Uncertain(request)
                : new FailedStep(
                    request.OperationId,
                    new StepCause(
                        StepErrorCode.IdentityUnregistered,
                        "A current native object is missing its registered public identity. No action was dispatched."));
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Step dispatch failed: {error}");
            return _touched
                ? Uncertain(request)
                : new FailedStep(
                    request.OperationId,
                    new StepCause(
                        StepErrorCode.DispatchFailed,
                        "The native step could not be dispatched. No action was dispatched."));
        }
    }

    private static Creature? BindTarget(
        StepRequest request,
        string? requested,
        NodeObjectRegistry registry,
        TargetType type,
        CombatState? combat,
        Func<Creature, bool> valid,
        out StepReceipt? error)
    {
        var requiresTarget = CombatInputProbe.RequiresExplicitTarget(type);
        var targets = CombatInputProbe.LegalTargets(type, combat, valid);
        var legal = targets.Select(registry.GetRef).ToImmutableArray();
        var binding = StepRules.BindTarget(requested, requiresTarget, legal);
        error = binding.Error is { } cause ? new RejectedStep(request.OperationId, cause) : null;
        return binding.Ref is { } reference
            ? targets.Single(candidate => registry.GetRef(candidate) == reference)
            : null;
    }

    private StepReceipt? DispatchInteract(StepRequest request, InteractStep step, NodeObjectRegistry registry)
    {
        if (!registry.TryResolve<RunSurfaceLifecycle.NativeInteractionBinding>(step.Action, out var binding))
            return InvalidRefs(request, [step.Action]);
        if (ObservationHub.Current.Reply is not CompletedStateRead completed)
            return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "Native interaction facts are unavailable.");

        var actions = completed.Observation.State switch
        {
            RunStateView run => run.Actions,
            CombatStateView combat => combat.Actions,
            _ => [],
        };
        if (!actions.Any(action => action.Ref == step.Action && action.Enabled))
            return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "This native interaction is not available now.");

        if (NMerchantRoom.Instance is { } merchantRoom && ShopInputProbe.IsCurrent(merchantRoom))
        {
            var merchantInventory = merchantRoom.Inventory;
            if (ReferenceEquals(binding!.Control, merchantRoom.MerchantButton))
            {
                if (!ShopInputProbe.IsVisible(merchantRoom.MerchantButton) || !ShopInputProbe.CanOpen(merchantRoom))
                    return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native merchant inventory cannot open now.");
                _gateway.TrackNativeAcceptance(
                    request,
                    () => GodotObject.IsInstanceValid(merchantInventory) && merchantInventory.IsOpen);
                _touched = true;
                merchantRoom.OpenInventory();
                return null;
            }
            if (ShopInputProbe.IsCloseControl(merchantRoom, binding!.Control))
            {
                if (!ShopInputProbe.IsVisible((Control)binding.Control) || !ShopInputProbe.CanClose(merchantRoom))
                    return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native merchant inventory cannot close now.");
                _gateway.TrackNativeAcceptance(
                    request,
                    () => GodotObject.IsInstanceValid(merchantInventory) && !merchantInventory.IsOpen);
                _touched = true;
                merchantInventory.Call(NMerchantInventory.MethodName.Close);
                return null;
            }
            if (ReferenceEquals(binding!.Control, merchantRoom.ProceedButton))
            {
                if (!ShopInputProbe.IsVisible(merchantRoom.ProceedButton) || !ShopInputProbe.CanLeave(merchantRoom))
                    return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native merchant room cannot be left now.");
                var map = NMapScreen.Instance!;
                _gateway.TrackNativeAcceptance(request, () => GodotObject.IsInstanceValid(map) && map.IsOpen);
                _touched = true;
                merchantRoom.ProceedButton.EmitSignal(NClickableControl.SignalName.Released, merchantRoom.ProceedButton);
                return null;
            }
        }

        if (binding!.Control is NCardHolder cardHolder)
        {
            if (!cardHolder.IsVisibleInTree() || !IsControlUsable(cardHolder.Hitbox))
                return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native card holder is not enabled and visible.");
            TrackInteractionAcceptance(request, cardHolder);
            if (NOverlayStack.Instance?.Peek() is NCardRewardSelectionScreen rewardOverlay)
                return PresentOverlayCardHolder(request, rewardOverlay, cardHolder, trackGatewayTask: false);
            _touched = true;
            cardHolder.EmitSignal(NCardHolder.SignalName.Pressed, cardHolder);
            return null;
        }

        if (binding.Control is not NClickableControl button || !IsControlUsable(button))
            return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native control is not enabled and visible.");

        if (binding.Owner is MerchantEntry entry)
        {
            var panel = NMerchantRoom.Instance?.Inventory
                        ?? (NEventRoom.Instance?.CustomEventNode as NFakeMerchant)?.Inventory;
            var inventory = panel?.Inventory;
            if (panel is not { IsOpen: true }
                || inventory is null
                || !ReferenceEquals(inventory, RunManager.Instance.MerchantSynchronizer.GetLocalInventory())
                || !inventory.AllEntries.Any(candidate => ReferenceEquals(candidate, entry))
                || !entry.IsStocked
                || !entry.EnoughGold)
            {
                return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The merchant entry is no longer available.");
            }

            _touched = true;
            var previousOverlay = NOverlayStack.Instance?.Peek();
            var purchase = RunManager.Instance.MerchantSynchronizer.PurchaseLocalEntry(entry);
            _gateway.TrackNativeTask(request, purchase, () =>
                NOverlayStack.Instance?.Peek() is NRewardsScreen rewards
                && !ReferenceEquals(rewards, previousOverlay)
                && ObservationHub.Current.Reply is CompletedStateRead current
                && current.Observation.State is RunStateView { Screen: RunScreen.Rewards } state
                && state.Actions.Any(action => action.Enabled));
            _ = ObserveNativeFailureAsync(request.OperationId, purchase);
            return null;
        }

        if (button is NMapPoint mapPoint
            && (NMapScreen.Instance is not { IsOpen: true, IsTravelEnabled: true }
                || mapPoint.State != MapPointState.Travelable))
        {
            return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native map point is no longer travelable.");
        }

        TrackInteractionAcceptance(request, button);
        if (NOverlayStack.Instance?.Peek() is NCardRewardSelectionScreen rewardChoice
            && ReferenceEquals(button, NodeHierarchy.FindRewardSkipButton(rewardChoice)))
        {
            return PresentOverlaySkip(request, rewardChoice, button, trackGatewayTask: false);
        }

        switch (button)
        {
            case NMapPoint point when NMapScreen.Instance is { IsOpen: true, IsTravelEnabled: true } map
                                      && point.State == MapPointState.Travelable:
                _touched = true;
                var travel = map.TravelToMapCoord(point.Point.coord);
                _gateway.TrackNativeTask(request, travel);
                _ = ObserveNativeFailureAsync(request.OperationId, travel);
                break;
            case NMapPoint:
                throw new InvalidOperationException("The native map point is no longer travelable.");
            case NRestSiteButton rest:
                return PresentFocusAndSubmit(
                    request,
                    rest,
                    () => IsControlUsable(rest),
                    focusSeconds: 0.22,
                    () => rest.Call(NRestSiteButton.MethodName.OnRelease),
                    trackGatewayTask: false);
            case NRewardButton reward:
                return PresentFocusAndSubmit(
                    request,
                    reward,
                    () => IsControlUsable(reward),
                    focusSeconds: 0.16,
                    () => reward.Call(NRewardButton.MethodName.OnRelease),
                    trackGatewayTask: false);
            case NEventOptionButton option:
                return PresentFocusAndSubmit(
                    request,
                    option,
                    () => IsControlUsable(option) && !option.Option.WasChosen,
                    focusSeconds: 0.22,
                    option.ForceClick,
                    trackGatewayTask: false);
            case NMerchantButton merchant:
                _touched = true;
                merchant.EmitSignal(NMerchantButton.SignalName.MerchantOpened, merchant);
                break;
            case NClickableControl sphereProceed when NOverlayStack.Instance?.Peek() is NCrystalSphereScreen sphere
                                                      && ReferenceEquals(sphere.GetNodeOrNull<NClickableControl>("%ProceedButton"), sphereProceed):
                _touched = true;
                sphereProceed.EmitSignal(NClickableControl.SignalName.Released, sphereProceed);
                if (NMapScreen.Instance is { IsOpen: true } && ReferenceEquals(NOverlayStack.Instance?.Peek(), sphere))
                    NOverlayStack.Instance.Remove(sphere);
                break;
            default:
                _touched = true;
                button.EmitSignal(NClickableControl.SignalName.Released, button);
                break;
        }
        return null;
    }

    private StepReceipt? DispatchSelect(StepRequest request, SelectStep step, NodeObjectRegistry registry)
    {
        if (ObservationHub.Current.Reply is not CompletedStateRead completed
            || completed.Observation.State.Selection is not { } selection)
        {
            if (CardSelectionInterceptor.GetPending() is null
                && _autoSelectedCards is { } autoSelected
                && step.Refs.Length == autoSelected.Length
                && step.Refs.Distinct(StringComparer.Ordinal).Count() == autoSelected.Length
                && step.Refs.All(reference =>
                    registry.TryResolve<CardModel>(reference, out var card)
                    && autoSelected.Any(candidate => ReferenceEquals(candidate, card))))
            {
                return new CompletedStep(request.OperationId, StepBoundary.Ready);
            }
            return StepGateway.Reject(request, StepErrorCode.SelectionUnavailable, "No native selection is available.");
        }

        if (StepRules.ValidateSelection(step.Refs, selection) is { } cause)
            return new RejectedStep(request.OperationId, cause);

        _submittedChoice = CurrentChoice();
        if (CardSelectionInterceptor.GetPending() is { } pending)
        {
            var subjects = step.Refs.Select(reference =>
            {
                if (!registry.TryResolve<CardModel>(reference, out var card))
                    throw new IdentityUnregisteredException();
                var index = pending.Options.ToList().FindIndex(option => ReferenceEquals(option, card));
                if (index < 0)
                    throw new IdentityUnregisteredException();
                return CardSelectionInterceptor.SubjectId(pending, index);
            }).ToArray();

            if (!CardSelectionInterceptor.TrySelectMany(subjects, () => _touched = true))
                return StepGateway.Reject(request, StepErrorCode.SelectionUnavailable, "The native selection changed before submission.");
            return null;
        }

        if (NOverlayStack.Instance?.Peek() is not Node screen)
            return StepGateway.Reject(request, StepErrorCode.SelectionUnavailable, "The native selection screen is unavailable.");

        if (screen is NChooseABundleSelectionScreen bundleScreen)
        {
            if (step.Refs.IsEmpty)
            {
                var skip = bundleScreen.GetNodeOrNull<NClickableControl>("%SkipButton");
                if (skip is null || !IsControlUsable(skip))
                    return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native skip control is unavailable.");
                return PresentOverlaySkip(request, bundleScreen, skip, trackGatewayTask: true);
            }

            var bundle = NodeHierarchy.Descendants(bundleScreen)
                .OfType<NCardBundle>()
                .SingleOrDefault(candidate => registry.GetRef(candidate.Bundle) == step.Refs.Single());
            if (bundle is null || !IsControlUsable(bundle.Hitbox))
                return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The selected bundle is not available.");
            return PresentFocusAndSubmit(
                request,
                bundle.Hitbox,
                () => ReferenceEquals(NOverlayStack.Instance?.Peek(), bundleScreen)
                      && GodotObject.IsInstanceValid(bundle)
                      && IsControlUsable(bundle.Hitbox),
                focusSeconds: 0.28,
                () =>
                {
                    bundle.EmitSignal(NCardBundle.SignalName.Clicked, bundle);
                    BeginConfirmation(screen, holdMs: 220);
                },
                trackGatewayTask: true);
        }

        var cards = step.Refs.Select(reference =>
            registry.TryResolve<CardModel>(reference, out var card)
                ? card!
                : throw new IdentityUnregisteredException()).ToArray();

        if (screen is NChooseACardSelectionScreen or NCardRewardSelectionScreen)
        {
            if (cards.Length == 0)
            {
                var skip = screen is NChooseACardSelectionScreen
                    ? screen.GetNodeOrNull<NClickableControl>("SkipButton")
                    : NodeHierarchy.FindRewardSkipButton((NCardRewardSelectionScreen)screen);
                if (skip is null || !IsControlUsable(skip))
                    return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The native skip control is unavailable.");
                return PresentOverlaySkip(request, screen, skip, trackGatewayTask: true);
            }

            var holder = NodeHierarchy.Descendants(screen)
                .OfType<NCardHolder>()
                .SingleOrDefault(candidate => ReferenceEquals(candidate.CardModel, cards.Single()));
            if (holder is null || !holder.IsVisibleInTree())
                return StepGateway.Reject(request, StepErrorCode.ActionUnavailable, "The selected native card is unavailable.");
            return PresentOverlayCardHolder(request, screen, holder, trackGatewayTask: true);
        }

        if (screen is not NCardGridSelectionScreen grid)
            return StepGateway.Reject(request, StepErrorCode.SelectionUnavailable, "The native selection screen cannot accept cards.");

        var callback = grid.GetType().GetMethod("OnCardClicked", PrivateInstance)
            ?? throw new InvalidOperationException("The native selection callback is unavailable.");
        var current = grid.GetType().GetField("_selectedCards", PrivateInstance)?.GetValue(grid) as IEnumerable<CardModel>
            ?? throw new InvalidOperationException("The native selected cards are unavailable.");

        var selected = current.ToArray();
        var toggles = selected
            .Where(card => !cards.Contains(card, ReferenceEqualityComparer.Instance))
            .Concat(cards.Where(card => !selected.Contains(card, ReferenceEqualityComparer.Instance)))
            .ToArray();

        if (ViewerPacing.IsInteractiveGui && toggles.Length > 0)
        {
            var holder = ScrollCardGridIntoView(grid, toggles[0]);
            if (toggles.Length == 1 && holder is not null && holder.IsVisibleInTree())
            {
                return PresentFocusAndSubmit(
                    request,
                    holder,
                    () => ReferenceEquals(NOverlayStack.Instance?.Peek(), screen)
                          && GodotObject.IsInstanceValid(holder)
                          && holder.IsVisibleInTree(),
                    focusSeconds: 0.22,
                    () =>
                    {
                        callback.Invoke(grid, [toggles[0]]);
                        if (ReferenceEquals(NOverlayStack.Instance?.Peek(), screen))
                            BeginConfirmation(screen, holdMs: 180);
                    },
                    trackGatewayTask: true);
            }
        }

        foreach (var card in toggles)
        {
            if (!ReferenceEquals(NOverlayStack.Instance?.Peek(), screen))
                break;
            _touched = true;
            callback.Invoke(grid, [card]);
        }

        if (ReferenceEquals(NOverlayStack.Instance?.Peek(), screen))
            BeginConfirmation(screen, holdMs: 260);
        return null;
    }

    private void TrackInteractionAcceptance(StepRequest request, Control control)
    {
        var mapWasOpen = NMapScreen.Instance?.IsOpen == true;
        var merchant = NMerchantRoom.Instance?.Inventory
                       ?? (NEventRoom.Instance?.CustomEventNode as NFakeMerchant)?.Inventory;
        var shopWasOpen = merchant?.IsOpen;
        var sphereEntity = NOverlayStack.Instance?.Peek() is NCrystalSphereScreen sphereScreen
            ? typeof(NCrystalSphereScreen).GetField("_entity", PrivateInstance)?.GetValue(sphereScreen) as CrystalSphereMinigame
            : null;
        var sphereTool = sphereEntity?.CrystalSphereTool;
        var sphereCount = sphereEntity?.DivinationCount;
        var ancientLayout = NEventRoom.Instance?.Layout as NAncientEventLayout;
        var ancientLine = ReadAncientDialogueLine(ancientLayout);

        _gateway.TrackNativeAcceptance(request, () =>
            !GodotObject.IsInstanceValid(control)
            || !control.IsInsideTree()
            || !control.IsVisibleInTree()
            || control is NClickableControl { IsEnabled: false }
            || control is NCardHolder { Hitbox.IsEnabled: false }
            || control is NEventOptionButton { Option.WasChosen: true }
            || (control is NCrystalSphereCell cell
                && (!cell.Entity.IsHidden || (sphereEntity is not null && sphereEntity.DivinationCount != sphereCount)))
            || (control is NDivinationButton
                && sphereEntity is not null
                && sphereEntity.CrystalSphereTool != sphereTool)
            || (control is NAncientDialogueHitbox
                && ancientLayout is not null
                && GodotObject.IsInstanceValid(ancientLayout)
                && ReadAncientDialogueLine(ancientLayout) != ancientLine)
            || (!mapWasOpen && NMapScreen.Instance?.IsOpen == true)
            || (merchant is not null && GodotObject.IsInstanceValid(merchant) && merchant.IsOpen != shopWasOpen));
    }

    private static int? ReadAncientDialogueLine(NAncientEventLayout? layout) =>
        layout is null
            ? null
            : typeof(NAncientEventLayout).GetField("_currentDialogueLine", PrivateInstance)?.GetValue(layout) as int?;

    private static async Task ObserveNativeFailureAsync(string operationId, Task completion)
    {
        try
        {
            await completion.ConfigureAwait(false);
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Native task failed: operation_id={operationId} error={error}");
        }
    }

    private void TrackAutoSelection(StepRequest request, AbstractModel source, int? turn)
    {
        _selectionOrigin = request;
        _selectionSource = source;
        _selectionTurn = turn;
        AutoResolvedCardSelectionPatch.Resolved = (context, selected) =>
        {
            if (_gateway.Status().Receipt is not PendingStep pending
                || pending.OperationId != request.OperationId
                || context.ModelStack?.Any(model => ReferenceEquals(model, _selectionSource)) != true)
            {
                return;
            }
            if (_autoSelectedCards is not null || _multipleAutoSelections)
            {
                _autoSelectedCards = null;
                _multipleAutoSelections = true;
                return;
            }
            _autoSelectedCards = selected.ToArray();
        };
    }

    private void ClearAutoSelection()
    {
        _selectionOrigin = null;
        _selectionSource = null;
        _selectionTurn = null;
        _autoSelectedCards = null;
        _multipleAutoSelections = false;
        AutoResolvedCardSelectionPatch.Resolved = null;
    }

    private StepReceipt? PresentOverlaySkip(
        StepRequest request,
        Node screen,
        NClickableControl skip,
        bool trackGatewayTask) =>
        PresentFocusAndSubmit(
            request,
            skip,
            () => ReferenceEquals(NOverlayStack.Instance?.Peek(), screen) && IsControlUsable(skip),
            focusSeconds: 0.24,
            () => skip.EmitSignal(NClickableControl.SignalName.Released, skip),
            trackGatewayTask);

    private StepReceipt? PresentOverlayCardHolder(
        StepRequest request,
        Node screen,
        NCardHolder holder,
        bool trackGatewayTask) =>
        PresentFocusAndSubmit(
            request,
            holder,
            () => ReferenceEquals(NOverlayStack.Instance?.Peek(), screen)
                  && GodotObject.IsInstanceValid(holder)
                  && holder.IsVisibleInTree()
                  && IsControlUsable(holder.Hitbox),
            focusSeconds: 0.28,
            () => holder.EmitSignal(NCardHolder.SignalName.Pressed, holder),
            trackGatewayTask);

    private StepReceipt? PresentFocusAndSubmit(
        StepRequest request,
        Control focusNode,
        Func<bool> stillValid,
        double focusSeconds,
        Action submit,
        bool trackGatewayTask)
    {
        if (!ViewerPacing.IsInteractiveGui)
        {
            _touched = true;
            submit();
            return null;
        }

        var cue = RunFocusCueAsync(request.OperationId, focusNode, stillValid, focusSeconds, submit);
        if (trackGatewayTask)
        {
            _visualPresentation = cue;
            _gateway.TrackNativeTask(request, cue);
            _ = ObserveNativeFailureAsync(request.OperationId, cue);
        }
        else
        {
            _visualPresentation = ObserveVisualCueOrReportUncertainAsync(request.OperationId, cue);
        }
        return null;
    }

    private async Task RunFocusCueAsync(
        string operationId,
        Control focusNode,
        Func<bool> stillValid,
        double focusSeconds,
        Action submit)
    {
        var tree = focusNode.GetTree();
        var usedRealFocus = false;
        focusNode.GetViewport()?.GuiReleaseFocus();
        if (focusNode.FocusMode != Control.FocusModeEnum.None)
        {
            focusNode.GrabFocus();
            usedRealFocus = true;
        }
        else
        {
            focusNode.EmitSignal(Control.SignalName.FocusEntered);
        }

        try
        {
            await tree.ToSignal(tree.CreateTimer(focusSeconds, ignoreTimeScale: true), SceneTreeTimer.SignalName.Timeout);
        }
        finally
        {
            if (GodotObject.IsInstanceValid(focusNode) && focusNode.IsInsideTree())
            {
                if (usedRealFocus)
                {
                    if (ReferenceEquals(focusNode.GetViewport()?.GuiGetFocusOwner(), focusNode))
                        focusNode.ReleaseFocus();
                }
                else
                {
                    focusNode.EmitSignal(Control.SignalName.FocusExited);
                }
            }
        }

        if (!_control.IsRunning || !stillValid())
        {
            _gateway.RejectPendingBeforeTouch(
                operationId,
                StepErrorCode.ActionUnavailable,
                !_control.IsRunning
                    ? "Agent control was paused before dispatch."
                    : "The native control changed before dispatch.");
            return;
        }

        _touched = true;
        submit();
    }

    private async Task ObserveVisualCueOrReportUncertainAsync(string operationId, Task cue)
    {
        try
        {
            await cue;
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Viewer highlight aborted: operation_id={operationId} error={error.Message}");
            _gateway.ReportUncertain(operationId);
        }
    }

    private static NCardHolder? ScrollCardGridIntoView(NCardGridSelectionScreen screen, CardModel targetCard)
    {
        try
        {
            if (screen.GetNodeOrNull<NCardGrid>("%CardGrid") is not { } grid
                || typeof(NCardGrid).GetField("_cards", PrivateInstance)?.GetValue(grid) is not IReadOnlyList<CardModel> cards)
            {
                return null;
            }

            var index = -1;
            for (var i = 0; i < cards.Count; i++)
            {
                if (ReferenceEquals(cards[i], targetCard))
                {
                    index = i;
                    break;
                }
            }
            if (index < 0)
                return null;

            var columns = Math.Max(1, grid.Get(NCardGrid.PropertyName.Columns).AsInt32());
            var cardSize = grid.Get(NCardGrid.PropertyName._cardSize).AsVector2();
            var top = grid.Get(NCardGrid.PropertyName.ScrollLimitTop).AsSingle();
            var bottom = grid.Get(NCardGrid.PropertyName.ScrollLimitBottom).AsSingle();
            var rowCenter = grid.YOffset + 80f + cardSize.Y * 0.5f + index / columns * (cardSize.Y + 40f);
            var destination = Math.Clamp(grid.Size.Y * 0.5f - rowCenter, Math.Min(top, bottom), Math.Max(top, bottom));
            grid.SetScrollPosition(destination);
            for (var step = 0; step < 12 && grid.GetCardHolder(targetCard) is null; step++)
                grid.Call(NCardGrid.MethodName.AllocateCardHolders);
            return grid.GetCardHolder(targetCard);
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Card grid scroll skipped: {error.Message}");
            return null;
        }
    }

    private void BeginConfirmation(Node screen, ulong holdMs = 0)
    {
        _confirmationScreen = screen;
        _confirmationNotBeforeMsec = ViewerPacing.IsInteractiveGui && holdMs > 0
            ? Time.GetTicksMsec() + holdMs
            : 0;
        _pressedConfirmations.Clear();
    }

    private bool AdvanceConfirmation(Node screen)
    {
        if (ViewerPacing.IsInteractiveGui && Time.GetTicksMsec() < _confirmationNotBeforeMsec)
            return true;

        var confirm = NodeHierarchy.Descendants(screen)
            .OfType<NConfirmButton>()
            .FirstOrDefault(button => IsControlUsable(button) && !_pressedConfirmations.Contains(button));
        if (confirm is null)
            return false;
        _pressedConfirmations.Add(confirm);
        _confirmationNotBeforeMsec = 0;
        confirm.EmitSignal(NClickableControl.SignalName.Released, confirm);
        return true;
    }

    private static bool IsControlUsable(NClickableControl control) =>
        GodotObject.IsInstanceValid(control)
        && !control.IsQueuedForDeletion()
        && control.IsVisibleInTree()
        && control.IsEnabled;

    private static RejectedStep InvalidRefs(StepRequest request, ImmutableArray<string> refs) =>
        new(
            request.OperationId,
            new InvalidStepRefs("The references are not current members of this native input.", refs));

    private static UnknownStep Uncertain(StepRequest request) =>
        new(
            request.OperationId,
            new StepCause(
                StepErrorCode.ExecutionUncertain,
                "The native step may have affected the game. Read the current state before making a new decision."));
}
