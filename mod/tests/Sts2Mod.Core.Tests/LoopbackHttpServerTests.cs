using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Sts2Mod.Core.Transport;
using Xunit;

namespace Sts2Mod.Core.Tests;

public sealed class LoopbackHttpServerTests
{
    private sealed class FakeHandlers : IModHttpHandlers
    {
        public Func<HttpReply> OnStatus { get; set; } = static () => new(200, "{\"route\":\"status\"}");
        public Func<string, Task<HttpReply>> OnInspect { get; set; } =
            static body => Task.FromResult(new HttpReply(200, $"{{\"route\":\"inspect\",\"body\":{body}}}"));

        public HttpReply GetStatus() => OnStatus();
        public HttpReply GetState() => new(200, "{\"route\":\"state\"}");
        public Task<HttpReply> Inspect(string body) => OnInspect(body);
        public Task<HttpReply> Catalog(string body) => Task.FromResult(new HttpReply(200, "{\"route\":\"catalog\"}"));
        public HttpReply SubmitAction(string body) => new(200, "{\"route\":\"action\"}");
        public HttpReply SetControl(string body) => new(200, "{\"route\":\"set_control\"}");
        public HttpReply SubmitDisplay(string body) => new(200, "{\"route\":\"display\"}");
        public Task<HttpReply> PresentMap(string body) => Task.FromResult(new HttpReply(200, "{\"route\":\"present\"}"));
        public Task<HttpReply> VerifyMapRestoration(string body) => Task.FromResult(new HttpReply(200, "{\"route\":\"verify\"}"));
        public HttpReply GetCheckpointIdentity() => new(200, "{\"route\":\"checkpoint_identity\"}");
        public HttpReply SubmitCheckpoint(string body) => new(200, "{\"route\":\"checkpoint\"}");
        public Task<HttpReply> PrepareLaboratoryScenario(string body) => Task.FromResult(new HttpReply(200, "{\"route\":\"scenario\"}"));
    }

    private static int AllocateLoopbackPort()
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    [Fact]
    public async Task AllSupportedRoutes_DispatchAndReturnProtocolJson()
    {
        var handlers = new FakeHandlers();
        var port = AllocateLoopbackPort();
        using var server = new LoopbackHttpServer(port, handlers, maxBodyBytes: 256, maxConcurrentRequests: 4);
        server.Start();

        using var client = new HttpClient { BaseAddress = new Uri(server.Prefix) };

        Assert.Equal("{\"route\":\"status\"}", await client.GetStringAsync("/status"));
        Assert.Equal("{\"route\":\"state\"}", await client.GetStringAsync("/state"));
        Assert.Equal("{\"route\":\"checkpoint_identity\"}", await client.GetStringAsync("/operator/checkpoint/identity"));

        using var emptyInspect = await client.PostAsync("/inspect", new StringContent("", Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.OK, emptyInspect.StatusCode);
        Assert.Equal("{\"route\":\"inspect\",\"body\":{}}", await emptyInspect.Content.ReadAsStringAsync());

        foreach (var (path, expected) in new[]
                 {
                     ("/catalog", "catalog"),
                     ("/action", "action"),
                     ("/control", "set_control"),
                     ("/display", "display"),
                     ("/map/present", "present"),
                     ("/map/restore/verify", "verify"),
                     ("/operator/checkpoint", "checkpoint"),
                     ("/operator/eval/scenario", "scenario"),
                 })
        {
            using var response = await client.PostAsync(path, new StringContent("{}", Encoding.UTF8, "application/json"));
            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            Assert.Equal(expected, doc.RootElement.GetProperty("route").GetString());
        }
    }

    [Fact]
    public async Task UnknownRoutesDisallowedMethodsQueryStringsAndOversizedBodies_ReturnHttp400InvalidRequest()
    {
        var handlers = new FakeHandlers();
        var port = AllocateLoopbackPort();
        using var server = new LoopbackHttpServer(port, handlers, maxBodyBytes: 32, maxConcurrentRequests: 4);
        server.Start();

        using var client = new HttpClient { BaseAddress = new Uri(server.Prefix) };

        using var unknown = await client.GetAsync("/act");
        Assert.Equal(HttpStatusCode.BadRequest, unknown.StatusCode);
        using var unknownDoc = JsonDocument.Parse(await unknown.Content.ReadAsStringAsync());
        Assert.Equal("invalid_request", unknownDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        using var getControl = await client.GetAsync("/control");
        Assert.Equal(HttpStatusCode.BadRequest, getControl.StatusCode);

        using var withQuery = await client.GetAsync("/status?extra=1");
        Assert.Equal(HttpStatusCode.BadRequest, withQuery.StatusCode);

        var largePayload = new string('x', 64);
        using var oversized = await client.PostAsync("/action", new StringContent(largePayload, Encoding.UTF8, "application/json"));
        Assert.Equal(HttpStatusCode.BadRequest, oversized.StatusCode);
        using var oversizedDoc = JsonDocument.Parse(await oversized.Content.ReadAsStringAsync());
        Assert.Equal("body", oversizedDoc.RootElement.GetProperty("error").GetProperty("fields")[0].GetString());
    }

    [Fact]
    public async Task HandlerExceptionsAndConcurrencyOverflow_ReturnHttp500TechnicalError()
    {
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var handlers = new FakeHandlers
        {
            OnStatus = static () => throw new InvalidOperationException("Exploded"),
            OnInspect = async _ =>
            {
                entered.TrySetResult();
                await release.Task;
                return new HttpReply(200, "{\"ok\":true}");
            },
        };

        var port = AllocateLoopbackPort();
        using var server = new LoopbackHttpServer(port, handlers, maxBodyBytes: 256, maxConcurrentRequests: 1);
        server.Start();

        using var client = new HttpClient { BaseAddress = new Uri(server.Prefix) };

        using var failed = await client.GetAsync("/status");
        Assert.Equal(HttpStatusCode.InternalServerError, failed.StatusCode);
        using var failedDoc = JsonDocument.Parse(await failed.Content.ReadAsStringAsync());
        Assert.Equal("internal_error", failedDoc.RootElement.GetProperty("error").GetProperty("code").GetString());

        var slowCall = client.PostAsync("/inspect", new StringContent("{}", Encoding.UTF8, "application/json"));
        await entered.Task;

        using var overflow = await client.GetAsync("/state");
        Assert.Equal(HttpStatusCode.InternalServerError, overflow.StatusCode);

        release.TrySetResult();
        using var slowResponse = await slowCall;
        Assert.Equal(HttpStatusCode.OK, slowResponse.StatusCode);
    }
}
