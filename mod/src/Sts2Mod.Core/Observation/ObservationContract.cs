using System.Collections.Immutable;
using System.Text.Json;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Developer;

namespace Sts2Mod.Core.Observation;

public enum ReadOutcome
{
    Completed,
    Rejected,
    Failed,
}

public enum NativeReadError
{
    NoRun,
    ReadUnavailable,
    IdentityUnregistered,
}

public enum NativeInputReadiness
{
    Ready,
    Selection,
    Busy,
    Terminal,
    Unavailable,
}

public enum PublicInputControl
{
    Ready,
    Busy,
    Unavailable,
}

public enum ObservationOwner
{
    Run,
    Combat,
}

public enum ObservationPhase
{
    Run,
    Combat,
    Terminal,
}

public enum RunScreen
{
    Map,
    Rewards,
    Shop,
    Event,
    Rest,
    Treasure,
    Selection,
    Transition,
    Other,
}

public enum CombatScreen
{
    Combat,
    CardSelection,
    Transition,
    Other,
}

public enum HpDisplay
{
    Numeric,
    Infinite,
    Unknown,
}

public enum UpgradeAvailability
{
    Available,
    NotUpgradable,
    Unavailable,
}

public enum MechanismKind
{
    Keyword,
    Power,
    Orb,
    Enchantment,
    Affliction,
    Relic,
    Potion,
    Other,
}

public enum InteractionOperation
{
    OpenShop,
    CloseShop,
    LeaveShop,
    OpenChest,
    Proceed,
}

public enum OfferKind
{
    Card,
    Relic,
    Potion,
    Gold,
    Service,
    Choice,
    Bundle,
}

public enum ResourceCostKind
{
    Number,
    X,
    None,
    Unknown,
}

[JsonConverter(typeof(ResourceCostConverter))]
public readonly record struct ResourceCost(ResourceCostKind Kind, int Amount = 0)
{
    public static ResourceCost FromNative(int amount, bool x) =>
        x ? new(ResourceCostKind.X)
        : amount < 0 ? new(ResourceCostKind.None)
        : new(ResourceCostKind.Number, amount);
}

public sealed class ResourceCostConverter : JsonConverter<ResourceCost>
{
    public override ResourceCost Read(ref Utf8JsonReader reader, Type type, JsonSerializerOptions options) =>
        reader.TokenType switch
        {
            JsonTokenType.Number when reader.TryGetInt32(out var amount) && amount >= 0 => new(ResourceCostKind.Number, amount),
            JsonTokenType.Null => new(ResourceCostKind.Unknown),
            JsonTokenType.String when reader.GetString() == "X" => new(ResourceCostKind.X),
            JsonTokenType.String when reader.GetString() == "none" => new(ResourceCostKind.None),
            _ => throw new JsonException("Invalid native resource cost."),
        };

    public override void Write(Utf8JsonWriter writer, ResourceCost value, JsonSerializerOptions options)
    {
        switch (value.Kind)
        {
            case ResourceCostKind.Number:
                writer.WriteNumberValue(value.Amount);
                return;
            case ResourceCostKind.X:
                writer.WriteStringValue("X");
                return;
            case ResourceCostKind.None:
                writer.WriteStringValue("none");
                return;
            case ResourceCostKind.Unknown:
                writer.WriteNullValue();
                return;
            default:
                throw new JsonException("Unknown resource cost kind.");
        }
    }
}

public sealed record NamedDefinition(string Name, string DefinitionId);

public sealed record ResourceCosts(ResourceCost Energy, ResourceCost Stars);

public sealed record ResourceBalances(int Energy, int Stars);

public sealed record ObservationStamp(string GameInstanceId, long ObservationSequence);

public sealed record ObservationLocation(
    string GameInstanceId,
    string? RunId,
    string? NodeId,
    ObservationOwner? Owner);

public sealed record PublicProgress(
    int Act,
    int Acts,
    int Floor,
    string? Position,
    string? RoomType,
    string Name,
    string DefinitionId)
{
    public int BossesLeft { get; init; }
}

public sealed record AscensionRule(int Level, string Name, string Text);

public sealed record RunRules(int Ascension, ImmutableArray<AscensionRule> Rules);

public sealed record PublicPower(
    string Name,
    string DefinitionId,
    decimal Amount,
    int DisplayAmount,
    string Type);

public sealed record PublicIntent(string Type)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Name { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? DamagePerHit { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? Hits { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? Amount { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Description { get; init; }
}

[JsonDerivedType(typeof(CombatPlayer))]
[JsonDerivedType(typeof(RunPlayer))]
public interface IObservedPlayer;

public sealed record CombatPlayer(
    string Name,
    string DefinitionId,
    int Hp,
    int MaxHp,
    int Gold,
    int PotionSlotCount,
    int EmptyPotionSlotCount,
    int Block,
    ResourceBalances Resources,
    ImmutableArray<PublicPower> Powers) : IObservedPlayer;

public sealed record RunPlayer(
    string Name,
    string DefinitionId,
    int Hp,
    int MaxHp,
    int Gold,
    int PotionSlotCount,
    int EmptyPotionSlotCount,
    int BaseDraw,
    int BaseEnergy,
    int BaseOrbSlots) : IObservedPlayer;

public sealed record EnemyView(
    string Ref,
    string Name,
    string DefinitionId,
    int? Hp,
    int? MaxHp,
    int Block,
    HpDisplay HpDisplay,
    ImmutableArray<PublicPower> Powers,
    ImmutableArray<PublicIntent> Intents)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? NextMoveId { get; init; }
}

public sealed record AllyView(
    string Ref,
    string Name,
    string? DefinitionId,
    int? Hp,
    int? MaxHp,
    int Block,
    HpDisplay HpDisplay,
    ImmutableArray<PublicPower> Powers,
    bool Alive);

public sealed record OrbView(string Name, string DefinitionId, decimal Passive, decimal Evoke);

public sealed record OrbQueueView(int Capacity, ImmutableArray<OrbView> Slots);

public enum ArrowSlotStatus
{
    Retrieved,
    Away,
    Destroyed,
}

public sealed record ArrowSlotView(
    ArrowSlotStatus Status,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ImpaledTargetRef);

public sealed record PileMembership(
    ImmutableArray<string> Hand,
    ImmutableArray<string> Draw,
    ImmutableArray<string> Discard,
    ImmutableArray<string> Exhaust,
    ImmutableArray<string> Other);

public sealed record SelectionView(
    string Prompt,
    NamedDefinition? Source,
    int Min,
    int Max,
    ImmutableArray<string> Candidates,
    ImmutableArray<string> Selected);

public sealed record InteractionCost(
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Gold = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Hp = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Energy = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Stars = null);

public sealed record PublicInteraction(string Ref, string Label, bool Enabled)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public InteractionOperation? Operation { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Reason { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Subject { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Destination { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public InteractionCost? Cost { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<string>? Targets { get; init; }
}

[JsonDerivedType(typeof(CombatStateView))]
[JsonDerivedType(typeof(RunStateView))]
[JsonDerivedType(typeof(TerminalStateView))]
public interface IObservedState
{
    ImmutableArray<string> Relics { get; }
    ImmutableArray<string> Potions { get; }
    SelectionView? Selection { get; }
}

public sealed record OfferView(
    string Ref,
    OfferKind Kind,
    ImmutableArray<string> Contents,
    ImmutableArray<string> Actions)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public bool? RequiresSelection { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? Price { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public bool? Stocked { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public bool? OnSale { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? Amount { get; init; }
}

public sealed record EventView(string Ref, string Name, bool Ancient)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? DefinitionId { get; init; }
}

public sealed record RunStateView(
    PublicInputControl Control,
    PublicProgress Progress,
    RunPlayer Player,
    ImmutableArray<string> Relics,
    ImmutableArray<string> Potions,
    ImmutableArray<PublicInteraction> Actions,
    SelectionView? Selection,
    ObservationPhase Phase,
    RunScreen Screen,
    ImmutableArray<string> Deck,
    ImmutableArray<OfferView> Offers) : IObservedState
{
    public EventView? Event { get; init; }
    public required float CardRarityOffset { get; init; }
    public int? BattleTurns { get; init; }
}

public sealed record CombatStateView(
    PublicInputControl Control,
    PublicProgress Progress,
    CombatPlayer Player,
    ImmutableArray<string> Relics,
    ImmutableArray<string> Potions,
    ImmutableArray<PublicInteraction> Actions,
    SelectionView? Selection,
    ObservationPhase Phase,
    CombatScreen Screen,
    int Turn,
    ImmutableArray<string> Deck,
    PileMembership Piles,
    ImmutableArray<EnemyView> Enemies,
    ImmutableArray<EnemyView> InactiveEnemies) : IObservedState
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public OrbQueueView? Orbs { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<ArrowSlotView>? ArrowSlots { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<AllyView>? Allies { get; init; }
}

public sealed record CardModifier(string DefinitionId, string Name, int? Amount)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Status { get; init; }
}

public sealed record CardModifierDescriptions(string? Enchantment, string? Affliction);

public sealed record TargetTextPreview(string Target, string Text);

[JsonPolymorphic(TypeDiscriminatorPropertyName = "kind")]
[JsonDerivedType(typeof(CardEntry), "card")]
[JsonDerivedType(typeof(RelicEntry), "relic")]
[JsonDerivedType(typeof(PotionEntry), "potion")]
[JsonDerivedType(typeof(EnemyEntry), "enemy")]
[JsonDerivedType(typeof(AllyEntry), "ally")]
[JsonDerivedType(typeof(OfferEntry), "offer")]
[JsonDerivedType(typeof(EventEntry), "event")]
[JsonDerivedType(typeof(OptionEntry), "option")]
[JsonDerivedType(typeof(BundleEntry), "bundle")]
public abstract record ObservedObject;

public sealed record OfferEntry(string Name) : ObservedObject;

public sealed record EventEntry(string Name, bool Ancient, string DefinitionId) : ObservedObject;

public sealed record OptionEntry(string Name) : ObservedObject
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? DefinitionId { get; init; }
}

public sealed record BundleEntry(string Name, ImmutableArray<string> Members) : ObservedObject;

public abstract record CardFacts(
    string DefinitionId,
    string Name,
    int? Upgrades,
    string? CardType,
    string? CardPool,
    string? Rarity,
    bool? IsUpgradable,
    bool? IsRemovable,
    ImmutableArray<string> Keywords,
    bool? ShouldRetainThisTurn,
    bool? ExhaustOnNextPlay,
    bool? HasTurnEndInHandEffect,
    CardModifier? Enchantment,
    CardModifier? Affliction,
    ResourceCosts Cost) : ObservedObject
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<string>? LocalKeywords { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ResourceCosts? CostWithoutGlobalEffects { get; init; }
}

public sealed record CardEntry : CardFacts
{
    public CardEntry(
        string definitionId,
        string name,
        int? upgrades,
        string? cardType,
        string? cardPool,
        string? rarity,
        bool? isUpgradable,
        bool? isRemovable,
        ImmutableArray<string> keywords,
        bool? shouldRetainThisTurn,
        bool? exhaustOnNextPlay,
        bool? hasTurnEndInHandEffect,
        CardModifier? enchantment,
        CardModifier? affliction,
        ResourceCosts cost)
        : base(
            definitionId,
            name,
            upgrades,
            cardType,
            cardPool,
            rarity,
            isUpgradable,
            isRemovable,
            keywords,
            shouldRetainThisTurn,
            exhaustOnNextPlay,
            hasTurnEndInHandEffect,
            enchantment,
            affliction,
            cost)
    {
    }

    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public bool? Playable { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<TargetTextPreview>? Previews { get; init; }
}

public sealed record CardDefinitionPreview : CardFacts
{
    public CardDefinitionPreview(CardFacts facts, string? text)
        : base(
            facts.DefinitionId,
            facts.Name,
            facts.Upgrades,
            facts.CardType,
            facts.CardPool,
            facts.Rarity,
            facts.IsUpgradable,
            facts.IsRemovable,
            facts.Keywords,
            facts.ShouldRetainThisTurn,
            facts.ExhaustOnNextPlay,
            facts.HasTurnEndInHandEffect,
            facts.Enchantment,
            facts.Affliction,
            facts.Cost)
    {
        Text = text;
        LocalKeywords = facts.LocalKeywords;
        CostWithoutGlobalEffects = facts.CostWithoutGlobalEffects;
    }

    public string? Text { get; }
}

public sealed record RelicEntry(
    string DefinitionId,
    string Name,
    string Status,
    bool IsUsedUp,
    bool IsMelted,
    int StackCount) : ObservedObject
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public int? Counter { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public bool? UsedThisCombat { get; init; }
}

public sealed record PotionEntry(
    string DefinitionId,
    string Name,
    bool Usable,
    bool NeedsTarget,
    string TargetType,
    string Usage) : ObservedObject;

public sealed record EnemyEntry(
    string Name,
    string DefinitionId,
    int? Hp,
    int? MaxHp,
    int Block,
    HpDisplay HpDisplay,
    ImmutableArray<PublicPower> Powers,
    ImmutableArray<PublicIntent> Intents) : ObservedObject
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? NextMoveId { get; init; }
}

public sealed record AllyEntry(
    string Name,
    string? DefinitionId,
    int? Hp,
    int? MaxHp,
    int Block,
    HpDisplay HpDisplay,
    ImmutableArray<PublicPower> Powers,
    bool Alive) : ObservedObject;

[JsonDerivedType(typeof(AvailableUpgradePreview))]
[JsonDerivedType(typeof(NotUpgradablePreview))]
[JsonDerivedType(typeof(UnavailableUpgradePreview))]
public abstract record UpgradePreview
{
    private protected UpgradePreview() { }
    public abstract UpgradeAvailability Status { get; }
}

public sealed record AvailableUpgradePreview(CardDefinitionPreview Card) : UpgradePreview
{
    public override UpgradeAvailability Status => UpgradeAvailability.Available;
}

public sealed record NotUpgradablePreview : UpgradePreview
{
    public override UpgradeAvailability Status => UpgradeAvailability.NotUpgradable;
}

public sealed record UnavailableUpgradePreview : UpgradePreview
{
    public override UpgradeAvailability Status => UpgradeAvailability.Unavailable;
}

public sealed record GeneratedCard(NamedDefinition Source, CardDefinitionPreview Card);

public sealed record MechanismView(
    MechanismKind Kind,
    string? DefinitionId,
    string Name,
    string? Text,
    string Owner);

public sealed record MapOdds(
    decimal Monster,
    decimal Elite,
    decimal Event,
    decimal Shop,
    decimal Treasure);

public sealed record PublicMapNode(
    string Position,
    string RoomType,
    ImmutableArray<string> Children)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public MapOdds? Odds { get; init; }
}

public sealed record PublicMap(
    ImmutableArray<NamedDefinition> Bosses,
    ImmutableArray<PublicMapNode> Nodes)
{
    public ImmutableArray<string>? LegalDestinations { get; init; }
}

public sealed record NativeObservation(
    ObservationStamp Stamp,
    ObservationLocation Location,
    string GameLanguage,
    NativeInputReadiness InputReadiness,
    IObservedState State,
    ImmutableDictionary<string, ObservedObject> Objects,
    ImmutableDictionary<string, string?> Descriptions,
    ImmutableDictionary<string, CardModifierDescriptions> ModifierDescriptions,
    ImmutableDictionary<string, UpgradePreview> Upgrades,
    ImmutableDictionary<string, ImmutableArray<GeneratedCard>> Generated,
    ImmutableArray<MechanismView> Mechanisms,
    RunRules RunRules)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public PublicMap? Map { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public NamedDefinition? Encounter { get; init; }
}

public sealed record ReadCause(NativeReadError Code, string Message);

[JsonDerivedType(typeof(CompletedStateRead))]
[JsonDerivedType(typeof(FailedStateRead))]
public abstract record StateReadReply
{
    private protected StateReadReply() { }
    public abstract ReadOutcome Status { get; }
}

public sealed record CompletedStateRead(NativeObservation Observation) : StateReadReply
{
    public override ReadOutcome Status => ReadOutcome.Completed;
}

public sealed record FailedStateRead(ReadCause Error) : StateReadReply
{
    public override ReadOutcome Status => ReadOutcome.Failed;
}

public abstract record InspectionData;

public sealed record MapInspectionData(PublicMap Map) : InspectionData;

public sealed record HistoryInspectionData(
    ImmutableArray<NamedDefinition> CardsPlayedThisCombat,
    ImmutableArray<NamedDefinition> CardsPlayedThisTurn,
    ImmutableArray<NamedDefinition> CardsExhaustedThisTurn) : InspectionData;

public sealed record DetailInspectionData(
    ImmutableDictionary<string, ObservedObject> Objects,
    ImmutableDictionary<string, string?> Descriptions,
    ImmutableDictionary<string, CardModifierDescriptions> ModifierDescriptions,
    ImmutableDictionary<string, ImmutableArray<GeneratedCard>> Generated,
    ImmutableArray<MechanismView> Mechanisms) : InspectionData
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public ImmutableDictionary<string, UpgradePreview>? Upgrades { get; init; }
}

public sealed record NativeReadPublication(
    StateReadReply Reply,
    HistoryInspectionData? History = null,
    ImmutableArray<string> Deck = default)
{
    public CheckpointIdentityFacts? CheckpointIdentities { get; init; }
    public ImmutableDictionary<string, UpgradePreview> InspectionUpgrades { get; init; } =
        ImmutableDictionary<string, UpgradePreview>.Empty;
}
