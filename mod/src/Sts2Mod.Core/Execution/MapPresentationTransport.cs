using System.Globalization;
using System.Text.Json;
using Sts2Mod.Core.Transport;

namespace Sts2Mod.Core.Execution;

public sealed record PresentationEdge(string From, string To);

public sealed record PresentationRequest(string OperationId, StepRoute Route, IReadOnlyList<PresentationEdge> Edges);

public sealed record PresentationVerificationRequest(string OperationId, StepRoute Route);

public sealed class MapPresentationTransport(
    Func<PresentationRequest, Task<HttpReply>> present,
    Func<PresentationVerificationRequest, Task<HttpReply>> verify)
{
    public Task<HttpReply> Present(string body)
    {
        try
        {
            using var document = Parse(body, includeEdges: true);
            var root = document.RootElement;
            var operationId = JsonContract.RequireText(root, "operation_id");
            var route = StepRoute.ParseJson(root.GetProperty("expected_route"));
            var edges = root.GetProperty("edges");
            if (edges.ValueKind != JsonValueKind.Array || edges.GetArrayLength() == 0)
                throw new JsonException();

            var unique = new HashSet<(string From, string To)>();
            var parsed = new List<PresentationEdge>();
            foreach (var edge in edges.EnumerateArray())
            {
                JsonContract.RequireKeys(edge, ["from", "to"]);
                var from = JsonContract.RequireText(edge, "from");
                var to = JsonContract.RequireText(edge, "to");
                if (!IsMapCoordinateId(from) || !IsMapCoordinateId(to) || !unique.Add((from, to)))
                    throw new JsonException();
                parsed.Add(new(from, to));
            }

            return present(new(operationId, route, parsed));
        }
        catch (JsonException)
        {
            return Task.FromResult(new HttpReply(400, InvalidRequestReply.Json(
                "The request is invalid.",
                "operation_id",
                "expected_route",
                "edges")));
        }
    }

    public Task<HttpReply> Verify(string body)
    {
        try
        {
            using var document = Parse(body, includeEdges: false);
            var root = document.RootElement;
            var operationId = JsonContract.RequireText(root, "operation_id");
            var route = StepRoute.ParseJson(root.GetProperty("expected_route"));
            return verify(new(operationId, route));
        }
        catch (JsonException)
        {
            return Task.FromResult(new HttpReply(400, InvalidRequestReply.Json(
                "The request is invalid.",
                "operation_id",
                "expected_route")));
        }
    }

    private static JsonDocument Parse(string body, bool includeEdges)
    {
        JsonContract.EnsureNoDuplicateProperties(body);
        var document = JsonDocument.Parse(body);
        try
        {
            JsonContract.RequireKeys(
                document.RootElement,
                includeEdges ? ["operation_id", "expected_route", "edges"] : ["operation_id", "expected_route"]);
            return document;
        }
        catch
        {
            document.Dispose();
            throw;
        }
    }

    private static bool IsMapCoordinateId(string value)
    {
        var parts = value.Split(':');
        return parts.Length == 3 && parts[0] == "map" && IsCoordinate(parts[1]) && IsCoordinate(parts[2]);
    }

    private static bool IsCoordinate(string value) =>
        int.TryParse(value, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var coordinate)
        && coordinate.ToString(CultureInfo.InvariantCulture) == value;
}
