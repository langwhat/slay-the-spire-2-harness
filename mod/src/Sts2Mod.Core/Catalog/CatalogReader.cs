using System.Collections.Immutable;
using System.Text.Json;
using Sts2Mod.Core.Observation;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Catalog;

/// <summary>
/// Validates catalog requests and serves native definition metadata, filtered search, and exact ID lookups.
/// </summary>
public static class CatalogReader
{
    public static (int StatusCode, string Json) Read(
        string body,
        Func<CatalogPublication> capture,
        string gameLanguage)
    {
        var result = ReadCore(body, capture);
        return (result.StatusCode, SemanticText.Json(result.Json, gameLanguage));
    }

    public static (int StatusCode, string Json) Unavailable() =>
        Error(
            200,
            ReadOutcome.Failed,
            CatalogErrorCode.CatalogUnavailable,
            "The native definition catalog could not be read.");

    private static (int StatusCode, string Json) ReadCore(string body, Func<CatalogPublication> capture)
    {
        CatalogQuery query;
        try
        {
            query = Decode(body);
        }
        catch (JsonException)
        {
            return Error(
                400,
                ReadOutcome.Rejected,
                CatalogErrorCode.InvalidRequest,
                "The request does not match the catalog contract.",
                ["request"]);
        }

        CatalogPublication catalog;
        try
        {
            catalog = capture();
        }
        catch (Exception)
        {
            return Unavailable();
        }

        if (catalog.Records
            .GroupBy(record => record.DefinitionId, StringComparer.Ordinal)
            .Any(group => group.Count() != 1))
        {
            return Error(
                200,
                ReadOutcome.Failed,
                CatalogErrorCode.CatalogIdentityConflict,
                "Native definition IDs are not unique across the catalog.");
        }

        if (query.Mode == CatalogMode.Catalog)
            return Completed(catalog.Data);

        if (query.Mode == CatalogMode.Lookup)
        {
            var index = catalog.Records.ToDictionary(record => record.DefinitionId, StringComparer.Ordinal);
            var found = ImmutableArray.CreateBuilder<NativeDefinition>();
            var missing = ImmutableArray.CreateBuilder<string>();
            try
            {
                foreach (var id in query.Ids)
                {
                    if (index.TryGetValue(id, out var record))
                        found.Add(record.Read());
                    else
                        missing.Add(id);
                }
            }
            catch (Exception)
            {
                return Error(
                    200,
                    ReadOutcome.Failed,
                    CatalogErrorCode.NativePreviewFailed,
                    "A native definition preview could not be rendered.");
            }
            return Completed(new LookupData(found.ToImmutable(), missing.ToImmutable()));
        }

        if (query.Category is not null
            && !catalog.Data.Categories.Any(category => JsonContract.Wire(category) == query.Category))
        {
            return Error(
                200,
                ReadOutcome.Rejected,
                CatalogErrorCode.UnsupportedCategory,
                "This native category is not supported.");
        }

        if (query.Filters is { } filters && !ValidFilters(filters, catalog.Data.CardFilters))
        {
            return Error(
                200,
                ReadOutcome.Rejected,
                CatalogErrorCode.InvalidFilter,
                "A card filter contains an unknown value.");
        }

        var terms = (query.Text ?? "").Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        var matches = catalog.Records
            .Where(record => query.Category is null || JsonContract.Wire(record.Category) == query.Category)
            .Where(record => MatchesFilters(record, query.Filters))
            .Where(record => terms.All(term =>
                Contains(record.DefinitionId, term)
                || Contains(record.Name, term)
                || Contains(record.Text, term)))
            .OrderBy(record => Rank(record, terms, query.Text ?? ""))
            .ThenBy(record => record.DefinitionId, StringComparer.Ordinal)
            .ToArray();

        var page = matches.Skip(query.Offset).Take(query.Limit).Select(ToMatch).ToImmutableArray();
        var next = query.Offset + page.Length;
        return Completed(new SearchData(page, matches.Length, next < matches.Length ? next : null));
    }

    private static CatalogQuery Decode(string body)
    {
        var query = JsonContract.DeserializeUnique<CatalogQuery>(body) ?? throw new JsonException();
        using var document = JsonDocument.Parse(body);
        var root = document.RootElement;
        if (root.GetProperty("mode").GetString() != JsonContract.Wire(query.Mode))
            throw new JsonException();

        var allowed = query.Mode switch
        {
            CatalogMode.Catalog => new[] { "mode" },
            CatalogMode.Lookup => ["mode", "ids"],
            CatalogMode.Search => ["mode", "category", "text", "filters", "offset", "limit"],
            _ => throw new JsonException(),
        };
        if (root.EnumerateObject().Any(property => !allowed.Contains(property.Name, StringComparer.Ordinal)))
            throw new JsonException();

        if (query.Mode == CatalogMode.Lookup
            && (query.Ids.IsDefault || query.Ids.Length is < 1 or > 20 || !Distinct(query.Ids)))
        {
            throw new JsonException();
        }

        if (query.Offset < 0 || query.Limit is < 1 or > 20)
            throw new JsonException();

        foreach (var name in new[] { "category", "text", "filters" })
        {
            if (root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Null)
                throw new JsonException();
        }

        if (query.Category is { Length: 0 } || (query.Filters is not null && query.Category != "card"))
            throw new JsonException();

        if (query.Filters is { } filters)
        {
            if (!OptionalDistinct(filters.Pools)
                || !OptionalDistinct(filters.Types)
                || !OptionalDistinct(filters.Rarities)
                || (!filters.EnergyCosts.IsDefault
                    && (filters.EnergyCosts.IsEmpty
                        || filters.EnergyCosts.Distinct().Count() != filters.EnergyCosts.Length
                        || filters.EnergyCosts.Any(cost => cost.Kind is not (ResourceCostKind.Number or ResourceCostKind.X)))))
            {
                throw new JsonException();
            }

            if (root.GetProperty("filters").EnumerateObject().Any(property => property.Value.ValueKind == JsonValueKind.Null))
                throw new JsonException();
        }

        return query;
    }

    private static bool Distinct(ImmutableArray<string> values) =>
        !values.Any(string.IsNullOrEmpty)
        && values.Distinct(StringComparer.Ordinal).Count() == values.Length;

    private static bool OptionalDistinct(ImmutableArray<string> values) =>
        values.IsDefault || (!values.IsEmpty && Distinct(values));

    private static bool ValidFilters(CardFilters filters, CardFilterCatalog available) =>
        Known(filters.Pools, available.Pools.Select(value => value.Value))
        && Known(filters.Types, available.Types.Select(value => value.Value))
        && Known(filters.Rarities, available.Rarities.Select(value => value.Value))
        && (filters.EnergyCosts.IsDefault || filters.EnergyCosts.All(available.EnergyCosts.Contains));

    private static bool Known(ImmutableArray<string> values, IEnumerable<string> available) =>
        values.IsDefault || values.All(available.Contains);

    private static bool MatchesFilters(CatalogRecord record, CardFilters? filters)
    {
        if (filters is null)
            return true;
        if (record.Card is not { } card)
            return false;
        return Included(filters.Pools, card.CardPool)
               && Included(filters.Types, card.CardType)
               && Included(filters.Rarities, card.Rarity)
               && (filters.EnergyCosts.IsDefault || filters.EnergyCosts.Contains(card.Cost.Energy));
    }

    private static bool Included(ImmutableArray<string> values, string? actual) =>
        values.IsDefault || (actual is not null && values.Contains(actual));

    private static bool Contains(string? text, string term) =>
        text?.Contains(term, StringComparison.OrdinalIgnoreCase) == true;

    private static int Rank(CatalogRecord record, string[] terms, string text) =>
        string.Equals(record.DefinitionId, text, StringComparison.OrdinalIgnoreCase)
        || string.Equals(record.Name, text, StringComparison.OrdinalIgnoreCase)
            ? 0
            : terms.All(term => Contains(record.DefinitionId, term) || Contains(record.Name, term))
                ? 1
                : 2;

    private static NativeMatch ToMatch(CatalogRecord record) =>
        record.Card is { } card
            ? new CardMatch(record.DefinitionId, record.Name, record.Text, card.CardType, card.Rarity, card.Cost)
            : new NonCardMatch(record.Category, record.DefinitionId, record.Name, record.Text);

    private static (int StatusCode, string Json) Completed<T>(T data) =>
        (200, JsonContract.Serialize(new { status = ReadOutcome.Completed, data }));

    private static (int StatusCode, string Json) Error(
        int http,
        ReadOutcome status,
        CatalogErrorCode code,
        string message,
        ImmutableArray<string>? fields = null) =>
        (http, JsonContract.Serialize(new
        {
            status,
            error = new CatalogCause(code, message) { Fields = fields },
        }));
}
