using System.Reflection;
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Combat;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.TestSupport;

namespace Sts2Mod.Execution;

/// <summary>
/// Combat-scoped <see cref="ICardSelector"/> installed on <c>RunManager.Instance.CardSelectorStack</c>
/// so in-combat card selections present native UI while awaiting explicit agent <c>select</c> steps.
/// </summary>
public sealed class CardSelectionInterceptor : ICardSelector
{
    private const ulong SingleCardSelectionInputDelayMsec = 350;
    private static readonly AccessTools.FieldRef<NChooseACardSelectionScreen, ulong> SingleCardOpenedAt =
        AccessTools.FieldRefAccess<NChooseACardSelectionScreen, ulong>("_openedTicks");
    private static readonly CardSelectionInterceptor Instance = new();
    private static readonly object SelectionLock = new();
    private static IDisposable? _scope;
    private static Player? _installedPlayer;
    private static PendingCardSelection? _pending;
    private static IOverlayScreen? _visibleSelection;
    private static NPlayerHand? _visibleHandSelection;
    private static long _nextRequestId;

    public static Action<IReadOnlyList<CardModel>>? SelectionOpening { get; set; }

    internal static readonly AsyncLocal<SelectionPresentation?> Presentation = new();

    internal sealed record SelectionPresentation(
        CardSelectorPrefs? Prefs,
        bool Upgrade,
        bool? CanSkip = null,
        SelectionSource? Source = null);

    internal sealed record SelectionSource(AbstractModel Model, string ModelId, string Name);

    public sealed record PendingCardSelection(
        string ChoiceId,
        IReadOnlyList<CardModel> Options,
        int MinSelect,
        int MaxSelect,
        AbstractModel? Source,
        string? SourceModelId,
        string? SourceName,
        CardSelectorPrefs Prefs,
        bool Upgrade,
        bool? CanSkip,
        TaskCompletionSource<IEnumerable<CardModel>> Completion);

    private CardSelectionInterceptor() { }

    public static bool IsActiveFor(Player player) =>
        RunManager.Instance.IsInProgress
        && ReferenceEquals(RunManager.Instance.CardSelectorStack?.GetSelector(player), Instance);

    public static PendingCardSelection? GetPending()
    {
        lock (SelectionLock)
            return _pending;
    }

    internal static NChooseACardSelectionScreen? VisibleSingleCardSelection =>
        _visibleSelection as NChooseACardSelectionScreen;

    // The native holder ignores clicks during the first 350 ms after the overlay opens.
    internal static bool IsSelectionReady(PendingCardSelection? pending) =>
        pending?.CanSkip is null
        || (VisibleSingleCardSelection is { } screen
            && ReferenceEquals(NOverlayStack.Instance?.Peek(), screen)
            && Time.GetTicksMsec() - SingleCardOpenedAt(screen) > SingleCardSelectionInputDelayMsec);

    public static string SubjectId(PendingCardSelection request, int index) =>
        $"agent-choice:{request.ChoiceId}:{index}:{request.Options[index].Id}";

    public static void Install()
    {
        var run = RunManager.Instance.DebugOnlyGetState();
        var stack = RunManager.Instance.CardSelectorStack;
        if (run is null || stack is null || run.Players.Count == 0)
            return;

        var player = LocalContext.GetMe(run);
        if (player is null)
            return;

        if (_scope is not null
            && ReferenceEquals(_installedPlayer, player)
            && ReferenceEquals(stack.GetSelector(player), Instance))
        {
            return;
        }

        if (_scope is not null)
            ResetForRunBoundary();

        if (stack.GetSelector(player) is not null)
        {
            GD.PrintErr("[sts2agent] CardSelectionInterceptor.Install skipped: another selector is active.");
            return;
        }

        _installedPlayer = player;
        _scope = stack.UseSelector(player, Instance);
    }

    public static void Uninstall() => ResetForRunBoundary();

    public static void ResetForRunBoundary()
    {
        var scope = _scope;
        _scope = null;
        _installedPlayer = null;

        PendingCardSelection? pending;
        lock (SelectionLock)
        {
            pending = _pending;
            _pending = null;
        }

        pending?.Completion.TrySetCanceled();
        CloseVisibleSelection();

        try
        {
            scope?.Dispose();
        }
        catch (Exception)
        {
            // Ignore disposal after native run cleanup has replaced the selector stack.
        }
    }

    public static bool TrySelectMany(
        IReadOnlyList<string> subjectInstanceIds,
        Action? onSelectionCommit = null)
    {
        PendingCardSelection? request;
        List<CardModel>? selected;
        lock (SelectionLock)
        {
            request = _pending;
            if (request is null)
                return false;

            if (subjectInstanceIds.Count < request.MinSelect
                || subjectInstanceIds.Count > request.MaxSelect
                || subjectInstanceIds.Count > request.Options.Count
                || subjectInstanceIds.Distinct(StringComparer.Ordinal).Count() != subjectInstanceIds.Count)
            {
                return false;
            }

            selected = new List<CardModel>(subjectInstanceIds.Count);
            foreach (var subjectInstanceId in subjectInstanceIds)
            {
                CardModel? match = null;
                for (var index = 0; index < request.Options.Count; index++)
                {
                    if (SubjectId(request, index) != subjectInstanceId)
                        continue;
                    match = request.Options[index];
                    break;
                }
                if (match is null)
                    return false;
                selected.Add(match);
            }
        }

        if (_visibleHandSelection is { } hand)
            return SubmitHandSelection(hand, selected, onSelectionCommit);

        lock (SelectionLock)
        {
            if (!ReferenceEquals(_pending, request))
                return false;
            onSelectionCommit?.Invoke();
            _pending = null;
        }

        request.Completion.SetResult(selected);
        CloseVisibleSelection();
        return true;
    }

    public Task<IEnumerable<CardModel>> GetSelectedCards(
        IEnumerable<CardModel> options,
        int minSelect,
        int maxSelect)
    {
        if (Presentation.Value?.CanSkip is { } canSkip)
            minSelect = canSkip ? 0 : 1;

        var list = options.ToList();
        SelectionOpening?.Invoke(list);
        if (list.Count == 0)
            return Task.FromResult<IEnumerable<CardModel>>([]);

        var completion = new TaskCompletionSource<IEnumerable<CardModel>>(
            TaskCreationOptions.RunContinuationsAsynchronously);

        PendingCardSelection request;
        lock (SelectionLock)
        {
            if (_pending is not null)
                throw new InvalidOperationException("An agent card selection is already pending.");

            var source = Presentation.Value?.Source;
            request = new PendingCardSelection(
                $"combat-card-{Interlocked.Increment(ref _nextRequestId)}",
                list,
                minSelect,
                maxSelect,
                source?.Model,
                source?.ModelId,
                source?.Name,
                Presentation.Value?.Prefs ?? new CardSelectorPrefs(
                    new LocString("gameplay_ui", "CHOOSE_CARD_HEADER"),
                    minSelect,
                    maxSelect),
                Presentation.Value?.Upgrade == true,
                Presentation.Value?.CanSkip,
                completion);
            _pending = request;
        }

        try
        {
            ShowVisibleSelection(request);
        }
        catch
        {
            lock (SelectionLock)
            {
                if (ReferenceEquals(_pending, request))
                {
                    _pending = null;
                    CloseVisibleSelection();
                }
            }
            throw;
        }
        return completion.Task;
    }

    public CardRewardSelection GetSelectedCardReward(
        IReadOnlyList<CardCreationResult> options,
        IReadOnlyList<CardRewardAlternative> alternatives) =>
        throw new NotSupportedException("Synchronous card rewards require an explicit agent selection.");

    private static void ShowVisibleSelection(PendingCardSelection request)
    {
        if (request.CanSkip is { } canSkip)
        {
            var screen = NChooseACardSelectionScreen.ShowScreen(request.Options, canSkip)
                ?? throw new InvalidOperationException("The native single-card selection is unavailable.");
            _visibleSelection = screen;
            _ = CompleteFromNativeSelectionAsync(request, screen.CardsSelected());
            return;
        }

        var hand = NPlayerHand.Instance;
        if (hand is not null
            && request.Options.All(option => hand.ActiveHolders.Any(
                holder => ReferenceEquals(holder.CardNode?.Model, option))))
        {
            _visibleHandSelection = hand;
            _ = CompleteFromNativeSelectionAsync(
                request,
                hand.SelectCards(
                    request.Prefs,
                    card => request.Options.Any(option => ReferenceEquals(option, card)),
                    source: null,
                    mode: request.Upgrade ? NPlayerHand.Mode.UpgradeSelect : NPlayerHand.Mode.SimpleSelect));
            return;
        }

        var simpleScreen = NSimpleCardSelectScreen.Create(request.Options, request.Prefs);
        _visibleSelection = simpleScreen;
        var overlayStack = NOverlayStack.Instance
            ?? throw new InvalidOperationException("The native overlay stack is unavailable.");
        overlayStack.Push(simpleScreen);
        _ = CompleteFromNativeSelectionAsync(request, simpleScreen.CardsSelected());
    }

    private static bool SubmitHandSelection(
        NPlayerHand hand,
        IReadOnlyList<CardModel> selected,
        Action? onSelectionCommit)
    {
        if (!hand.IsInCardSelection)
            return false;

        var nativeSelected = typeof(NPlayerHand)
            .GetField("_selectedCards", BindingFlags.Instance | BindingFlags.NonPublic)
            ?.GetValue(hand) as IEnumerable<CardModel>
            ?? throw new InvalidOperationException("The native hand selection is unavailable.");

        if (selected.Any(card => !hand.ActiveHolders.Any(holder => ReferenceEquals(holder.CardNode?.Model, card))))
            return false;

        var current = nativeSelected.ToArray();
        var toggles = current
            .Where(card => !selected.Contains(card, ReferenceEqualityComparer.Instance))
            .Concat(selected.Where(card => !current.Contains(card, ReferenceEqualityComparer.Instance)))
            .ToArray();

        var holders = new List<NCardHolder>(toggles.Length);
        foreach (var card in toggles)
        {
            var holder = hand.ActiveHolders.FirstOrDefault(candidate =>
                ReferenceEquals(candidate.CardNode?.Model, card));
            if (holder is null)
                return false;
            holders.Add(holder);
        }

        var confirm = hand.GetNodeOrNull<NConfirmButton>("%SelectModeConfirmButton");
        if (confirm is not { Visible: true } || !confirm.IsVisibleInTree())
            return false;

        onSelectionCommit?.Invoke();
        foreach (var holder in holders)
            holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);

        confirm = hand.GetNodeOrNull<NConfirmButton>("%SelectModeConfirmButton");
        if (confirm is not { Visible: true, IsEnabled: true } || !confirm.IsVisibleInTree())
            throw new InvalidOperationException("Native combat card selection confirm button was not enabled after holder selection.");

        confirm.EmitSignal(NClickableControl.SignalName.Released, confirm);
        return true;
    }

    private static async Task CompleteFromNativeSelectionAsync(
        PendingCardSelection request,
        Task<IEnumerable<CardModel>> nativeSelection)
    {
        try
        {
            var selected = (await nativeSelection).ToList();
            lock (SelectionLock)
            {
                if (!ReferenceEquals(_pending, request))
                    return;
                _pending = null;
                _visibleSelection = null;
                _visibleHandSelection = null;
            }
            request.Completion.TrySetResult(selected);
        }
        catch (OperationCanceledException)
        {
            lock (SelectionLock)
            {
                if (!ReferenceEquals(_pending, request))
                    return;
                _pending = null;
                _visibleSelection = null;
                _visibleHandSelection = null;
            }
            request.Completion.TrySetCanceled();
        }
        catch (Exception exception)
        {
            lock (SelectionLock)
            {
                if (ReferenceEquals(_pending, request))
                {
                    _pending = null;
                    _visibleSelection = null;
                    _visibleHandSelection = null;
                }
            }
            request.Completion.TrySetException(exception);
            GD.PrintErr($"[sts2agent] Native card selection failed: {exception}");
        }
    }

    private static void CloseVisibleSelection()
    {
        var hand = _visibleHandSelection;
        _visibleHandSelection = null;
        if (hand is { IsInCardSelection: true })
            hand.Call(NPlayerHand.MethodName.CancelHandSelectionIfNecessary);

        var screen = _visibleSelection;
        _visibleSelection = null;
        if (screen is not Node node || !GodotObject.IsInstanceValid(node) || !node.IsInsideTree())
            return;
        NOverlayStack.Instance?.Remove(screen);
    }
}

[HarmonyPatch]
internal static class AutoResolvedCardSelectionPatch
{
    internal static Action<PlayerChoiceContext, IReadOnlyList<CardModel>>? Resolved { get; set; }

    private static IEnumerable<MethodBase> TargetMethods() =>
        typeof(CardSelectCmd)
            .GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Where(method => method.Name == nameof(CardSelectCmd.FromHand)
                             || (method.Name == nameof(CardSelectCmd.FromCombatPile) && method.GetParameters().Length == 5));

    private static void Postfix(
        PlayerChoiceContext context,
        Player player,
        CardSelectorPrefs prefs,
        Task<IEnumerable<CardModel>> __result)
    {
        if (!CardSelectionInterceptor.IsActiveFor(player)
            || prefs.RequireManualConfirmation
            || !__result.IsCompletedSuccessfully)
        {
            return;
        }

        var selected = __result.Result.ToArray();
        if (selected.Length > 0 && selected.Length <= prefs.MinSelect)
            Resolved?.Invoke(context, selected);
    }
}

[HarmonyPatch]
internal static class CardSelectionPresentationPatch
{
    private static IEnumerable<MethodBase> TargetMethods() =>
        typeof(CardSelectCmd)
            .GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Where(method => method.GetParameters().Any(parameter => parameter.ParameterType == typeof(CardSelectorPrefs))
                             || method.Name is nameof(CardSelectCmd.FromHandForUpgrade)
                                 or nameof(CardSelectCmd.FromChooseACardScreen));

    private static void Prefix(
        MethodBase __originalMethod,
        object[] __args,
        out CardSelectionInterceptor.SelectionPresentation? __state)
    {
        __state = CardSelectionInterceptor.Presentation.Value;
        var upgrade = __originalMethod.Name is nameof(CardSelectCmd.FromHandForUpgrade)
            or nameof(CardSelectCmd.FromDeckForUpgrade);
        var prefs = __args.OfType<CardSelectorPrefs>().FirstOrDefault();
        bool? canSkip = null;

        if (__originalMethod.Name == nameof(CardSelectCmd.FromChooseACardScreen))
        {
            canSkip = __args.OfType<bool>().Single();
            prefs = new CardSelectorPrefs(
                new LocString("gameplay_ui", "CHOOSE_CARD_HEADER"),
                canSkip.Value ? 0 : 1,
                1);
        }
        else if (__originalMethod.Name == nameof(CardSelectCmd.FromHandForUpgrade))
        {
            prefs = new CardSelectorPrefs(
                new LocString("gameplay_ui", "CHOOSE_CARD_UPGRADE_HEADER"),
                1);
        }

        var sourceIndex = Array.FindIndex(
            __originalMethod.GetParameters(),
            parameter => parameter.Name == "source");
        var source = sourceIndex < 0 ? null : __args[sourceIndex] as AbstractModel;
        source ??= __args.OfType<PlayerChoiceContext>().FirstOrDefault()?.LastInvolvedModel;
        CardSelectionInterceptor.Presentation.Value = new(prefs, upgrade, canSkip, DescribeSource(source));
    }

    private static CardSelectionInterceptor.SelectionSource? DescribeSource(AbstractModel? source)
    {
        if (source is null)
            return null;

        var name = source switch
        {
            CardModel card => card.Title,
            RelicModel relic => relic.Title.GetFormattedText(),
            PotionModel potion => potion.Title.GetFormattedText(),
            PowerModel power => power.Title.GetFormattedText(),
            _ => null,
        };
        return name is null ? null : new(source, source.Id.ToString(), name);
    }

    private static void Finalizer(CardSelectionInterceptor.SelectionPresentation? __state) =>
        CardSelectionInterceptor.Presentation.Value = __state;
}
