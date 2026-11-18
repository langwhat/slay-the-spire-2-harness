using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class JsonContractTests
{
    private enum SampleMode
    {
        CardSelection,
        EndTurn,
    }

    private sealed record SampleEnvelope(
        SampleMode CurrentMode,
        ImmutableDictionary<string, int> ObjectCounts,
        ResourceCost Cost);

    [Fact]
    public void Serialize_UsesSnakeCaseForPropertiesAndEnumsWhilePreservingDictionaryKeys()
    {
        var value = new SampleEnvelope(
            SampleMode.CardSelection,
            ImmutableDictionary<string, int>.Empty
                .Add("card:1:1", 2)
                .Add("CARD.STRIKE_RED", 1),
            ResourceCost.FromNative(2, x: false));

        var json = JsonContract.Serialize(value);
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;

        Assert.Equal("card_selection", root.GetProperty("current_mode").GetString());
        Assert.Equal(2, root.GetProperty("cost").GetInt32());

        var counts = root.GetProperty("object_counts");
        Assert.Equal(2, counts.GetProperty("card:1:1").GetInt32());
        Assert.Equal(1, counts.GetProperty("CARD.STRIKE_RED").GetInt32());

        var roundTrip = JsonContract.DeserializeUnique<SampleEnvelope>(json);
        Assert.NotNull(roundTrip);
        Assert.Equal(SampleMode.CardSelection, roundTrip!.CurrentMode);
        Assert.Equal(new ResourceCost(ResourceCostKind.Number, 2), roundTrip.Cost);
    }

    [Fact]
    public void ResourceCostConverter_RoundTripsAllVariantsAndRejectsInvalidTokens()
    {
        Assert.Equal("0", JsonContract.Serialize(ResourceCost.FromNative(0, x: false)));
        Assert.Equal("\"X\"", JsonContract.Serialize(ResourceCost.FromNative(0, x: true)));
        Assert.Equal("\"none\"", JsonContract.Serialize(ResourceCost.FromNative(-1, x: false)));
        Assert.Equal("null", JsonContract.Serialize(new ResourceCost(ResourceCostKind.Unknown)));

        Assert.Equal(new ResourceCost(ResourceCostKind.Number, 3), JsonContract.DeserializeUnique<ResourceCost>("3"));
        Assert.Equal(new ResourceCost(ResourceCostKind.X), JsonContract.DeserializeUnique<ResourceCost>("\"X\""));
        Assert.Equal(new ResourceCost(ResourceCostKind.None), JsonContract.DeserializeUnique<ResourceCost>("\"none\""));
        Assert.Equal(new ResourceCost(ResourceCostKind.Unknown), JsonContract.DeserializeUnique<ResourceCost>("null"));

        Assert.Throws<JsonException>(() => JsonContract.DeserializeUnique<ResourceCost>("-1"));
        Assert.Throws<JsonException>(() => JsonContract.DeserializeUnique<ResourceCost>("\"free\""));
    }

    [Theory]
    [InlineData("{\"a\":1,\"a\":2}")]
    [InlineData("{\"outer\":{\"dup\":true,\"dup\":false}}")]
    [InlineData("{\"items\":[{\"id\":\"x\",\"id\":\"y\"}]}")]
    public void EnsureNoDuplicateProperties_RejectsDuplicatesAtAnyDepth(string json)
    {
        Assert.Throws<JsonException>(() => JsonContract.EnsureNoDuplicateProperties(json));
    }

    [Fact]
    public void DeserializeUnique_RejectsUnknownPropertiesAndIntegerEnumValues()
    {
        Assert.Throws<JsonException>(() =>
            JsonContract.DeserializeUnique<SampleEnvelope>(
                "{\"current_mode\":\"end_turn\",\"object_counts\":{},\"cost\":1,\"extra\":true}"));

        Assert.Throws<JsonException>(() =>
            JsonContract.DeserializeUnique<SampleEnvelope>(
                "{\"current_mode\":0,\"object_counts\":{},\"cost\":1}"));
    }

    [Fact]
    public void SerializeCanonical_SortsObjectPropertiesOrdinallyAndPreservesArrayOrder()
    {
        var payload = new Dictionary<string, object>
        {
            ["z_key"] = new[] { "b", "a" },
            ["a_key"] = new Dictionary<string, int>
            {
                ["second"] = 2,
                ["first"] = 1,
            },
        };

        var canonical = JsonContract.SerializeCanonical(payload);
        Assert.Equal("{\"a_key\":{\"first\":1,\"second\":2},\"z_key\":[\"b\",\"a\"]}", canonical);
    }

    [Fact]
    public void ValidationHelpersAndErrorReplies_MatchWireSchema()
    {
        using var document = JsonDocument.Parse("{\"id\":\"op-1\",\"opt\":\"ok\"}");
        var root = document.RootElement;

        JsonContract.RequireKeys(root, ["id"], ["opt"]);
        Assert.Equal("op-1", JsonContract.RequireText(root, "id"));
        Assert.Throws<JsonException>(() => JsonContract.RequireKeys(root, ["id"]));
        Assert.Throws<JsonException>(() => JsonContract.RequireKeys(root, ["id", "missing"], ["opt"]));

        using var invalidDoc = JsonDocument.Parse(InvalidRequestReply.Json("Bad input.", "id", "step"));
        Assert.Equal("rejected", invalidDoc.RootElement.GetProperty("status").GetString());
        Assert.Equal("invalid_request", invalidDoc.RootElement.GetProperty("error").GetProperty("code").GetString());
        Assert.Equal(2, invalidDoc.RootElement.GetProperty("error").GetProperty("fields").GetArrayLength());

        using var techDoc = JsonDocument.Parse(TechnicalErrorReply.Json("Boom."));
        Assert.Equal("failed", techDoc.RootElement.GetProperty("status").GetString());
        Assert.Equal("internal_error", techDoc.RootElement.GetProperty("error").GetProperty("code").GetString());
    }
}
