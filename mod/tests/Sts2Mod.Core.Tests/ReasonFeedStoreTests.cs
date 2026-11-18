using System.Text.Json;
using Sts2Mod.Core.Display;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class ReasonFeedStoreTests
{
    [Fact]
    public void WriteJson_ValidatesContractDeduplicatesConsecutiveEntriesAndBoundsFeed()
    {
        var store = new ReasonFeedStore(maxEntries: 3, entryLifetime: TimeSpan.FromSeconds(20), maxReasonLength: 40);
        var start = new DateTimeOffset(2026, 11, 18, 12, 0, 0, TimeSpan.Zero);

        var first = store.WriteJson("{\"reason\":\"  Open   the chest\\nto inspect relics. \"}", start);
        Assert.Equal(200, first.StatusCode);
        Assert.Equal("{\"status\":\"shown\"}", first.Json);

        var active = store.GetActive(start.AddSeconds(1));
        Assert.Single(active);
        Assert.Equal("Open the chest to inspect relics.", active[0].Text);
        var firstId = active[0].Id;

        // Consecutive identical normalized text refreshes timestamp instead of stacking duplicates.
        var duplicate = store.WriteJson("{\"reason\":\"Open the chest to inspect relics.\"}", start.AddSeconds(12));
        Assert.Equal(200, duplicate.StatusCode);
        var refreshed = store.GetActive(start.AddSeconds(25));
        Assert.Single(refreshed);
        Assert.Equal(firstId, refreshed[0].Id);

        store.Record("Play Bash first to apply Vulnerable.", start.AddSeconds(26));
        store.Record("Spend remaining energy on Defend.", start.AddSeconds(27));
        store.Record("End turn with full block.", start.AddSeconds(28));

        var bounded = store.GetActive(start.AddSeconds(29), maxCount: 4);
        Assert.Equal(3, bounded.Length);
        Assert.Equal("End turn with full block.", bounded[0].Text);
        Assert.Equal("Spend remaining energy on Defend.", bounded[1].Text);
        Assert.Equal("Play Bash first to apply Vulnerable.", bounded[2].Text);

        // Long text is cleanly truncated with an ellipsis.
        store.Record(new string('A', 60), start.AddSeconds(30));
        var truncated = store.GetActive(start.AddSeconds(30), maxCount: 1);
        Assert.Equal(40, truncated[0].Text.Length);
        Assert.EndsWith("…", truncated[0].Text, StringComparison.Ordinal);

        // Expired entries drop out automatically, and Clear empties remaining entries.
        Assert.Empty(store.GetActive(start.AddSeconds(60)));
        store.Record("Fresh entry", start.AddSeconds(61));
        Assert.Single(store.GetActive(start.AddSeconds(61)));
        store.Clear();
        Assert.Empty(store.GetActive(start.AddSeconds(61)));
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("{\"reason\":\"\"}")]
    [InlineData("{\"reason\":\"   \\n\\t  \"}")]
    [InlineData("{\"reason\":123}")]
    [InlineData("{\"reason\":\"ok\",\"extra\":true}")]
    [InlineData("{\"reason\":\"first\",\"reason\":\"second\"}")]
    [InlineData("not-json")]
    public void WriteJson_RejectsMalformedBlankDuplicateOrExtraFields(string invalidBody)
    {
        var store = new ReasonFeedStore();
        var reply = store.WriteJson(invalidBody);

        Assert.Equal(400, reply.StatusCode);
        using var document = JsonDocument.Parse(reply.Json);
        Assert.Equal("rejected", document.RootElement.GetProperty("status").GetString());
        Assert.Equal("invalid_request", document.RootElement.GetProperty("error").GetProperty("code").GetString());
        Assert.Equal("reason", document.RootElement.GetProperty("error").GetProperty("fields")[0].GetString());
    }
}
