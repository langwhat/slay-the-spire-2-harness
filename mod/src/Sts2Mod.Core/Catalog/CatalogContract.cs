using System.Collections.Immutable;
using System.Text.Json.Serialization;
using Sts2Mod.Core.Observation;

namespace Sts2Mod.Core.Catalog;

public enum CatalogMode
{
    Catalog,
    Search,
    Lookup,
}

public enum CatalogCategory
{
    Card,
    Relic,
    Potion,
    Orb,
}

public enum CatalogErrorCode
{
    InvalidRequest,
    UnsupportedCategory,
    InvalidFilter,
    CatalogUnavailable,
    CatalogIdentityConflict,
    NativePreviewFailed,
}

public sealed record NamedFilter(string Value, string Name);

public sealed record PoolFilter(string Value, string Name)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public NamedDefinition? Character { get; init; }
}

public sealed record CardFilters
{
    public ImmutableArray<string> Pools { get; init; }
    public ImmutableArray<string> Types { get; init; }
    public ImmutableArray<string> Rarities { get; init; }
    public ImmutableArray<ResourceCost> EnergyCosts { get; init; }
}

public sealed record CatalogQuery
{
    public required CatalogMode Mode { get; init; }
    public string? Category { get; init; }
    public string? Text { get; init; }
    public CardFilters? Filters { get; init; }
    public int Offset { get; init; }
    public int Limit { get; init; } = 5;
    public ImmutableArray<string> Ids { get; init; }
}

public sealed record CardFilterCatalog(
    ImmutableArray<PoolFilter> Pools,
    ImmutableArray<NamedFilter> Types,
    ImmutableArray<NamedFilter> Rarities,
    ImmutableArray<ResourceCost> EnergyCosts);

public sealed record CatalogData(
    ImmutableArray<CatalogCategory> Categories,
    CardFilterCatalog CardFilters);

public sealed record NativeRelic(string DefinitionId, string Name, string? Text, string? Rarity);

public sealed record NativePotion(
    string DefinitionId,
    string Name,
    string? Text,
    string? TargetType,
    string? Usage);

public sealed record NativeOrb(string DefinitionId, string Name, string? Text);

[JsonDerivedType(typeof(CardDefinition))]
[JsonDerivedType(typeof(RelicDefinition))]
[JsonDerivedType(typeof(PotionDefinition))]
[JsonDerivedType(typeof(OrbDefinition))]
public abstract record NativeDefinition(string DefinitionId, string Name)
{
    public abstract CatalogCategory Category { get; }
    [JsonIgnore] public abstract string? BaseText { get; }
}

public sealed record CardDefinition(
    string DefinitionId,
    string Name,
    CardDefinitionPreview Native,
    UpgradePreview Upgrade) : NativeDefinition(DefinitionId, Name)
{
    public override CatalogCategory Category => CatalogCategory.Card;
    [JsonIgnore] public override string? BaseText => Native.Text;
}

public sealed record RelicDefinition(
    string DefinitionId,
    string Name,
    NativeRelic Native) : NativeDefinition(DefinitionId, Name)
{
    public override CatalogCategory Category => CatalogCategory.Relic;
    [JsonIgnore] public override string? BaseText => Native.Text;
}

public sealed record PotionDefinition(
    string DefinitionId,
    string Name,
    NativePotion Native) : NativeDefinition(DefinitionId, Name)
{
    public override CatalogCategory Category => CatalogCategory.Potion;
    [JsonIgnore] public override string? BaseText => Native.Text;
}

public sealed record OrbDefinition(
    string DefinitionId,
    string Name,
    NativeOrb Native) : NativeDefinition(DefinitionId, Name)
{
    public override CatalogCategory Category => CatalogCategory.Orb;
    [JsonIgnore] public override string? BaseText => Native.Text;
}

[JsonDerivedType(typeof(CardMatch))]
[JsonDerivedType(typeof(NonCardMatch))]
public abstract record NativeMatch(
    CatalogCategory Category,
    string DefinitionId,
    string Name,
    string? Description);

public sealed record CardMatch(
    string DefinitionId,
    string Name,
    string? Description,
    string? CardType,
    string? Rarity,
    ResourceCosts Cost) : NativeMatch(CatalogCategory.Card, DefinitionId, Name, Description);

public sealed record NonCardMatch(
    CatalogCategory Category,
    string DefinitionId,
    string Name,
    string? Description) : NativeMatch(Category, DefinitionId, Name, Description);

public sealed record SearchData(ImmutableArray<NativeMatch> Matches, int Total, int? NextOffset);

public sealed record LookupData(ImmutableArray<NativeDefinition> Entries, ImmutableArray<string> MissingIds);

public sealed record CatalogRecord(
    CatalogCategory Category,
    string DefinitionId,
    string Name,
    string? Text,
    Func<NativeDefinition> Read)
{
    public CardDefinitionPreview? Card { get; init; }
}

public sealed record CatalogPublication(CatalogData Data, ImmutableArray<CatalogRecord> Records);

public sealed record CatalogCause(CatalogErrorCode Code, string Message)
{
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public ImmutableArray<string>? Fields { get; init; }
}
