using System.Collections.Immutable;
using System.Reflection;
using System.Runtime.CompilerServices;
using Godot;
using MegaCrit.Sts2.addons.mega_text;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Combat.History;
using MegaCrit.Sts2.Core.Combat.History.Entries;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Ancients;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.FoolArrows;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.Potions;
using MegaCrit.Sts2.Core.Events.Custom.CrystalSphereEvent;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.HoverTips;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.Models.Relics;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Combat;
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
using MegaCrit.Sts2.Core.Nodes.Screens.TreasureRoomRelic;
using MegaCrit.Sts2.Core.Nodes.TreasureRooms;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Mod.Core.Developer;
using Sts2Mod.Core.Identity;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Sts2Mod.Developer;
using Sts2Mod.Execution;
using HpDisplay = Sts2Mod.Core.Observation.HpDisplay;

namespace Sts2Mod.Observation;

internal static class CombatInputProbe
{
    public static bool RequiresExplicitTarget(TargetType targetType) =>
        targetType is TargetType.AnyEnemy or TargetType.AnyAlly;

    public static Creature[] LegalTargets(
        TargetType targetType,
        CombatState? combat,
        Func<Creature, bool> isValidTarget)
    {
        if (!RequiresExplicitTarget(targetType) || combat is null)
            return [];

        var candidates = targetType == TargetType.AnyEnemy
            ? combat.HittableEnemies
            : combat.Allies.Where(creature => creature.Player is null || !LocalContext.IsMe(creature.Player));
        return candidates.Where(creature => creature.IsAlive && isValidTarget(creature)).ToArray();
    }

    public static bool IsCombatInputReady(Player player, CombatState? combat)
    {
        var pcs = player.PlayerCombatState;
        if (combat is null || pcs is null || pcs.Phase != PlayerTurnPhase.Play)
            return false;
        if (CombatManager.Instance.IsOverOrEnding || CombatManager.Instance.PlayerActionsDisabled)
            return false;

        var hand = NPlayerHand.Instance;
        if (hand is not null && (hand.InCardPlay || hand.CurrentMode != NPlayerHand.Mode.Play))
            return false;

        if (RunManager.Instance.ActionExecutor.CurrentlyRunningAction is not null
            || !RunManager.Instance.ActionQueueSet.IsEmpty
            || CombatManager.Instance.IsExecutingCardOrPotionEffect(player))
        {
            return false;
        }

        return true;
    }

    public static bool IsRunInputActionIdle() =>
        RunManager.Instance.ActionQueueSet.IsEmpty
        && RunManager.Instance.ActionExecutor.CurrentlyRunningAction is null
        && !ShopInputProbe.IsShopSettling(RunManager.Instance.DebugOnlyGetState());

    public static bool IsPotionUsable(
        PotionModel potion,
        Player owner,
        CombatState? combat,
        bool hasBlockingSelection)
    {
        if (hasBlockingSelection || potion.IsQueued || !owner.CanUseOrRemovePotions || !potion.PassesCustomUsabilityCheck)
            return false;

        if (combat is null && (RunManager.Instance.IsPotionUseLocked || !IsRunInputActionIdle()))
            return false;

        switch (potion.Usage)
        {
            case PotionUsage.AnyTime:
                if (combat is not null && !IsCombatInputReady(owner, combat))
                    return false;
                break;
            case PotionUsage.CombatOnly:
                var creature = owner.Creature;
                if (combat is null
                    || !CombatManager.Instance.IsInProgress
                    || creature.CombatState?.CurrentSide != creature.Side
                    || !creature.IsAlive
                    || CombatManager.Instance.PlayerActionsDisabled
                    || !IsCombatInputReady(owner, combat))
                {
                    return false;
                }
                break;
            default:
                return false;
        }

        if (RequiresExplicitTarget(potion.TargetType)
            && LegalTargets(potion.TargetType, combat, potion.IsValidTarget).Length == 0)
        {
            return false;
        }

        return true;
    }

    public static bool IsPotionDiscardable(
        PotionModel potion,
        Player owner,
        CombatState? combat,
        bool hasBlockingSelection)
    {
        if (hasBlockingSelection || potion.IsQueued || !owner.CanUseOrRemovePotions)
            return false;
        if (combat is null)
            return !RunManager.Instance.IsPotionUseLocked && IsRunInputActionIdle();
        return IsCombatInputReady(owner, combat);
    }
}

/// <summary>
/// Latches cards played in the active combat keyed by run instance and node so post-combat run observations
/// retain the full battle play list even after <see cref="CombatHistory.Clear"/> runs inside combat teardown.
/// </summary>
internal sealed class BattleHistoryTracker
{
    private long _runInstanceId;
    private string? _nodeId;
    private ImmutableArray<NamedDefinition> _cardsPlayed;
    private bool _hasSnapshot;

    public void Reset()
    {
        _runInstanceId = 0;
        _nodeId = null;
        _cardsPlayed = default;
        _hasSnapshot = false;
    }

    public void BeginCombat(long runInstanceId, string? nodeId)
    {
        if (string.IsNullOrEmpty(nodeId))
        {
            Reset();
            return;
        }

        _runInstanceId = runInstanceId;
        _nodeId = nodeId;
        _cardsPlayed = ImmutableArray<NamedDefinition>.Empty;
        _hasSnapshot = true;
    }

    public void Record(long runInstanceId, string? nodeId, ImmutableArray<NamedDefinition> cardsPlayed)
    {
        if (string.IsNullOrEmpty(nodeId))
            return;

        _runInstanceId = runInstanceId;
        _nodeId = nodeId;
        _cardsPlayed = cardsPlayed.IsDefault ? ImmutableArray<NamedDefinition>.Empty : cardsPlayed;
        _hasSnapshot = true;
    }

    public ImmutableArray<NamedDefinition>? ReadForCompletedBattle(
        long runInstanceId,
        string? nodeId,
        int? battleTurns)
    {
        if (battleTurns is not > 0 || !_hasSnapshot || string.IsNullOrEmpty(nodeId))
            return null;

        if (_runInstanceId != runInstanceId || !string.Equals(_nodeId, nodeId, StringComparison.Ordinal))
            return null;

        return _cardsPlayed;
    }
}

/// <summary>
/// Publishes immutable per-frame observations on the game thread for concurrent lock-free HTTP reads.
/// </summary>
internal static class ObservationHub
{
    private static readonly string GameId = Guid.NewGuid().ToString("N");
    private static readonly RunInstanceSequencer RunSequencer = new();
    private static readonly BattleHistoryTracker BattleHistory = new();
    private static long _sequence;
    private static NativeReadPublication _current = Failure(NativeReadError.NoRun, "No run is active.");
    private static NativeReadPublication? _lastTrusted;
    private static bool _terminal;
    private static bool _identityFailureReported;

    public static IdentityLifecycle? Identities { get; private set; }
    public static string GameInstanceId => GameId;
    public static long CurrentRunInstanceId => RunSequencer.Current;
    public static bool IsRunActive => RunSequencer.IsActive;
    public static bool IsTerminalLatched => _terminal;
    public static bool CheckpointIdentityEnabled { get; set; }
    public static Func<NativeReadPublication, NativeReadPublication>? ProjectForRead { get; set; }

    public static NativeReadPublication Current => Volatile.Read(ref _current);
    private static NativeReadPublication ForRead => ProjectForRead?.Invoke(Current) ?? Current;

    public static string ReadJson() => ObservationReader.Read(ForRead);
    public static HttpReply InspectJson(string body) => ObservationReader.Inspect(ForRead, body);
    public static string ReadCheckpointIdentityJson() => CheckpointIdentityReader.ReadJson(Current);

    public static void Initialize()
    {
        var identities = new IdentityLifecycle();
        Identities = identities;
        CardSelectionInterceptor.SelectionOpening = cards =>
        {
            foreach (var card in cards)
                identities.RegisterCard(card);
        };
        RunManager.Instance.RoomEntered += identities.EnterNode;
        CombatManager.Instance.CombatSetUp += _ =>
        {
            identities.RegisterCurrent();
            OnCombatSetUp();
        };
        CombatManager.Instance.History.Changed += OnCombatHistoryChanged;
    }

    public static void OnRunStarted()
    {
        _terminal = false;
        _identityFailureReported = false;
        _lastTrusted = null;
        BattleHistory.Reset();
        RunSequencer.BeginRun();
        Volatile.Write(
            ref _current,
            Failure(NativeReadError.ReadUnavailable, "The new run has not published an observation yet."));
        Identities?.ResetRun();
    }

    public static void OnRunCleanUp()
    {
        BattleHistory.Reset();
        RunSequencer.MarkRunEnded();
        if (!_terminal)
            Volatile.Write(ref _current, Failure(NativeReadError.NoRun, "No run is active."));
    }

    public static void Invalidate()
    {
        if (_terminal)
            return;
        Volatile.Write(ref _current, Failure(NativeReadError.ReadUnavailable, "Native publication is unavailable."));
    }

    public static void PublishCurrentFrame()
    {
        try
        {
            if (_terminal)
                return;

            if (!RunManager.Instance.IsInProgress || RunManager.Instance.IsCleaningUp)
            {
                Volatile.Write(ref _current, Failure(NativeReadError.NoRun, "No run is active."));
                return;
            }

            var run = RunManager.Instance.DebugOnlyGetState();
            // Native setup installs the run state before RunStarted registers its identities.
            if (!RunSequencer.IsActive || run is null || run.Players.Count == 0)
            {
                Volatile.Write(ref _current, Failure(NativeReadError.ReadUnavailable, "Native run state is not ready."));
                return;
            }

            var registry = Identities?.Registry ?? throw new IdentityUnregisteredException();
            var runInstanceId = RunSequencer.Current;
            var combat = CombatManager.Instance.IsInProgress && !CombatManager.Instance.IsEnding
                ? CombatManager.Instance.DebugOnlyGetState()
                : null;
            var language = LocManager.Instance?.Language ?? "";

            var observer = new GameObserver(registry, run, combat, runInstanceId, language);
            var player = run.Players.Single();
            var deck = player.Deck.Cards.Select(registry.GetRef).ToImmutableArray();
            var sequence = Interlocked.Increment(ref _sequence);

            var combatHistory = combat is not null ? CaptureCombatHistory(player, combat) : null;
            if (combatHistory is not null)
                BattleHistory.Record(runInstanceId, registry.NodeId, combatHistory.CardsPlayedThisCombat);

            var observation = combat is not null
                ? observer.CaptureCombat(GameId, sequence, deck)
                : observer.CaptureRun(GameId, sequence, deck, BattleHistory);

            var publication = new NativeReadPublication(
                new CompletedStateRead(observation),
                combatHistory,
                deck)
            {
                InspectionUpgrades = observer.InspectionUpgrades,
                CheckpointIdentities = CheckpointIdentityEnabled
                    ? TryCaptureCheckpointIdentities(observation, registry)
                    : null,
            };

            _identityFailureReported = false;
            _lastTrusted = publication;
            Volatile.Write(ref _current, publication);
        }
        catch (IdentityUnregisteredException error)
        {
            if (!_identityFailureReported)
                GD.PrintErr($"[sts2agent] Native identity publication failed: {error}");
            _identityFailureReported = true;
            Volatile.Write(
                ref _current,
                Failure(NativeReadError.IdentityUnregistered, "A native object was not registered before publication."));
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Native observation capture failed: {error}");
            Volatile.Write(
                ref _current,
                Failure(NativeReadError.ReadUnavailable, "The native observation could not be captured."));
        }
    }

    public static void PublishRunEnded(bool isVictory)
    {
        if (_terminal)
            return;

        if (!RunManager.Instance.IsCleaningUp && RunManager.Instance.IsInProgress)
            PublishCurrentFrame();

        _terminal = true;
        RunSequencer.MarkRunEnded();
        var previous = _lastTrusted
                       ?? Failure(NativeReadError.ReadUnavailable, "The completed run has no trustworthy observation.");
        var stamp = new ObservationStamp(GameId, Interlocked.Increment(ref _sequence));
        Volatile.Write(
            ref _current,
            TerminalObservation.Create(previous, isVictory ? RunResult.Victory : RunResult.Defeat, stamp));
    }

    private static void OnCombatSetUp()
    {
        if (!RunSequencer.IsActive)
            return;
        BattleHistory.BeginCombat(RunSequencer.Current, Identities?.Registry.NodeId);
    }

    private static void OnCombatHistoryChanged()
    {
        try
        {
            if (!RunSequencer.IsActive)
                return;

            var history = CombatManager.Instance.History;
            // CombatHistory.Clear() invokes Changed after emptying entries; preserve the latched battle snapshot.
            if (!history.Entries.Any())
                return;

            var run = RunManager.Instance.DebugOnlyGetState();
            var nodeId = Identities?.Registry.NodeId;
            if (run is null || run.Players.Count != 1 || string.IsNullOrEmpty(nodeId))
                return;

            BattleHistory.Record(RunSequencer.Current, nodeId, CaptureCardsPlayed(history, run.Players[0]));
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Combat history latch failed: {error}");
        }
    }

    internal static ImmutableArray<NamedDefinition> CaptureCardsPlayed(CombatHistory history, Player player) =>
        history.CardPlaysFinished
            .Where(entry => ReferenceEquals(entry.Actor?.Player, player))
            .Select(entry => new NamedDefinition(entry.CardPlay.Card.Title, entry.CardPlay.Card.Id.ToString()))
            .ToImmutableArray();

    private static HistoryInspectionData CaptureCombatHistory(Player player, CombatState combat)
    {
        var history = CombatManager.Instance.History;
        var playedCombat = CaptureCardsPlayed(history, player);

        var playedTurn = history.CardPlaysFinished
            .Where(entry => ReferenceEquals(entry.Actor?.Player, player) && entry.HappenedThisTurn(combat))
            .Select(entry => new NamedDefinition(entry.CardPlay.Card.Title, entry.CardPlay.Card.Id.ToString()))
            .ToImmutableArray();

        var exhaustedTurn = history.Entries
            .OfType<CardExhaustedEntry>()
            .Where(entry => ReferenceEquals(entry.Actor?.Player, player) && entry.HappenedThisTurn(combat))
            .Select(entry => new NamedDefinition(entry.Card.Title, entry.Card.Id.ToString()))
            .ToImmutableArray();

        return new HistoryInspectionData(playedCombat, playedTurn, exhaustedTurn);
    }

    private static CheckpointIdentityFacts? TryCaptureCheckpointIdentities(
        NativeObservation observation,
        NodeObjectRegistry registry)
    {
        try
        {
            return CheckpointIdentities.Capture(observation, registry);
        }
        catch (Exception error)
        {
            GD.PrintErr($"[sts2agent] Checkpoint identity capture unavailable: {error}");
            return null;
        }
    }

    private static NativeReadPublication Failure(NativeReadError code, string message) =>
        new(new FailedStateRead(new ReadCause(code, message)));
}

internal sealed class GameObserver(
    NodeObjectRegistry registry,
    RunState run,
    CombatState? combat,
    long runInstanceId,
    string gameLanguage)
{
    private static readonly ConditionalWeakTable<CardModel, UpgradePreviewReader> UpgradeReaders = new();

    private readonly Dictionary<string, ObservedObject> _objects = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string?> _descriptions = new(StringComparer.Ordinal);
    private readonly Dictionary<string, CardModifierDescriptions> _modifiers = new(StringComparer.Ordinal);
    private readonly Dictionary<string, ImmutableArray<GeneratedCard>> _generated = new(StringComparer.Ordinal);
    private readonly Dictionary<string, UpgradePreview> _upgrades = new(StringComparer.Ordinal);
    private readonly Dictionary<string, UpgradePreview> _inspectionUpgrades = new(StringComparer.Ordinal);
    private readonly List<MechanismView> _mechanisms = [];
    private readonly List<PublicInteraction> _interactions = [];
    private readonly List<OfferView> _offers = [];
    private readonly HashSet<CardModel> _cards = new(ReferenceEqualityComparer.Instance);
    private readonly Dictionary<Creature, ImmutableArray<PublicPower>> _powers = new(ReferenceEqualityComparer.Instance);
    private readonly HashSet<string> _inlineEnemyPowerIds = new(StringComparer.Ordinal);
    private readonly List<(string Owner, Type PowerType, string FallbackName)> _pendingPowerRules = [];
    private readonly HashSet<(string Owner, string PowerId)> _emittedPowerRules = [];

    public ImmutableDictionary<string, UpgradePreview> InspectionUpgrades =>
        _inspectionUpgrades.ToImmutableDictionary(StringComparer.Ordinal);

    public NativeObservation CaptureCombat(string gameId, long sequence, ImmutableArray<string> deck)
    {
        var player = run.Players.Single();
        var pcs = player.PlayerCombatState
            ?? throw new InvalidOperationException("Native player combat state is absent.");

        var pending = CardSelectionInterceptor.GetPending();
        var combatReady = pending is null && CombatInputProbe.IsCombatInputReady(player, combat);
        AddInventory(hasBlockingSelection: pending is not null, combatReady: combatReady);

        var enemies = combat!.Enemies.Where(creature => creature.IsAlive).Select(ObserveEnemy).ToImmutableArray();
        var inactive = combat.Enemies.Where(creature => !creature.IsAlive).Select(ObserveEnemy).ToImmutableArray();
        foreach (var creature in combat.Enemies)
        {
            var enemyRef = registry.GetRef(creature);
            var (mechanics, inlinePowers, hoistedPowers, completeness) = EnemyMechanismReader.ReadWithPowers(creature, gameLanguage);
            _inlineEnemyPowerIds.UnionWith(inlinePowers);
            if (mechanics is not null || completeness == MechanismCompleteness.Unavailable)
                _mechanisms.Add(new MechanismView(
                    MechanismKind.Other, creature.ModelId.ToString(), CreatureName(creature), mechanics,
                    enemyRef)
                {
                    Completeness = completeness == MechanismCompleteness.Complete ? null : completeness,
                });
            foreach (var powerType in hoistedPowers)
                _pendingPowerRules.Add((enemyRef, powerType, powerType.Name));
        }
        var allies = combat.Allies
            .Where(creature => creature.Player is null || !LocalContext.IsMe(creature.Player))
            .Select(ObserveAlly)
            .ToImmutableArray();
        var actor = player.Creature;
        if (player.Character is Regent || pcs.Stars > 0)
            AddRegentStarsMacro(CreatureHolderKey(actor));
        if (player.Character is Necrobinder)
            AddOstyMacros(CreatureHolderKey(actor));

        var selectionReady = pending is not null && CardSelectionInterceptor.IsSelectionReady(pending);
        var readiness = selectionReady
            ? NativeInputReadiness.Selection
            : combatReady
                ? NativeInputReadiness.Ready
                : NativeInputReadiness.Busy;

        var state = new CombatStateView(
            readiness == NativeInputReadiness.Busy ? PublicInputControl.Busy : PublicInputControl.Ready,
            ObserveProgress(),
            new CombatPlayer(
                player.Character.Title.GetFormattedText(),
                player.Character.Id.ToString(),
                actor.CurrentHp,
                actor.MaxHp,
                player.Gold,
                player.PotionSlots.Count,
                player.PotionSlots.Count(potion => potion is null),
                actor.Block,
                new ResourceBalances(pcs.Energy, pcs.Stars),
                ObservePowers(actor)),
            player.Relics.Select(registry.GetRef).ToImmutableArray(),
            player.Potions.Select(registry.GetRef).ToImmutableArray(),
            [],
            ObserveCombatSelection(pending),
            ObservationPhase.Combat,
            pending is not null ? CombatScreen.CardSelection : CombatScreen.Combat,
            combat.RoundNumber,
            deck,
            new PileMembership(
                Refs(pcs.Hand.Cards),
                SortedRefs(pcs.DrawPile.Cards),
                SortedRefs(pcs.DiscardPile.Cards),
                SortedRefs(pcs.ExhaustPile.Cards),
                SortedRefs(pcs.AllPiles
                    .Where(pile => pile != pcs.Hand
                                   && pile != pcs.DrawPile
                                   && pile != pcs.DiscardPile
                                   && pile != pcs.ExhaustPile)
                    .SelectMany(pile => pile.Cards))),
            enemies,
            inactive)
        {
            Allies = allies.IsEmpty ? null : allies,
            Orbs = ObserveOrbs(pcs.OrbQueue.Capacity, pcs.OrbQueue.Orbs, actor),
            ArrowSlots = ObserveArrowSlots(pcs.Arrows, combat, actor),
            Melodies = ObserveMelodies(player, actor),
            TurnActivity = CaptureTurnActivity(player, combat),
        };

        FlushPowerMechanisms();
        var encounter = combat.Encounter;
        return new NativeObservation(
            new ObservationStamp(gameId, sequence),
            new ObservationLocation(gameId, $"run-{runInstanceId}", registry.NodeId, ObservationOwner.Combat),
            gameLanguage,
            readiness,
            state,
            _objects.ToImmutableDictionary(StringComparer.Ordinal),
            _descriptions.ToImmutableDictionary(StringComparer.Ordinal),
            _modifiers.ToImmutableDictionary(StringComparer.Ordinal),
            _upgrades.ToImmutableDictionary(StringComparer.Ordinal),
            _generated.ToImmutableDictionary(StringComparer.Ordinal),
            _mechanisms.ToImmutableArray(),
            CaptureRunRules())
        {
            Map = MapObserver.Observe(run),
            Encounter = encounter is null
                ? null
                : new NamedDefinition(encounter.Title.GetFormattedText(), encounter.Id.ToString()),
        };
    }

    internal static TurnActivity CaptureTurnActivity(Player player, CombatState combat) =>
        BuildTurnActivity(CombatManager.Instance.History, player, combat);

    internal static TurnActivity BuildTurnActivity(CombatHistory history, Player player, ICombatState combat)
    {
        var playedThisTurn = history.CardPlaysFinished
            .Where(entry => ReferenceEquals(entry.Actor?.Player, player) && entry.HappenedThisTurn(combat))
            .Select(entry => entry.CardPlay.Card.Type);

        var exhaustedThisTurn = history.Entries
            .OfType<CardExhaustedEntry>()
            .Count(entry => ReferenceEquals(entry.Actor?.Player, player) && entry.HappenedThisTurn(combat));

        var discardedThisTurn = history.Entries
            .OfType<CardDiscardedEntry>()
            .Count(entry => ReferenceEquals(entry.Actor?.Player, player) && entry.HappenedThisTurn(combat));

        return CountTurnActivity(playedThisTurn, exhaustedThisTurn, discardedThisTurn);
    }

    internal static TurnActivity CountTurnActivity(
        IEnumerable<CardType> playedCardTypesThisTurn,
        int cardsExhaustedThisTurn,
        int? cardsDiscardedThisTurn)
    {
        var cardsPlayed = 0;
        var attacksPlayed = 0;
        var skillsPlayed = 0;
        var powersPlayed = 0;

        foreach (var cardType in playedCardTypesThisTurn)
        {
            cardsPlayed++;
            switch (cardType)
            {
                case CardType.Attack:
                    attacksPlayed++;
                    break;
                case CardType.Skill:
                    skillsPlayed++;
                    break;
                case CardType.Power:
                    powersPlayed++;
                    break;
            }
        }

        return new TurnActivity(
            cardsPlayed,
            attacksPlayed,
            skillsPlayed,
            powersPlayed,
            cardsExhaustedThisTurn)
        {
            CardsDiscarded = cardsDiscardedThisTurn,
        };
    }

    public NativeObservation CaptureRun(
        string gameId,
        long sequence,
        ImmutableArray<string> deck,
        BattleHistoryTracker? battleHistory = null)
    {
        var player = run.Players.Single();
        var selection = ObserveRunSelection();
        var overlay = NOverlayStack.Instance?.Peek();
        var hasOverlaySelection = selection is not null || overlay is NCardRewardSelectionScreen;
        AddInventory(hasBlockingSelection: hasOverlaySelection, combatReady: false);

        var screen = RunScreen.Other;
        EventView? currentEvent = null;
        var hasActiveEvent = IsEventSurfaceActive();

        if (selection is not null)
        {
            screen = RunScreen.Selection;
            if (hasActiveEvent)
                currentEvent = ObserveEvent(publishControls: false);
        }
        // The native map covers the overlay stack, which can still retain settled rewards.
        else if (NMapScreen.Instance is { IsOpen: true, IsTravelEnabled: true } map)
        {
            ObserveMapInteractions(map);
            screen = RunScreen.Map;
        }
        else if (overlay is NCrystalSphereScreen sphere)
        {
            ObserveCrystalSphere(sphere);
            screen = RunScreen.Event;
        }
        else if (overlay is NCardRewardSelectionScreen cardReward)
        {
            var activeOfferRef = ObserveCardRewards(cardReward);
            selection = ObserveCardRewardSelection(cardReward, activeOfferRef);
            screen = RunScreen.Rewards;
        }
        else if (overlay is NRewardsScreen rewards)
        {
            ObserveRewards(rewards, active: true);
            screen = RunScreen.Rewards;
        }
        else if (hasActiveEvent)
        {
            currentEvent = ObserveEvent(publishControls: true);
            screen = RunScreen.Event;
            if (NEventRoom.Instance?.CustomEventNode is NFakeMerchant fake)
            {
                ObserveShop(fake.Inventory);
                if (fake.Inventory.IsOpen)
                    screen = RunScreen.Shop;
            }
        }
        else if (run.CurrentRoom?.RoomType == RoomType.Shop && NMerchantRoom.Instance is { } merchant)
        {
            ObserveShop(merchant.Inventory);
            screen = RunScreen.Shop;
        }
        else if (run.CurrentRoom?.RoomType == RoomType.RestSite && NRestSiteRoom.Instance is { } rest)
        {
            ObserveRestSite(rest);
            screen = RunScreen.Rest;
        }
        else if (run.CurrentRoom?.RoomType == RoomType.Treasure)
        {
            screen = ObserveTreasureRoom() ? RunScreen.Treasure : RunScreen.Transition;
        }

        if (screen == RunScreen.Other && run.CurrentRoom is null)
            screen = RunScreen.Transition;

        var hasEnabledInteraction = _interactions.Any(action => action.Enabled);
        var selectionReady = selection is not null
                             && CardSelectionInterceptor.IsSelectionReady(CardSelectionInterceptor.GetPending())
                             && (screen != RunScreen.Rewards || hasEnabledInteraction);

        var readiness = selectionReady
            ? NativeInputReadiness.Selection
            : selection is not null
                ? NativeInputReadiness.Busy
                : hasEnabledInteraction
                  && CombatInputProbe.IsRunInputActionIdle()
                  && !RunManager.Instance.IsChangingRooms
                  && screen is not (RunScreen.Transition or RunScreen.Other)
                    ? NativeInputReadiness.Ready
                    : NativeInputReadiness.Busy;

        var battleTurns = run.CurrentMapPointHistoryEntry?.Rooms.LastOrDefault(room =>
            room.RoomType is RoomType.Monster or RoomType.Elite or RoomType.Boss)?.TurnsTaken;
        var resolvedBattleTurns = battleTurns is > 0 ? battleTurns : null;
        var battleCardsPlayed = battleHistory?.ReadForCompletedBattle(
            runInstanceId,
            registry.NodeId,
            resolvedBattleTurns);

        var state = new RunStateView(
            readiness == NativeInputReadiness.Busy ? PublicInputControl.Busy : PublicInputControl.Ready,
            ObserveProgress(),
            new RunPlayer(
                player.Character.Title.GetFormattedText(),
                player.Character.Id.ToString(),
                player.Creature.CurrentHp,
                player.Creature.MaxHp,
                player.Gold,
                player.PotionSlots.Count,
                player.PotionSlots.Count(potion => potion is null),
                CombatManager.baseHandDrawCount,
                player.MaxEnergy,
                player.Character.BaseOrbSlotCount),
            player.Relics.Select(registry.GetRef).ToImmutableArray(),
            player.Potions.Select(registry.GetRef).ToImmutableArray(),
            _interactions.ToImmutableArray(),
            selection,
            ObservationPhase.Run,
            screen,
            deck,
            _offers.ToImmutableArray())
        {
            Event = currentEvent,
            CardRarityOffset = player.PlayerOdds.CardRarity.CurrentValue,
            BattleTurns = resolvedBattleTurns,
            BattleCardsPlayed = battleCardsPlayed,
        };

        var mapView = MapObserver.Observe(run);
        if (mapView is not null)
        {
            var upcomingBosses = RemainingBossEncounters(includeCurrentCombatBoss: false).ToArray();
            if (upcomingBosses.Length > 0)
            {
                if (MacroMechanismReader.ReadActBossContinuity(upcomingBosses) is { } doubleBossRule)
                {
                    _mechanisms.Add(new MechanismView(
                        MechanismKind.Other,
                        MacroMechanismReader.ActDoubleBossId,
                        run.Act.Title.GetFormattedText(),
                        doubleBossRule,
                        $"{ActMechanismOwner}:{upcomingBosses[0].Id}"));
                }
                if (EventMechanismReader.ReadActTransition(run) is { } actRule)
                {
                    _mechanisms.Add(new MechanismView(
                        MechanismKind.Other,
                        run.Act.Id.ToString(),
                        run.Act.Title.GetFormattedText(),
                        actRule,
                        ActMechanismOwner));
                }
                foreach (var encounter in upcomingBosses.Concat(run.Act.AllEliteEncounters))
                {
                    var encounterId = encounter.Id.ToString();
                    var (rules, hoistedPowers, completeness) = EncounterMechanismReader.ReadWithHoistedPowers(
                        encounter, run.AscensionLevel, run.GameMode, gameLanguage);
                    if (rules is not null || completeness == MechanismCompleteness.Unavailable)
                    {
                        _mechanisms.Add(new MechanismView(
                            MechanismKind.Other,
                            encounterId,
                            encounter.Title.GetFormattedText(),
                            rules,
                            ActMechanismOwner)
                        {
                            Completeness = completeness == MechanismCompleteness.Complete ? null : completeness,
                        });
                    }
                    var powerOwner = $"{ActMechanismOwner}:{encounterId}";
                    foreach (var powerType in hoistedPowers)
                        _pendingPowerRules.Add((powerOwner, powerType, powerType.Name));
                }
            }
        }

        FlushPowerMechanisms();
        return new NativeObservation(
            new ObservationStamp(gameId, sequence),
            new ObservationLocation(gameId, $"run-{runInstanceId}", registry.NodeId, ObservationOwner.Run),
            gameLanguage,
            readiness,
            state,
            _objects.ToImmutableDictionary(StringComparer.Ordinal),
            _descriptions.ToImmutableDictionary(StringComparer.Ordinal),
            _modifiers.ToImmutableDictionary(StringComparer.Ordinal),
            _upgrades.ToImmutableDictionary(StringComparer.Ordinal),
            _generated.ToImmutableDictionary(StringComparer.Ordinal),
            _mechanisms.ToImmutableArray(),
            CaptureRunRules())
        {
            Map = mapView,
        };
    }

    private PublicProgress ObserveProgress() =>
        new(
            run.CurrentActIndex + 1,
            run.Acts.Count,
            run.TotalFloor,
            MapObserver.CurrentNodePosition(run),
            run.CurrentRoom?.RoomType.ToString().ToLowerInvariant(),
            run.Act.Title.GetFormattedText(),
            run.Act.Id.ToString())
        {
            BossesLeft = CountBossesLeft(),
        };

    private RunRules CaptureRunRules() =>
        new(
            run.AscensionLevel,
            Enumerable.Range(1, run.AscensionLevel)
                .Select(level => new AscensionRule(
                    level,
                    AscensionHelper.GetTitle(level, run.GameMode).GetFormattedText(),
                    AscensionHelper.GetDescription(level, run.GameMode).GetFormattedText()))
                .ToImmutableArray());

    private IEnumerable<EncounterModel> RemainingBossEncounters(bool includeCurrentCombatBoss)
    {
        if (run.Map is null or NullActMap)
            yield break;

        var current = run.CurrentMapPoint;
        bool IsRemaining(MegaCrit.Sts2.Core.Map.MapPoint? point) =>
            point is not null
            && (current is null
                || point.coord.row > current.coord.row
                || (includeCurrentCombatBoss && point.coord.row == current.coord.row));

        if (IsRemaining(run.Map.BossMapPoint))
            yield return run.Act.BossEncounter;
        if (IsRemaining(run.Map.SecondBossMapPoint) && run.Act.SecondBossEncounter is { } secondEncounter)
            yield return secondEncounter;
    }

    private int CountBossesLeft() =>
        RemainingBossEncounters(includeCurrentCombatBoss: combat is not null).Count();

    private void AddInventory(bool hasBlockingSelection, bool combatReady)
    {
        var player = run.Players.Single();
        foreach (var card in player.Deck.Cards)
            AddAutomaticUpgrade(card, combatReady: false);

        if (combat is not null)
        {
            foreach (var pile in player.Piles)
            {
                foreach (var card in pile.Cards)
                    AddCard(card, combatReady);
            }
        }

        var pending = CardSelectionInterceptor.GetPending();
        if (pending is not null)
        {
            foreach (var card in pending.Options)
            {
                if (combat is null)
                    AddAutomaticUpgrade(card, combatReady: false);
                else
                    AddCard(card, combatReady: false);
            }
        }

        foreach (var potion in player.Potions)
        {
            var usable = CombatInputProbe.IsPotionUsable(potion, player, combat, hasBlockingSelection);
            AddPotion(potion, usable);
        }

        foreach (var relic in player.Relics)
            AddRelic(relic);
    }

    private void AddCard(CardModel card, bool combatReady)
    {
        if (!_cards.Add(card))
            return;

        var reference = registry.GetRef(card);
        var pile = card.Pile?.Type ?? PileType.None;
        bool? playable = null;
        ImmutableArray<TargetTextPreview>? previews = null;

        if (combat is not null)
        {
            var requiresTarget = CombatInputProbe.RequiresExplicitTarget(card.TargetType);
            var legalTargets = CombatInputProbe.LegalTargets(card.TargetType, combat, card.IsValidTarget);

            if (pile == PileType.Hand)
            {
                playable = combatReady
                           && card.CanPlay()
                           && (!requiresTarget || legalTargets.Any(card.CanPlayTargeting));
            }

            if (requiresTarget)
            {
                previews = legalTargets
                    .Select(target => new TargetTextPreview(
                        registry.GetRef(target),
                        CardPreviewReader.ReadTargetText(card, pile, target)))
                    .ToImmutableArray();
            }
        }

        var rendered = CardPreviewReader.ReadLiveCard(card, pile, playable, previews);
        _objects.Add(reference, rendered.Entry);
        _descriptions.Add(reference, rendered.Text);
        _modifiers.Add(reference, rendered.ModifierDescriptions);
        AddHoverTipMaterial(reference, rendered.Entry.Name, rendered.Entry.DefinitionId, card.HoverTips);
        if (ItemMechanismReader.ReadCard(card, gameLanguage) is { } cardRule)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                rendered.Entry.DefinitionId,
                rendered.Entry.Name,
                cardRule,
                reference));
        }
        foreach (var powerType in ItemMechanismReader.ReferencedPowers(card))
            _pendingPowerRules.Add((reference, powerType, powerType.Name));
        if (card.Enchantment is { } enchantment && ModifierMechanismReader.ReadEnchantment(enchantment, gameLanguage) is { } enchRule)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                enchantment.Id.ToString(),
                enchantment.Title.GetFormattedText(),
                enchRule,
                reference));
        }
        if (card.Affliction is { } affliction && ModifierMechanismReader.ReadAffliction(affliction, gameLanguage) is { } affRule)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                affliction.Id.ToString(),
                affliction.Title.GetFormattedText(),
                affRule,
                reference));
        }
        if (card.EnergyCost.CostsX || card.HasStarCostX)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                MacroMechanismReader.XCostRuleId,
                "X-Cost",
                MacroMechanismReader.XCostRule(),
                reference));
        }
        if (card.BaseStarCost > 0 || card.CurrentStarCost > 0 || card.HasStarCostX || ItemMechanismReader.UsesStars(card))
            AddRegentStarsMacro(reference);
        if (card is MegaCrit.Sts2.Core.Models.Cards.SovereignBlade || ItemMechanismReader.UsesForge(card))
            AddForgeMacro(reference);
        if (ItemMechanismReader.UsesOsty(card))
            AddOstyMacros(reference);
        if (ItemMechanismReader.UsesSly(card))
            AddSlyMacro(reference);
        if (ItemMechanismReader.UsesArrowShooting(card))
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                MacroMechanismReader.FoolArrowsRuleId,
                "Fool Arrows",
                MacroMechanismReader.FoolArrowsRule(),
                reference));
        }
        if (ItemMechanismReader.UsesMelodies(card))
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                MacroMechanismReader.FoolMelodiesRuleId,
                "Fool Melodies",
                MacroMechanismReader.FoolMelodiesRule(),
                reference));
        }

        var upgradeReader = UpgradeReaders.GetValue(
            card,
            native => new UpgradePreviewReader(error =>
                GD.PrintErr($"[sts2agent] Upgrade preview unavailable for {native.Id}: {error}")));
        _inspectionUpgrades.Add(
            reference,
            upgradeReader.Read(card.IsUpgradable, () => CardPreviewReader.ReadUpgradedPreview(card)));
    }

    private void AddAutomaticUpgrade(CardModel card, bool combatReady = false)
    {
        AddCard(card, combatReady);
        var reference = registry.GetRef(card);
        _upgrades[reference] = _inspectionUpgrades[reference];
    }

    private void AddPotion(PotionModel potion, bool usable = false)
    {
        var reference = registry.GetRef(potion);
        if (_objects.ContainsKey(reference))
            return;

        var title = potion.Title.GetFormattedText();
        _objects[reference] = new PotionEntry(
            potion.Id.ToString(),
            title,
            usable,
            CombatInputProbe.RequiresExplicitTarget(potion.TargetType),
            potion.TargetType.ToString().ToLowerInvariant(),
            potion.Usage.ToString().ToLowerInvariant());
        _descriptions[reference] = potion.DynamicDescription.GetFormattedText();
        AddHoverTipMaterial(reference, title, potion.Id.ToString(), potion.HoverTips);
        if (ItemMechanismReader.ReadPotion(potion, gameLanguage) is { } potionRule)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                potion.Id.ToString(),
                title,
                potionRule,
                reference));
        }
        foreach (var powerType in ItemMechanismReader.ReferencedPowers(potion))
            _pendingPowerRules.Add((reference, powerType, powerType.Name));
        if (ItemMechanismReader.UsesStars(potion))
            AddRegentStarsMacro(reference);
        if (ItemMechanismReader.UsesForge(potion))
            AddForgeMacro(reference);
        if (ItemMechanismReader.UsesOsty(potion))
            AddOstyMacros(reference);
    }

    private void AddRelic(RelicModel relic)
    {
        var reference = registry.GetRef(relic);
        if (_objects.ContainsKey(reference))
            return;

        var title = relic.Title.GetFormattedText();
        _objects[reference] = new RelicEntry(
            relic.Id.ToString(),
            title,
            relic.Status switch
            {
                MegaCrit.Sts2.Core.Entities.Relics.RelicStatus.Normal => Sts2Mod.Core.Observation.RelicStatus.Normal,
                MegaCrit.Sts2.Core.Entities.Relics.RelicStatus.Active => Sts2Mod.Core.Observation.RelicStatus.Active,
                MegaCrit.Sts2.Core.Entities.Relics.RelicStatus.Disabled => Sts2Mod.Core.Observation.RelicStatus.Disabled,
                _ => throw new ArgumentOutOfRangeException(nameof(relic), relic.Status, "Unknown native relic status."),
            },
            relic.IsUsedUp,
            relic.IsMelted,
            relic.StackCount)
        {
            Counter = relic.ShowCounter ? relic.DisplayAmount : null,
            UsedThisCombat = combat is not null && relic is CentennialPuzzle puzzle ? puzzle.UsedThisCombat : null,
        };
        _descriptions[reference] = relic.DynamicDescription.GetFormattedText();
        AddHoverTipMaterial(reference, title, relic.Id.ToString(), relic.HoverTips);
        if (RelicMechanismReader.Read(relic, gameLanguage) is { } relicRule)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                relic.Id.ToString(),
                title,
                relicRule,
                reference));
        }
        foreach (var powerType in RelicMechanismReader.ReferencedPowers(relic))
            _pendingPowerRules.Add((reference, powerType, powerType.Name));
        if (ItemMechanismReader.UsesStars(relic))
            AddRegentStarsMacro(reference);
        if (ItemMechanismReader.UsesForge(relic))
            AddForgeMacro(reference);
        if (ItemMechanismReader.UsesOsty(relic))
            AddOstyMacros(reference);
    }

    private ImmutableArray<ArrowSlotView>? ObserveArrowSlots(
        IReadOnlyList<ArrowSlot> slots, CombatState combatState, Creature owner)
    {
        if (slots.Count == 0)
            return null;

        var targetRefs = new Dictionary<ArrowSlot, string>(ReferenceEqualityComparer.Instance);
        foreach (var target in combatState.Creatures)
        {
            foreach (var impaled in target.GetPowerInstances<ImpaledPower>().Where(power => power.Applier == owner))
            {
                var targetRef = target == owner ? "player" : registry.GetRef(target);
                foreach (var arrow in impaled.Arrows)
                    targetRefs.Add(arrow, targetRef);
            }
        }

        _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.FoolArrowsRuleId,
            "Fool Arrows",
            MacroMechanismReader.FoolArrowsRule(),
            CreatureHolderKey(owner)));

        return slots.Select(slot => new ArrowSlotView(
            slot.Status switch
            {
                ArrowStatus.Retrieved => ArrowSlotStatus.Retrieved,
                ArrowStatus.Away => ArrowSlotStatus.Away,
                _ => throw new ArgumentOutOfRangeException(nameof(slots), slot.Status, "Unknown native arrow status."),
            },
            slot.Status == ArrowStatus.Away ? targetRefs.GetValueOrDefault(slot) : null)).ToImmutableArray();
    }

    private ImmutableArray<MelodyTag>? ObserveMelodies(Player player, Creature actor)
    {
        var melodies = ImmutableArray.CreateBuilder<MelodyTag>(3);
        if (MelodyCmd.PlayedWahThisTurn(player))
            melodies.Add(MelodyTag.Wah);
        if (MelodyCmd.PlayedChoThisTurn(player))
            melodies.Add(MelodyTag.Cho);
        if (MelodyCmd.PlayedKiThisTurn(player))
            melodies.Add(MelodyTag.Ki);
        if (melodies.Count == 0 && player.Character is not Fool)
            return null;
        _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.FoolMelodiesRuleId,
            "Fool Melodies",
            MacroMechanismReader.FoolMelodiesRule(),
            CreatureHolderKey(actor)));
        return melodies.ToImmutable();
    }

    private OrbQueueView ObserveOrbs(int capacity, IReadOnlyList<OrbModel> nativeOrbs, Creature holder)
    {
        var holderKey = CreatureHolderKey(holder);
        var orbs = ImmutableArray.CreateBuilder<OrbView>();
        foreach (var orb in nativeOrbs)
        {
            var orbTitle = orb.Title.GetFormattedText();
            var orbId = orb.Id.ToString();
            orbs.Add(new OrbView(orbTitle, orbId, orb.PassiveVal, orb.EvokeVal));
            AddHoverTipMaterial(holderKey, orbTitle, orbId, orb.HoverTips);
            if (ModifierMechanismReader.ReadOrb(orb, gameLanguage) is { } orbRule)
            {
                _mechanisms.Add(new MechanismView(
                    MechanismKind.Other,
                    orbId,
                    orbTitle,
                    orbRule,
                    holderKey));
            }
        }
        if (capacity > 0 || nativeOrbs.Count > 0)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                MacroMechanismReader.OrbQueueRuleId,
                "Orb Queue",
                MacroMechanismReader.OrbQueueRule(),
                holderKey));
        }
        return new OrbQueueView(capacity, orbs.ToImmutable());
    }

    private SelectionView? ObserveCombatSelection(CardSelectionInterceptor.PendingCardSelection? pending)
    {
        if (pending is null)
            return null;

        if (pending.Upgrade)
        {
            foreach (var card in pending.Options)
                AddAutomaticUpgrade(card);
        }

        NamedDefinition? source = pending.SourceModelId is { } id && !string.IsNullOrEmpty(pending.SourceName)
            ? new NamedDefinition(pending.SourceName, id)
            : null;

        return new SelectionView(
            pending.Prefs.Prompt.GetFormattedText(),
            source,
            pending.MinSelect,
            pending.MaxSelect,
            Refs(pending.Options),
            []);
    }

    private EnemyView ObserveEnemy(Creature creature)
    {
        var reference = registry.GetRef(creature);
        var (hp, maxHp, display) = ObserveHitPoints(creature);
        var name = CreatureName(creature);
        var powers = ObservePowers(creature);
        var move = creature.Monster?.NextMove;
        var intents = ObserveIntents(creature);
        var id = creature.ModelId.ToString();
        var moveId = move is not null && EnemyMechanismReader.IsPublicMove(move)
            ? move.StateId
            : null;

        _objects.Add(reference, new EnemyEntry(name, id, hp, maxHp, creature.Block, display, powers, intents)
        {
            NextMoveId = moveId,
        });
        _descriptions.Add(reference, null);
        return new EnemyView(reference, name, id, hp, maxHp, creature.Block, display, powers, intents)
        {
            NextMoveId = moveId,
        };
    }

    private AllyView ObserveAlly(Creature creature)
    {
        var reference = registry.GetRef(creature);
        var (hp, maxHp, display) = ObserveHitPoints(creature);
        var name = CreatureName(creature);
        var powers = ObservePowers(creature);
        var id = creature.ModelId.ToString();

        _objects.Add(reference, new AllyEntry(name, id, hp, maxHp, creature.Block, display, powers, creature.IsAlive));
        _descriptions.Add(reference, null);
        if (creature.Monster is MegaCrit.Sts2.Core.Models.Monsters.Osty)
            AddOstyMacros(reference, name);
        return new AllyView(reference, name, id, hp, maxHp, creature.Block, display, powers, creature.IsAlive);
    }

    private void AddRegentStarsMacro(string owner)
        => _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.RegentStarsRuleId,
            "Stars",
            MacroMechanismReader.RegentStarsRule(),
            owner));

    private void AddForgeMacro(string owner)
        => _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.ForgeSovereignBladeRuleId,
            "Forge",
            MacroMechanismReader.ForgeSovereignBladeRule(),
            owner));

    private void AddSlyMacro(string owner)
        => _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.SlyRuleId,
            "Sly",
            MacroMechanismReader.SlyRule(),
            owner));

    private void AddOstyMacros(string owner, string? ostyName = null)
    {
        var label = ostyName
            ?? ModelDb.GetByIdOrNull<MonsterModel>(ModelDb.GetId(typeof(MegaCrit.Sts2.Core.Models.Monsters.Osty)))?.Title.GetFormattedText()
            ?? "Osty";
        _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.OstyRoutingId,
            label,
            MacroMechanismReader.OstyDamageRoutingRule(),
            owner));
        _mechanisms.Add(new MechanismView(
            MechanismKind.Other,
            MacroMechanismReader.OstySummonId,
            label,
            MacroMechanismReader.OstySummonRule(),
            owner));
    }

    private ImmutableArray<PublicIntent> ObserveIntents(Creature creature)
    {
        if (creature.Monster is not { } monster || monster.NextMove?.Intents is not { } nativeIntents)
            return [];

        var targets = combat!.PlayerCreatures.Where(target => target.IsAlive).ToList();
        return nativeIntents.Select(intent =>
        {
            if (intent.IntentType is IntentType.Hidden or IntentType.Unknown)
                return new PublicIntent("unknown");

            string? hoverTitle = null;
            string? hoverDescription = null;
            try
            {
                var hover = intent.GetHoverTip(targets, creature);
                hoverTitle = hover.Title;
                hoverDescription = hover.Description;
            }
            catch (Exception)
            {
                // Transient teardown hook errors leave optional hover text null.
            }

            int? damagePerHit = null;
            if (intent is AttackIntent attack)
            {
                try
                {
                    damagePerHit = attack.GetSingleDamage(targets, creature);
                }
                catch (Exception)
                {
                    damagePerHit = null;
                }
            }

            return new PublicIntent(intent.IntentType.ToString().ToLowerInvariant())
            {
                Name = hoverTitle,
                Description = hoverDescription,
                DamagePerHit = damagePerHit,
                Hits = intent is AttackIntent repeated ? repeated.Repeats : null,
                Amount = intent is StatusIntent status ? status.CardCount : null,
            };
        }).ToImmutableArray();
    }

    private ImmutableArray<PublicPower> ObservePowers(Creature creature)
    {
        if (_powers.TryGetValue(creature, out var existing))
            return existing;

        var holderKey = CreatureHolderKey(creature);
        var result = creature.Powers
            .Where(power => power.IsVisible)
            .Select(power =>
            {
                var title = power.Title.GetFormattedText();
                _pendingPowerRules.Add((holderKey, power.GetType(), title));
                AddHoverTipMaterial(holderKey, title, power.Id.ToString(), power.HoverTips);
                if (ItemMechanismReader.UsesStars(power))
                    AddRegentStarsMacro(holderKey);
                if (ItemMechanismReader.UsesForge(power))
                    AddForgeMacro(holderKey);
                return new PublicPower(
                    title,
                    power.Id.ToString(),
                    power.Amount,
                    power.DisplayAmount,
                    power.TypeForCurrentAmount.ToString().ToLowerInvariant());
            })
            .ToImmutableArray();

        _powers.Add(creature, result);
        return result;
    }

    private void AddHoverTipMaterial(string owner, string name, string id, IEnumerable<IHoverTip> tips)
    {
        var generated = ImmutableArray.CreateBuilder<GeneratedCard>();
        foreach (var tip in tips)
        {
            if (tip is CardHoverTip generatedCard)
            {
                generated.Add(new GeneratedCard(
                    new NamedDefinition(name, id),
                    CardPreviewReader.ReadDefinitionPreview(generatedCard.Card)));
                continue;
            }

            if (tip is not HoverTip text || string.IsNullOrEmpty(text.Title))
                continue;

            if (text.CanonicalModel is PowerModel canonicalPower)
                _pendingPowerRules.Add((owner, canonicalPower.GetType(), text.Title));
            else if (text.CanonicalModel is EnchantmentModel canonicalEnchantment
                && ModifierMechanismReader.ReadEnchantment(canonicalEnchantment, gameLanguage) is { } tipEnchRule)
            {
                _mechanisms.Add(new MechanismView(
                    MechanismKind.Other,
                    canonicalEnchantment.Id.ToString(),
                    text.Title,
                    tipEnchRule,
                    owner));
            }
            else if (text.CanonicalModel is AfflictionModel canonicalAffliction
                && ModifierMechanismReader.ReadAffliction(canonicalAffliction, gameLanguage) is { } tipAffRule)
            {
                _mechanisms.Add(new MechanismView(
                    MechanismKind.Other,
                    canonicalAffliction.Id.ToString(),
                    text.Title,
                    tipAffRule,
                    owner));
            }
            else if (text.CanonicalModel is OrbModel canonicalOrb
                && ModifierMechanismReader.ReadOrb(canonicalOrb, gameLanguage) is { } tipOrbRule)
            {
                _mechanisms.Add(new MechanismView(
                    MechanismKind.Other,
                    canonicalOrb.Id.ToString(),
                    text.Title,
                    tipOrbRule,
                    owner));
            }

            var mechanismKind = text.CanonicalModel switch
            {
                PowerModel => MechanismKind.Power,
                OrbModel => MechanismKind.Orb,
                EnchantmentModel => MechanismKind.Enchantment,
                AfflictionModel => MechanismKind.Affliction,
                RelicModel => MechanismKind.Relic,
                PotionModel => MechanismKind.Potion,
                _ => MechanismKind.Keyword,
            };
            _mechanisms.Add(new MechanismView(
                mechanismKind,
                text.CanonicalModel?.Id.ToString(),
                text.Title,
                text.Description,
                owner));
        }

        _generated[owner] = _generated.TryGetValue(owner, out var previous)
            ? previous.AddRange(generated)
            : generated.ToImmutable();
    }

    internal const string ActMechanismOwner = "act";

    private void FlushPowerMechanisms()
    {
        var includeMultiplayer = run.Players.Count > 1;
        foreach (var (owner, powerType, fallbackName) in _pendingPowerRules)
        {
            string powerId;
            try
            {
                powerId = ModelDb.GetId(powerType).ToString();
            }
            catch
            {
                continue;
            }
            if (_inlineEnemyPowerIds.Contains(powerId) || !_emittedPowerRules.Add((owner, powerId)))
                continue;
            if (PowerMechanismReader.Read(powerType, gameLanguage, includeMultiplayer) is not { } ruleText)
                continue;

            string canonicalTitle;
            try
            {
                canonicalTitle = ModelDb.GetByIdOrNull<PowerModel>(ModelDb.GetId(powerType))?.Title.GetFormattedText() ?? fallbackName;
            }
            catch
            {
                canonicalTitle = fallbackName;
            }

            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                powerId,
                canonicalTitle,
                ruleText,
                owner));
        }
    }

    private bool IsEventSurfaceActive() =>
        run.CurrentRoom?.RoomType is RoomType.Event
        && NEventRoom.Instance is { } eventRoom
        && GodotObject.IsInstanceValid(eventRoom)
        && eventRoom.IsInsideTree()
        && RunManager.Instance.EventSynchronizer.Events.Count > 0
        && NMapScreen.Instance is not { IsOpen: true, IsTravelEnabled: true };

    private SelectionView? ObserveRunSelection()
    {
        if (CardSelectionInterceptor.GetPending() is { } pending)
            return ObserveCombatSelection(pending);

        var overlay = NOverlayStack.Instance?.Peek();
        if (overlay is NChooseABundleSelectionScreen bundleScreen)
        {
            var bundles = bundleScreen.GetNode<Control>("%BundleRow").GetChildren().OfType<NCardBundle>().ToArray();
            foreach (var bundle in bundles)
            {
                foreach (var card in bundle.Bundle)
                    AddAutomaticUpgrade(card);
                var reference = registry.GetRef(bundle.Bundle);
                var title = string.Join(", ", bundle.Bundle.Select(card => card.Title));
                _objects[reference] = new BundleEntry(title, Refs(bundle.Bundle));
                _descriptions[reference] = null;
            }

            var prompt = new LocString("gameplay_ui", "CHOOSE_A_PACK").GetFormattedText();
            var skipButton = bundleScreen.GetNodeOrNull<NClickableControl>("%SkipButton");
            var canSkip = typeof(NChooseABundleSelectionScreen)
                              .GetField("_canSkip", BindingFlags.Instance | BindingFlags.NonPublic)
                              ?.GetValue(bundleScreen) is true
                          && skipButton is { IsEnabled: true }
                          && skipButton.IsVisibleInTree();
            return new SelectionView(
                prompt,
                null,
                canSkip ? 0 : 1,
                1,
                bundles.Select(bundle => registry.GetRef(bundle.Bundle)).ToImmutableArray(),
                []);
        }

        if (overlay is NChooseACardSelectionScreen cardChoice)
        {
            if (!RunSurfaceLifecycle.TryGetCardChoice(cardChoice, out var choice))
                throw new InvalidOperationException("Native card choice was not registered before publication.");
            foreach (var card in choice.Cards)
                AddAutomaticUpgrade(card);
            return new SelectionView(
                new LocString("gameplay_ui", "CHOOSE_CARD_HEADER").GetFormattedText(),
                null,
                choice.CanSkip ? 0 : 1,
                1,
                Refs(choice.Cards),
                []);
        }

        if (overlay is not NCardGridSelectionScreen grid)
            return null;

        var cards = typeof(NCardGridSelectionScreen)
            .GetField("_cards", BindingFlags.Instance | BindingFlags.NonPublic)
            ?.GetValue(grid) as IReadOnlyList<CardModel>
            ?? throw new InvalidOperationException("Native selector candidates are unavailable.");
        if (grid.GetType()
                .GetField("_prefs", BindingFlags.Instance | BindingFlags.NonPublic)
                ?.GetValue(grid) is not CardSelectorPrefs prefs)
        {
            throw new InvalidOperationException("Native selector preferences are unavailable.");
        }

        var selected = grid.GetType()
            .GetField("_selectedCards", BindingFlags.Instance | BindingFlags.NonPublic)
            ?.GetValue(grid) as IEnumerable<CardModel>;

        foreach (var card in cards)
            AddAutomaticUpgrade(card);

        // The enchant preview never confirms an empty selection. Only Close can return one.
        var minSelect = grid is NDeckEnchantSelectScreen enchant
                        && prefs.MinSelect == 0
                        && NodeHierarchy.FindEnchantCloseButton(enchant) is null
            ? 1
            : prefs.MinSelect;
        return new SelectionView(
            prefs.Prompt.GetFormattedText(),
            null,
            minSelect,
            prefs.MaxSelect,
            Refs(cards),
            selected is null ? [] : Refs(selected));
    }

    private void ObserveRestSite(NRestSiteRoom rest)
    {
        var liveButtons = NodeHierarchy.Descendants(rest)
            .OfType<NRestSiteButton>()
            .Where(button => GodotObject.IsInstanceValid(button) && !button.IsQueuedForDeletion())
            .ToArray();
        foreach (var option in rest.Options)
        {
            var reference = registry.GetRef(option);
            var title = option.Title.GetFormattedText();
            _objects[reference] = new OptionEntry(title);
            _descriptions[reference] = option.Description.GetFormattedText();
            if (liveButtons.FirstOrDefault(candidate => ReferenceEquals(candidate.Option, option)) is { } button)
                AddInteraction(button, title, option.IsEnabled && button.IsEnabled, reference);
        }
        ObserveButton(rest.ProceedButton, operation: InteractionOperation.Proceed);
    }

    private bool ObserveTreasureRoom()
    {
        if (Engine.GetMainLoop() is not SceneTree tree)
            return false;

        var treasure = tree.Root.GetNodeOrNull<NTreasureRoom>("/root/Game/RootSceneContainer/Run/RoomContainer/TreasureRoom");
        if (treasure is null)
            return false;

        foreach (var holder in NodeHierarchy.Descendants(treasure).OfType<NTreasureRoomRelicHolder>())
        {
            if (!holder.IsVisibleInTree() || RunSurfaceLifecycle.GetInitializedTreasureRelic(holder) is not { } relic)
                continue;
            AddRelic(relic);
            AddInteraction(holder, relic.Title.GetFormattedText(), holder.IsEnabled, registry.GetRef(relic));
        }

        if (treasure.GetNodeOrNull<NClickableControl>("Chest") is { } chest)
            ObserveButton(chest, operation: InteractionOperation.OpenChest);
        ObserveButton(treasure.ProceedButton, operation: InteractionOperation.Proceed);
        return true;
    }

    private void ObserveRewards(NRewardsScreen screen, bool active)
    {
        foreach (var button in NodeHierarchy.Descendants(screen.GetNode<Control>("%RewardsContainer")).OfType<NRewardButton>())
        {
            if (button.Reward is not { } reward || (active && !button.IsVisibleInTree()))
                continue;

            var reference = registry.GetRef(reward);
            var contents = ImmutableArray<string>.Empty;
            OfferKind kind;
            int? amount = null;

            switch (reward)
            {
                case CardReward cards:
                    foreach (var card in cards.Cards)
                        AddAutomaticUpgrade(card);
                    contents = Refs(cards.Cards);
                    kind = OfferKind.Card;
                    break;
                case SpecialCardReward special:
                    var specialCard = RunSurfaceLifecycle.GetSpecialRewardCard(special)
                        ?? throw new InvalidOperationException("Native special reward card is unavailable.");
                    AddAutomaticUpgrade(specialCard);
                    contents = [registry.GetRef(specialCard)];
                    kind = OfferKind.Card;
                    break;
                case PotionReward potion when potion.Potion is { } model:
                    AddPotion(model);
                    contents = [registry.GetRef(model)];
                    kind = OfferKind.Potion;
                    break;
                case RelicReward relic when relic.Relic is { } model:
                    AddRelic(model);
                    contents = [registry.GetRef(model)];
                    kind = OfferKind.Relic;
                    break;
                case GoldReward gold:
                    kind = OfferKind.Gold;
                    amount = gold.Amount;
                    break;
                case CardRemovalReward or CardUpgradeReward:
                    kind = OfferKind.Service;
                    break;
                default:
                    throw new InvalidOperationException("The native reward has no supported public content.");
            }

            var label = reward.Description.GetFormattedText();
            _objects[reference] = new OfferEntry(label);
            _descriptions[reference] = label;
            var action = AddInteraction(button, label, active && button.IsEnabled, reference);
            _offers.Add(new OfferView(reference, kind, contents, [action])
            {
                Amount = amount,
                RequiresSelection = kind == OfferKind.Card
                    ? reward is CardReward
                    : reward is CardRemovalReward or CardUpgradeReward
                        ? true
                        : null,
                CanReroll = reward is CardReward { CanReroll: true } ? true : null,
            });
        }

        if (active && screen.GetNodeOrNull<NProceedButton>("ProceedButton") is { } proceed)
            ObserveButton(proceed, operation: InteractionOperation.Proceed);
    }

    private string? ObserveCardRewards(NCardRewardSelectionScreen screen)
    {
        var reference = registry.GetRef(screen);
        var cards = screen.GetNode<Control>("UI/CardRow").GetChildren().OfType<NCardHolder>()
            .Where(holder => !holder.IsQueuedForDeletion() && holder.CardModel is not null)
            .ToArray();

        var actions = ImmutableArray.CreateBuilder<string>();
        foreach (var holder in cards)
        {
            var card = holder.CardModel!;
            AddAutomaticUpgrade(card);
            actions.Add(AddInteraction(
                holder,
                card.Title,
                holder.IsVisibleInTree() && holder.Hitbox.IsEnabled,
                registry.GetRef(card)));
        }

        foreach (var button in screen.GetNode<Control>("UI/RewardAlternatives").GetChildren().OfType<NCardRewardAlternativeButton>())
            ObserveButton(button, reference);

        actions.AddRange(_interactions.Where(action => action.Subject == reference).Select(action => action.Ref));

        var parentScreens = NOverlayStack.Instance is { } overlays
            ? overlays.GetChildren().OfType<NRewardsScreen>().ToArray()
            : [];
        var matchingParents = parentScreens
            .SelectMany(parent => NodeHierarchy.Descendants(parent.GetNode<Control>("%RewardsContainer")).OfType<NRewardButton>())
            .Select(button => button.Reward)
            .OfType<CardReward>()
            .Where(reward => ReferenceEquals(RunSurfaceLifecycle.GetCardRewardScreen(reward), screen))
            .Distinct(ReferenceEqualityComparer.Instance)
            .Cast<CardReward>()
            .ToArray();
        var parentOfferRef = matchingParents.Length == 1
            ? registry.GetRef(matchingParents[0])
            : null;

        var title = new LocString("gameplay_ui", "CHOOSE_CARD_HEADER").GetFormattedText();
        _objects[reference] = new OfferEntry(title);
        _descriptions[reference] = title;
        _offers.Add(new OfferView(reference, OfferKind.Card, Refs(cards.Select(holder => holder.CardModel!)), actions.ToImmutable())
        {
            ParentOfferRef = parentOfferRef,
            RequiresSelection = true,
            CanReroll = NodeHierarchy.FindRewardRerollButton(screen) is not null ? true : null,
        });

        foreach (var parent in parentScreens)
            ObserveRewards(parent, active: false);

        return matchingParents.Length switch
        {
            1 => parentOfferRef,
            0 => reference,
            _ => null,
        };
    }

    private SelectionView ObserveCardRewardSelection(NCardRewardSelectionScreen screen, string? activeOfferRef)
    {
        if (!RunSurfaceLifecycle.TryGetRewardChoice(screen, out var choice))
            throw new InvalidOperationException("Native reward choice was not registered before publication.");

        var canSkip = NodeHierarchy.FindRewardDeclineButton(screen) is not null;

        return new SelectionView(
            new LocString("gameplay_ui", "CHOOSE_CARD_HEADER").GetFormattedText(),
            null,
            canSkip ? 0 : 1,
            1,
            Refs(choice.Cards.Select(result => result.Card)),
            [])
        {
            OfferRef = activeOfferRef,
        };
    }

    private void ObserveShop(NMerchantInventory inventory)
    {
        var shopSettling = ShopInputProbe.IsShopSettling(run);
        if (inventory.IsOpen)
        {
            foreach (var slot in inventory.GetAllSlots())
            {
                if (slot.IsQueuedForDeletion() || !slot.IsVisibleInTree() || slot.Entry is not { } entry)
                    continue;

                var reference = registry.GetRef(entry);
                var kind = OfferKind.Service;
                string? name;
                string? description = null;
                var contents = ImmutableArray<string>.Empty;

                switch (entry)
                {
                    case MerchantCardEntry or MerchantPotionEntry or MerchantRelicEntry when !entry.IsStocked:
                        name = NativeInteractionLabels.SoldOut(gameLanguage);
                        kind = entry switch
                        {
                            MerchantCardEntry => OfferKind.Card,
                            MerchantPotionEntry => OfferKind.Potion,
                            _ => OfferKind.Relic,
                        };
                        break;
                    case MerchantCardEntry card when card.CreationResult?.Card is { } model:
                        AddAutomaticUpgrade(model);
                        contents = [registry.GetRef(model)];
                        name = model.Title;
                        kind = OfferKind.Card;
                        break;
                    case MerchantPotionEntry potion when potion.Model is { } model:
                        AddPotion(model);
                        contents = [registry.GetRef(model)];
                        name = model.Title.GetFormattedText();
                        kind = OfferKind.Potion;
                        break;
                    case MerchantRelicEntry relic when relic.Model is { } model:
                        AddRelic(model);
                        contents = [registry.GetRef(model)];
                        name = model.Title.GetFormattedText();
                        kind = OfferKind.Relic;
                        break;
                    case MerchantCardRemovalEntry removalEntry:
                        var removalTitle = new LocString("merchant_room", "MERCHANT.cardRemovalService.title").GetFormattedText();
                        name = string.IsNullOrWhiteSpace(removalTitle)
                            ? NativeInteractionLabels.CardRemovalService(gameLanguage)
                            : removalTitle;
                        var removalDesc = new LocString("merchant_room", "MERCHANT.cardRemovalService.description");
                        removalDesc.Add("Amount", MerchantCardRemovalEntry.PriceIncrease);
                        description = removalDesc.GetFormattedText();
                        _mechanisms.Add(new MechanismView(
                            MechanismKind.Other,
                            MacroMechanismReader.ShopRemovalId,
                            name,
                            MacroMechanismReader.ReadShopRemoval(run.Players.Single(), removalEntry),
                            reference));
                        break;
                    default:
                        throw new InvalidOperationException("The native merchant entry has no supported public content.");
                }

                _objects[reference] = new OfferEntry(name);
                _descriptions[reference] = description;
                var action = AddInteraction(
                    slot.Hitbox,
                    name,
                    !shopSettling && entry.IsStocked && entry.EnoughGold && slot.Hitbox.IsEnabled,
                    reference,
                    gold: entry.Cost);
                _offers.Add(new OfferView(reference, kind, contents, [action])
                {
                    Price = entry.Cost,
                    Stocked = entry.IsStocked,
                    OnSale = entry is MerchantCardEntry { IsOnSale: true },
                });
            }
        }

        if (NMerchantRoom.Instance is { } room && ReferenceEquals(room.Inventory, inventory) && ShopInputProbe.IsCurrent(room))
        {
            foreach (var back in NodeHierarchy.Descendants(inventory).OfType<NBackButton>().Where(ShopInputProbe.IsVisible))
            {
                AddInteraction(
                    back,
                    DisplayedButtonLabel(back) ?? NativeInteractionLabels.Operation(gameLanguage, InteractionOperation.CloseShop),
                    ShopInputProbe.CanClose(room),
                    operation: InteractionOperation.CloseShop);
            }
            if (ShopInputProbe.IsVisible(room.MerchantButton))
            {
                AddInteraction(
                    room.MerchantButton,
                    DisplayedButtonLabel(room.MerchantButton) ?? NativeInteractionLabels.Operation(gameLanguage, InteractionOperation.OpenShop),
                    ShopInputProbe.CanOpen(room),
                    operation: InteractionOperation.OpenShop);
            }
            if (ShopInputProbe.IsVisible(room.ProceedButton))
            {
                AddInteraction(
                    room.ProceedButton,
                    DisplayedButtonLabel(room.ProceedButton) ?? NativeInteractionLabels.Operation(gameLanguage, InteractionOperation.LeaveShop),
                    ShopInputProbe.CanLeave(room),
                    operation: InteractionOperation.LeaveShop);
            }
            return;
        }

        foreach (var back in NodeHierarchy.Descendants(inventory).OfType<NBackButton>().Where(ShopInputProbe.IsVisible))
        {
            AddInteraction(
                back,
                DisplayedButtonLabel(back) ?? NativeInteractionLabels.Operation(gameLanguage, InteractionOperation.CloseShop),
                !shopSettling && back.IsEnabled,
                operation: InteractionOperation.CloseShop);
        }

        if (NMerchantRoom.Instance is { } otherRoom)
        {
            ObserveButton(otherRoom.MerchantButton, operation: InteractionOperation.OpenShop);
            ObserveButton(otherRoom.ProceedButton, operation: InteractionOperation.LeaveShop);
        }
    }

    private EventView? ObserveEvent(bool publishControls)
    {
        var sync = RunManager.Instance.EventSynchronizer;
        if (sync.Events.Count == 0)
            return null;

        var native = sync.GetLocalEvent();
        var reference = registry.GetRef(native);
        var name = native.Title.GetFormattedText();
        var ancient = native is AncientEventModel || NEventRoom.Instance?.Layout is NAncientEventLayout;

        _objects[reference] = new EventEntry(name, ancient, native.Id.ToString());
        _descriptions[reference] = ancient ? ReadAncientDialogue() : native.Description?.GetFormattedText();

        var view = new EventView(reference, name, ancient)
        {
            DefinitionId = native.Id.ToString(),
        };

        if (!publishControls)
            return view;

        var visibleOptions = new List<(string Title, MegaCrit.Sts2.Core.Events.EventOption Option)>();
        if (NEventRoom.Instance?.Layout is { } layout)
        {
            foreach (var button in layout.OptionButtons)
            {
                if (!button.IsVisibleInTree())
                    continue;
                var option = button.Option;
                var optionRef = registry.GetRef(option);
                var title = option.Title.GetFormattedText();
                visibleOptions.Add((title, option));
                _objects[optionRef] = new OptionEntry(title);
                _descriptions[optionRef] = option.Description.GetFormattedText();
                AddHoverTipMaterial(optionRef, title, native.Id.ToString(), option.HoverTips);
                AddInteraction(
                    button,
                    title,
                    button.IsEnabled && !option.IsLocked && !option.WasChosen,
                    optionRef,
                    operation: native.IsFinished && option.IsProceed ? InteractionOperation.Proceed : null);
            }
        }

        var (eventRule, eventCompleteness) = EventMechanismReader.ReadEventWithCompleteness(
            native, visibleOptions, NEventRoom.Instance?.CustomEventNode is NFakeMerchant);
        if (eventRule is not null || eventCompleteness == MechanismCompleteness.Unavailable)
        {
            _mechanisms.Add(new MechanismView(
                MechanismKind.Other,
                native.Id.ToString(),
                name,
                eventRule,
                reference)
            {
                Completeness = eventCompleteness == MechanismCompleteness.Complete ? null : eventCompleteness,
            });
        }

        if (NEventRoom.Instance?.CustomEventNode is not NFakeMerchant
            && !native.IsFinished
            && native.CurrentOptions.Count == 0
            && NEventRoom.Instance?.Layout is NAncientEventLayout ancientLayout
            && ancientLayout.GetNodeOrNull<NAncientDialogueHitbox>("%DialogueHitbox") is { IsEnabled: true } hitbox
            && hitbox.IsVisibleInTree())
        {
            ObserveButton(hitbox, reference, operation: InteractionOperation.Proceed);
        }

        if (NEventRoom.Instance?.CustomEventNode is NFakeMerchant fake)
        {
            ObserveButton(fake.MerchantButton, reference, operation: InteractionOperation.OpenShop);
            foreach (var proceed in NodeHierarchy.Descendants(fake).OfType<NProceedButton>())
                ObserveButton(proceed, reference, operation: InteractionOperation.LeaveShop);
        }
        else if (NEventRoom.Instance?.CustomEventNode is Node custom)
        {
            foreach (var button in NodeHierarchy.Descendants(custom).OfType<NClickableControl>())
                ObserveButton(button, reference);
        }

        return view;
    }

    private void ObserveCrystalSphere(NCrystalSphereScreen screen)
    {
        var entity = typeof(NCrystalSphereScreen)
            .GetField("_entity", BindingFlags.Instance | BindingFlags.NonPublic)
            ?.GetValue(screen) as CrystalSphereMinigame
            ?? throw new InvalidOperationException("Native crystal sphere state is unavailable.");

        foreach (var cell in screen.GetNode<Control>("%Cells").GetChildren().OfType<NCrystalSphereCell>())
        {
            if (!cell.IsVisibleInTree())
                continue;
            AddInteraction(
                cell,
                $"({cell.Entity.X}, {cell.Entity.Y})",
                cell.IsEnabled && cell.Entity.IsHidden && entity.DivinationCount > 0);
        }

        var bigButton = screen.GetNode<NClickableControl>("%BigDivinationButton");
        if (!bigButton.IsQueuedForDeletion() && bigButton.IsVisibleInTree())
        {
            var bigLabel = DisplayedButtonLabel(bigButton)
                           ?? new LocString("events", "CRYSTAL_SPHERE.button.DIVINATION_LABEL_BIG").GetFormattedText();
            AddInteraction(
                bigButton,
                bigLabel,
                bigButton.IsEnabled
                && entity.DivinationCount > 0
                && entity.CrystalSphereTool != CrystalSphereMinigame.CrystalSphereToolType.Big);
        }

        var smallButton = screen.GetNode<NClickableControl>("%SmallDivinationButton");
        if (!smallButton.IsQueuedForDeletion() && smallButton.IsVisibleInTree())
        {
            var smallLabel = DisplayedButtonLabel(smallButton)
                             ?? new LocString("events", "CRYSTAL_SPHERE.button.DIVINATION_LABEL_SMALL").GetFormattedText();
            AddInteraction(
                smallButton,
                smallLabel,
                smallButton.IsEnabled
                && entity.DivinationCount > 0
                && entity.CrystalSphereTool != CrystalSphereMinigame.CrystalSphereToolType.Small);
        }

        ObserveButton(screen.GetNode<NClickableControl>("%ProceedButton"), operation: InteractionOperation.Proceed);
    }

    private void ObserveMapInteractions(NMapScreen map)
    {
        foreach (var point in NodeHierarchy.Descendants(map).OfType<NMapPoint>())
        {
            if (!point.IsVisibleInTree())
                continue;
            var destination = MapObserver.NodePosition(point.Point.coord);
            AddInteraction(
                point,
                DisplayedButtonLabel(point) ?? NativeInteractionLabels.Travel(gameLanguage, destination),
                map.IsTravelEnabled && point.State == MapPointState.Travelable,
                destination: destination);
        }
    }

    private string AddInteraction(
        object native,
        string label,
        bool enabled,
        string? subject = null,
        int? gold = null,
        string? destination = null,
        InteractionOperation? operation = null)
    {
        var reference = RunSurfaceLifecycle.GetInteractionRef(native);
        _interactions.RemoveAll(action => action.Ref == reference);
        _interactions.Add(new PublicInteraction(reference, label, enabled)
        {
            Subject = subject,
            Destination = destination,
            Operation = operation,
            Cost = gold is { } cost ? new InteractionCost(Gold: cost) : null,
        });
        return reference;
    }

    private void ObserveButton(
        NClickableControl button,
        string? subject = null,
        string? fallbackLabel = null,
        InteractionOperation? operation = null)
    {
        if (button.IsQueuedForDeletion() || !button.IsVisibleInTree())
            return;

        var label = DisplayedButtonLabel(button)
                    ?? (operation is { } kind ? NativeInteractionLabels.Operation(gameLanguage, kind) : fallbackLabel);
        if (label is null)
            return;

        AddInteraction(button, label, button.IsEnabled, subject, operation: operation);
    }

    private static string? DisplayedButtonLabel(NClickableControl button)
    {
        const BindingFlags PrivateInstance = BindingFlags.Instance | BindingFlags.NonPublic;
        var label = button switch
        {
            NBackButton or NMerchantButton or NMapPoint or NTreasureButton => null,
            NProceedButton => (typeof(NProceedButton).GetField("_label", PrivateInstance)?.GetValue(button) as MegaLabel)?.Text,
            NAncientDialogueHitbox => NEventRoom.Instance?.Layout is NAncientEventLayout ancientLayout
                ? (typeof(NAncientEventLayout).GetField("_fakeNextButtonLabel", PrivateInstance)?.GetValue(ancientLayout) as MegaLabel)?.Text
                : null,
            _ => NodeHierarchy.Descendants(button).OfType<RichTextLabel>().Select(text => text.Text)
                .Concat(NodeHierarchy.Descendants(button).OfType<Label>().Select(text => text.Text))
                .FirstOrDefault(text => !string.IsNullOrEmpty(text)),
        };

        if (label is null && button is NCardRewardAlternativeButton)
        {
            label = typeof(NCardRewardAlternativeButton)
                .GetField("_optionName", PrivateInstance)
                ?.GetValue(button) as string;
        }

        return string.IsNullOrEmpty(label) ? null : label;
    }

    private static string? ReadAncientDialogue()
    {
        if (NEventRoom.Instance?.Layout is not NAncientEventLayout layout)
            return null;

        const BindingFlags PrivateInstance = BindingFlags.Instance | BindingFlags.NonPublic;
        var page = (int)(typeof(NAncientEventLayout).GetField("_currentDialogueLine", PrivateInstance)?.GetValue(layout) ?? -1);
        var lines = typeof(NAncientEventLayout).GetField("_dialogue", PrivateInstance)?.GetValue(layout) as IReadOnlyList<AncientDialogueLine>;
        if (lines is null || page < 0 || page >= lines.Count)
            return null;
        return lines[page].LineText?.GetFormattedText();
    }

    private ImmutableArray<string> Refs(IEnumerable<CardModel> cards) =>
        cards.Select(registry.GetRef).ToImmutableArray();

    private ImmutableArray<string> SortedRefs(IEnumerable<CardModel> cards) =>
        cards.Select(registry.GetRef).Order(StringComparer.Ordinal).ToImmutableArray();

    private string CreatureHolderKey(Creature creature) =>
        creature.Player is not null ? "player" : registry.GetRef(creature);

    private static string CreatureName(Creature creature) =>
        creature.Monster?.Title.GetFormattedText() ?? creature.Player!.Character.Title.GetFormattedText();

    private static (int? Hp, int? MaxHp, HpDisplay Display) ObserveHitPoints(Creature creature) =>
        creature.HpDisplay switch
        {
            MegaCrit.Sts2.Core.Entities.Creatures.HpDisplay.Normal => (creature.CurrentHp, creature.MaxHp, HpDisplay.Numeric),
            MegaCrit.Sts2.Core.Entities.Creatures.HpDisplay.InfiniteWithNumbers => (creature.CurrentHp, creature.MaxHp, HpDisplay.Infinite),
            MegaCrit.Sts2.Core.Entities.Creatures.HpDisplay.InfiniteWithoutNumbers => (null, null, HpDisplay.Infinite),
            _ => (null, null, HpDisplay.Unknown),
        };
}
