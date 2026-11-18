using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Catalog;
using Sts2Mod.Core.Observation;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class CatalogReaderTests
{
    private static CatalogPublication SampleCatalog(bool duplicateId = false, bool throwOnLookup = false)
    {
        var strikeFacts = new CardEntry(
            "CARD.STRIKE_RED",
            "[gold]Strike[/gold]",
            0,
            "attack",
            "CARD_POOL.IRONCLAD",
            "starter",
            true,
            true,
            [],
            false,
            false,
            false,
            null,
            null,
            new ResourceCosts(ResourceCost.FromNative(1, x: false), ResourceCost.FromNative(-1, x: false)));
        var strikePreview = new CardDefinitionPreview(strikeFacts, "Deal 6 damage.");
        var strikeUpgrade = new CardDefinitionPreview(strikeFacts with { Name = "Strike+", Upgrades = 1 }, "Deal 9 damage.");

        var whirlwindFacts = strikeFacts with
        {
            DefinitionId = "CARD.WHIRLWIND",
            Name = "Whirlwind",
            Rarity = "uncommon",
            Cost = new ResourceCosts(ResourceCost.FromNative(0, x: true), ResourceCost.FromNative(-1, x: false)),
        };
        var whirlwindPreview = new CardDefinitionPreview(whirlwindFacts, "Strike all enemies X times.");

        var records = ImmutableArray.CreateBuilder<CatalogRecord>();
        records.Add(new CatalogRecord(
            CatalogCategory.Card,
            strikePreview.DefinitionId,
            strikePreview.Name,
            strikePreview.Text,
            () => throwOnLookup
                ? throw new InvalidOperationException("Preview boom.")
                : new CardDefinition(
                    strikePreview.DefinitionId,
                    strikePreview.Name,
                    strikePreview,
                    new AvailableUpgradePreview(strikeUpgrade)))
        {
            Card = strikePreview,
        });

        records.Add(new CatalogRecord(
            CatalogCategory.Card,
            whirlwindPreview.DefinitionId,
            whirlwindPreview.Name,
            whirlwindPreview.Text,
            () => new CardDefinition(
                whirlwindPreview.DefinitionId,
                whirlwindPreview.Name,
                whirlwindPreview,
                new NotUpgradablePreview()))
        {
            Card = whirlwindPreview,
        });

        var relicId = duplicateId ? "CARD.STRIKE_RED" : "RELIC.STRIKE_DUMMY";
        var nativeRelic = new NativeRelic(relicId, "Strike Dummy", "Cards containing \"Strike\" deal 3 extra damage.", "uncommon");
        records.Add(new CatalogRecord(
            CatalogCategory.Relic,
            nativeRelic.DefinitionId,
            nativeRelic.Name,
            nativeRelic.Text,
            () => new RelicDefinition(nativeRelic.DefinitionId, nativeRelic.Name, nativeRelic)));

        var data = new CatalogData(
            [CatalogCategory.Card, CatalogCategory.Relic, CatalogCategory.Potion, CatalogCategory.Orb],
            new CardFilterCatalog(
                [new PoolFilter("CARD_POOL.IRONCLAD", "Ironclad") { Character = new NamedDefinition("Ironclad", "CHARACTER.IRONCLAD") }],
                [new NamedFilter("attack", "Attack")],
                [new NamedFilter("starter", "Starter"), new NamedFilter("uncommon", "Uncommon")],
                [ResourceCost.FromNative(1, x: false), ResourceCost.FromNative(0, x: true)]));

        return new CatalogPublication(data, records.ToImmutable());
    }

    [Fact]
    public void CatalogSearchAndLookup_RankFilterPaginateAndPreserveLookupOrder()
    {
        var (metaCode, metaJson) = CatalogReader.Read("{\"mode\":\"catalog\"}", () => SampleCatalog(), "eng");
        Assert.Equal(200, metaCode);
        using var metaDoc = JsonDocument.Parse(metaJson);
        Assert.Equal("completed", metaDoc.RootElement.GetProperty("status").GetString());
        Assert.Equal(4, metaDoc.RootElement.GetProperty("data").GetProperty("categories").GetArrayLength());

        // Search orders ID/name matches ahead of description-only matches and strips BBCode tags.
        var (searchCode, searchJson) = CatalogReader.Read(
            "{\"mode\":\"search\",\"text\":\"Strike\",\"limit\":2,\"offset\":0}",
            () => SampleCatalog(),
            "eng");
        Assert.Equal(200, searchCode);
        using var searchDoc = JsonDocument.Parse(searchJson);
        var data = searchDoc.RootElement.GetProperty("data");
        Assert.Equal(3, data.GetProperty("total").GetInt32());
        Assert.Equal(2, data.GetProperty("next_offset").GetInt32());
        Assert.Equal("CARD.STRIKE_RED", data.GetProperty("matches")[0].GetProperty("definition_id").GetString());
        Assert.Equal("Strike", data.GetProperty("matches")[0].GetProperty("name").GetString());
        Assert.Equal("RELIC.STRIKE_DUMMY", data.GetProperty("matches")[1].GetProperty("definition_id").GetString());

        // Filtered card search with X energy cost.
        var (filteredCode, filteredJson) = CatalogReader.Read(
            "{\"mode\":\"search\",\"category\":\"card\",\"filters\":{\"rarities\":[\"uncommon\"],\"energy_costs\":[\"X\"]}}",
            () => SampleCatalog(),
            "eng");
        Assert.Equal(200, filteredCode);
        using var filteredDoc = JsonDocument.Parse(filteredJson);
        var matches = filteredDoc.RootElement.GetProperty("data").GetProperty("matches");
        Assert.Single(matches.EnumerateArray());
        Assert.Equal("CARD.WHIRLWIND", matches[0].GetProperty("definition_id").GetString());

        // Lookup preserves request order for both entries and missing_ids.
        var (lookupCode, lookupJson) = CatalogReader.Read(
            "{\"mode\":\"lookup\",\"ids\":[\"MISSING.ONE\",\"RELIC.STRIKE_DUMMY\",\"CARD.STRIKE_RED\",\"MISSING.TWO\"]}",
            () => SampleCatalog(),
            "eng");
        Assert.Equal(200, lookupCode);
        using var lookupDoc = JsonDocument.Parse(lookupJson);
        var entries = lookupDoc.RootElement.GetProperty("data").GetProperty("entries");
        var missing = lookupDoc.RootElement.GetProperty("data").GetProperty("missing_ids");
        Assert.Equal("RELIC.STRIKE_DUMMY", entries[0].GetProperty("definition_id").GetString());
        Assert.Equal("CARD.STRIKE_RED", entries[1].GetProperty("definition_id").GetString());
        Assert.Equal("Strike", entries[1].GetProperty("name").GetString());
        Assert.Equal("MISSING.ONE", missing[0].GetString());
        Assert.Equal("MISSING.TWO", missing[1].GetString());
    }

    [Fact]
    public void CatalogReader_ReportsContractRejectionsAndCatalogFailuresCleanly()
    {
        var (conflictCode, conflictJson) = CatalogReader.Read("{\"mode\":\"catalog\"}", () => SampleCatalog(duplicateId: true), "eng");
        Assert.Equal(200, conflictCode);
        using var conflictDoc = JsonDocument.Parse(conflictJson);
        Assert.Equal("catalog_identity_conflict", conflictDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var (previewFailCode, previewFailJson) = CatalogReader.Read(
            "{\"mode\":\"lookup\",\"ids\":[\"CARD.STRIKE_RED\"]}",
            () => SampleCatalog(throwOnLookup: true),
            "eng");
        Assert.Equal(200, previewFailCode);
        using var previewFailDoc = JsonDocument.Parse(previewFailJson);
        Assert.Equal("native_preview_failed", previewFailDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var (catCode, catJson) = CatalogReader.Read("{\"mode\":\"search\",\"category\":\"event\"}", () => SampleCatalog(), "eng");
        Assert.Equal(200, catCode);
        using var catDoc = JsonDocument.Parse(catJson);
        Assert.Equal("unsupported_category", catDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var (filterCode, filterJson) = CatalogReader.Read(
            "{\"mode\":\"search\",\"category\":\"card\",\"filters\":{\"types\":[\"curse\"]}}",
            () => SampleCatalog(),
            "eng");
        Assert.Equal(200, filterCode);
        using var filterDoc = JsonDocument.Parse(filterJson);
        Assert.Equal("invalid_filter", filterDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var (unavailableCode, unavailableJson) = CatalogReader.Read(
            "{\"mode\":\"catalog\"}",
            static () => throw new InvalidOperationException("DB unready"),
            "eng");
        Assert.Equal(200, unavailableCode);
        using var unavailableDoc = JsonDocument.Parse(unavailableJson);
        Assert.Equal("catalog_unavailable", unavailableDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        foreach (var invalidRequest in new[]
                 {
                     "{\"mode\":\"catalog\",\"text\":\"extra\"}",
                     "{\"mode\":\"lookup\",\"ids\":[]}",
                     "{\"mode\":\"lookup\",\"ids\":[\"CARD.A\",\"CARD.A\"]}",
                     "{\"mode\":\"search\",\"filters\":{\"types\":[\"attack\"]}}",
                     "{\"mode\":\"search\",\"limit\":25}",
                 })
        {
            var (badCode, _) = CatalogReader.Read(invalidRequest, () => SampleCatalog(), "eng");
            Assert.Equal(400, badCode);
        }
    }
}
